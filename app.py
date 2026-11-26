"""
Commit reviewer: GitHub webhook -> ocr (any OpenAI-compatible LLM) -> per-finding verification -> status, comment, report.

One commit at a time, in arrival order:
  1. status "pending" on the commit;
  2. bare mirror of the repository (clone the first time, fetch after) and a detached worktree at the commit;
  3. `ocr review --commit <sha>` against the configured model;
  4. every finding goes through a second call that confirms, refutes or leaves it uncertain and rates severity
     (same idea as CodeRabbit's "unverified_finding"; the verifier is NOT independent: same model);
  5. tokens and cost of every call are recorded (DATA_DIR/usage.jsonl and the report);
  6. JSON + Markdown report in DATA_DIR/reports/<owner>__<repo>/<sha>.*;
  7. commit comment (findings not refuted) and final status (failure on a confirmed finding at or above GATE).

Bring your own key: LLM_BASE_URL, LLM_API_KEY and LLM_MODEL are the only LLM settings. The same endpoint serves
`ocr` (registered as the custom provider "byok", OpenAI protocol) and the verifier calls made here.
"""
import base64
import hashlib
import hmac
import json
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import audit
import render

DATA = Path(os.getenv("DATA_DIR", "/data"))
SECRET = os.environ["WEBHOOK_SECRET"].encode()
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPOS = {r.strip().lower() for r in os.environ["REPOS"].split(",") if r.strip()}

# ---------- BYOK: one OpenAI-compatible endpoint for review and verification ----------
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
LLM_API_KEY = os.environ["LLM_API_KEY"]
MODEL = os.getenv("LLM_MODEL", "deepseek-flash")
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "deepseek")  # models.dev provider id, used only to look up prices
OCR_PROVIDER = "byok"  # name ocr knows the endpoint by; fixed, independent of LLM_PROVIDER
MODELS_DEV = "https://models.dev/api.json"

SEVERITIES = ["critical", "major", "minor"]
GATE = os.getenv("GATE", "major")
DRY_RUN = os.getenv("DRY_RUN") == "1"  # never call GitHub (status/comment go to the log only)
PORT = int(os.getenv("PORT", 8080))
CONTEXT = "countersign/review"
MAX_BODY = 25 * 1024 * 1024  # GitHub webhook payload ceiling
SHA = re.compile(r"^[0-9a-f]{40}$")
REPO = re.compile(r"^[\w.-]+/[\w.-]+$")

CONTEXT_PR = "countersign/audit"
AUDIT_BASE = os.getenv("AUDIT_BASE", "main")
PR_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}
# endereço público do serviço, para o link "Details" do status apontar ao relatório
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")


def relatorio_url(repo: str, nome: str) -> str | None:
    """URL pública do relatório de um commit (sha) ou de uma PR (pr-<n>-<sha>)."""
    return f"{PUBLIC_URL}/reports/{repo}/{nome}" if PUBLIC_URL else None

QUEUE: "queue.Queue[tuple]" = queue.Queue()  # (function, *args)
QUEUE_FILE = DATA / "queue.json"
FILA_LOCK = threading.Lock()
MIRROR_LOCK = threading.Lock()  # fetch no espelho é o único passo compartilhado entre workers
ANDAMENTO: dict[str, float] = {}
ANDAMENTO_LOCK = threading.Lock()
EM_CURSO: dict[str, tuple] = {}  # job retirado da fila: fica no arquivo até terminar
WORKERS = max(1, int(os.getenv("WORKERS", "1")))


def log(msg: str):
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def salva_fila() -> None:
    """Fila em disco: reinício do container não perde push já recebido (webhook não é reenviado).
    Inclui o job em execução, senão um restart no meio do review perde justamente esse commit."""
    with FILA_LOCK:
        itens = list(QUEUE.queue) + list(EM_CURSO.values())
        dados = [{"funcao": f.__name__, "args": list(a)} for f, *a in itens]
        tmp = QUEUE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
        tmp.replace(QUEUE_FILE)


def enfileira(funcao, *args) -> None:
    QUEUE.put((funcao, *args))
    salva_fila()


def carrega_fila() -> int:
    """Devolve à fila o que ficou pendente do processo anterior."""
    if not QUEUE_FILE.exists():
        return 0
    try:
        itens = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    except Exception as e:  # arquivo corrompido não impede subir
        log(f"fila em disco ilegível ({e}); seguindo vazio")
        itens = []
    for item in itens:
        funcao = {"review_commit": review_commit, "review_pr": review_pr}.get(item.get("funcao"))
        args = item.get("args", [])
        if funcao is review_commit and not (len(args) >= 2 and SHA.match(str(args[1]))):
            log(f"fila em disco: entrada descartada, sha inválido ({args[1:2]})")
            continue
        if funcao:
            QUEUE.put((funcao, *args))
    QUEUE_FILE.unlink(missing_ok=True)
    return QUEUE.qsize()


def estado(ultimo: str | None = None) -> dict:
    with ANDAMENTO_LOCK:
        em_andamento = {k: round(time.monotonic() - v) for k, v in ANDAMENTO.items()}
    indice = DATA / "reports" / "index.md"
    linha = ultimo
    if linha is None and indice.exists():
        linhas = [l for l in indice.read_text(encoding="utf-8").splitlines() if l.strip()]
        linha = linhas[-1] if linhas else None
    return {"ok": True, "model": MODEL, "workers": WORKERS, "queue": QUEUE.qsize(),
            "running": em_andamento, "last": linha}


# ---------- pure functions (tested in test_app.py) ----------

def signature_valid(body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(SECRET, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):])


def push_commits(event: dict) -> list[tuple[str, str, str]]:
    """(repo, sha, clone_url) of every new commit in a push to a configured repository."""
    repo = event.get("repository", {}).get("full_name", "").lower()
    if repo not in REPOS or not REPO.match(repo) or event.get("deleted"):
        return []
    clone_url = event["repository"]["clone_url"]
    return [(repo, c["id"], clone_url) for c in event.get("commits", []) if SHA.match(c.get("id", ""))]


def pr_to_audit(event: dict) -> tuple | None:
    """(repo, number, head sha, base sha, clone_url) of a PR opened/updated against AUDIT_BASE; else None."""
    pr = event.get("pull_request") or {}
    repo = event.get("repository", {}).get("full_name", "").lower()
    if (repo not in REPOS or not REPO.match(repo) or event.get("action") not in PR_ACTIONS or pr.get("draft")
            or pr.get("base", {}).get("ref") != AUDIT_BASE):
        return None
    head, base = pr.get("head", {}).get("sha", ""), pr.get("base", {}).get("sha", "")
    if not (SHA.match(head) and SHA.match(base)):
        return None
    return repo, int(pr["number"]), head, base, event["repository"]["clone_url"]


def final_state(findings: list[dict]) -> tuple[str, str]:
    limit = SEVERITIES.index(GATE)
    blocking = [f for f in findings if f["verification"]["verdict"] == "confirmed"
                and SEVERITIES.index(f["verification"]["severity"]) <= limit]
    confirmed = sum(f["verification"]["verdict"] == "confirmed" for f in findings)
    text = f"{confirmed} confirmed, {len(findings)} finding(s) total"
    return ("failure" if blocking else "success"), text


def normalize_verification(raw: dict) -> dict:
    verdict = raw.get("verdict") if raw.get("verdict") in ("confirmed", "refuted", "uncertain") else "uncertain"
    severity = raw.get("severity") if raw.get("severity") in SEVERITIES else "minor"
    return {"verdict": verdict, "severity": severity, "reason": str(raw.get("reason", ""))[:600]}


def cached_tokens(usage: dict) -> int:
    """Cache hits are provider specific: DeepSeek prompt_cache_hit_tokens, OpenAI prompt_tokens_details.cached_tokens."""
    return int(usage.get("prompt_cache_hit_tokens")
               or (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)


def cost_usd(input_tokens: int, cached_tokens: int, output_tokens: int, price: dict | None) -> float | None:
    """USD for one review. price is USD per million tokens: {"input": x, "output": y, "cache_read": z}."""
    if not price or "input" not in price or "output" not in price:
        return None
    cached = min(cached_tokens, input_tokens)
    cache_price = price.get("cache_read", price["input"])
    return round(((input_tokens - cached) * price["input"] + cached * cache_price
                  + output_tokens * price["output"]) / 1_000_000, 6)


def pick_price(catalog: dict, provider: str, model: str) -> dict | None:
    """Cost entry from the models.dev catalog: the provider's own entry first, then any provider listing the model."""
    def cost(entry: dict) -> dict | None:
        c = (entry or {}).get("cost") or {}
        return {k: c[k] for k in ("input", "output", "cache_read") if isinstance(c.get(k), (int, float))}

    own = cost((catalog.get(provider) or {}).get("models", {}).get(model))
    if own:
        return own
    for entry in catalog.values():
        for key, info in ((entry or {}).get("models") or {}).items():
            if (key == model or info.get("canonical_model_id", "").endswith(f"/{model}")) and cost(info):
                return cost(info)
    return None


def money(cost: float | None) -> str:
    if cost is None:
        return "n/a (no price)"
    return f"${cost:.4f}" if cost >= 0.01 else f"${cost:.6f}"


def markdown(sha: str, findings: list[dict], usage: dict) -> str:
    lines = [f"## Automated review — `{sha[:7]}` ({MODEL})", "",
             f"{final_state(findings)[1]}; "
             f"{sum(f['verification']['verdict'] == 'refuted' for f in findings)} refuted by the verifier are hidden below. "
             f"Tokens: {usage['total_tokens']:,} ({usage['cached_input_tokens']:,} cached) · "
             f"Cost: {usage['cost_text']}.", ""]
    for f in findings:
        v = f["verification"]
        if v["verdict"] == "refuted":
            continue
        lines += [f"### {v['severity']} · {v['verdict']} — `{f['path']}:{f.get('start_line')}-{f.get('end_line')}`",
                  "", f["content"].strip(), "", f"_Verifier:_ {v['reason']}", ""]
        if f.get("suggestion_code"):
            lines += ["```", f["suggestion_code"].rstrip(), "```", ""]
    lines.append("_Generated by ocr + the configured model. Verifier = same model, not independent. "
                 "Not a definitive verdict._")
    return "\n".join(lines)


# ---------- pricing (models.dev, same catalog opencode uses) ----------

PRICE: dict | None = None


def load_price() -> dict | None:
    """USD per million tokens for MODEL. Env wins, then a cached lookup, then models.dev. None = unknown."""
    env = {k: os.getenv(f"PRICE_{k.upper()}") for k in ("input", "output", "cache_read")}
    if env["input"] and env["output"]:
        return {**{k: float(v) for k, v in env.items() if v}, "source": "env"}
    cache = DATA / "prices.json"
    try:
        cached = json.loads(cache.read_text(encoding="utf-8"))
        if cached.get("provider") == LLM_PROVIDER and cached.get("model") == MODEL and cached.get("cost"):
            return cached["cost"]
    except (OSError, ValueError):
        pass
    try:
        request = urllib.request.Request(MODELS_DEV, headers={"User-Agent": "countersign"})
        catalog = json.load(urllib.request.urlopen(request, timeout=30))
        price = pick_price(catalog, LLM_PROVIDER, MODEL)
        if price:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"provider": LLM_PROVIDER, "model": MODEL, "cost": price,
                                         "fetched": f"{datetime.now(timezone.utc):%Y-%m-%d}"}), encoding="utf-8")
        return price
    except Exception as e:  # no network is not fatal: the review still runs, cost stays unknown
        log(f"price lookup failed for {LLM_PROVIDER}/{MODEL}: {e}")
        return None


class Usage:
    """Token counters for one review. Thread-safe: the PR audit calls run in a pool."""

    def __init__(self):
        self.lock = threading.Lock()
        self.input = self.cached = self.output = 0

    def add(self, input_tokens: int = 0, cached_input_tokens: int = 0, output_tokens: int = 0):
        with self.lock:
            self.input += int(input_tokens or 0)
            self.cached += int(cached_input_tokens or 0)
            self.output += int(output_tokens or 0)

    def add_chat(self, response: dict):
        usage = response.get("usage") or {}
        self.add(usage.get("prompt_tokens"), cached_tokens(usage), usage.get("completion_tokens"))

    def report(self, price: dict | None) -> dict:
        """Tokens and cost. price is passed in, so nothing depends on module state (None = cost unknown)."""
        with self.lock:
            cached = min(self.cached, self.input)
            cost = cost_usd(self.input, cached, self.output, price)
            return {"input_tokens": self.input, "cached_input_tokens": cached, "output_tokens": self.output,
                    "total_tokens": self.input + self.output, "cost_usd": cost, "cost_text": money(cost)}


def save_usage(kind: str, repo: str, ref: str, usage: dict) -> None:
    line = {"at": f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}", "kind": kind, "repo": repo, "ref": ref,
            "model": MODEL, **usage}
    with (DATA / "usage.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


# ---------- GitHub ----------

def github(method: str, path: str, body: dict):
    if DRY_RUN:
        log(f"DRY_RUN {method} {path} {json.dumps(body, ensure_ascii=False)[:300]}")
        return
    req = urllib.request.Request(f"https://api.github.com{path}", json.dumps(body).encode(), method=method,
                                 headers={"Authorization": f"Bearer {GITHUB_TOKEN}",
                                          "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28"})
    for attempt in (1, 2):
        try:
            urllib.request.urlopen(req, timeout=30).read()
            return
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == 2:  # GitHub returns 5xx sporadically: retry once, then give up
                raise
            log(f"GitHub {e.code} on {method} {path}; retrying in 2s")
            time.sleep(2)


def status(repo: str, sha: str, state: str, description: str, context: str = CONTEXT, alvo: str | None = None):
    """O link "Details" do check na GitHub é o target_url; sem ele o status não abre nada."""
    corpo = {"state": state, "context": context, "description": description[:140]}
    link = alvo or relatorio_url(repo, sha)
    if link:
        corpo["target_url"] = link
    github("POST", f"/repos/{repo}/statuses/{sha}", corpo)


# ---------- git ----------

def git(*args, cwd=None):
    # token only in the process environment: it never reaches the command line or the mirror's config
    basic = base64.b64encode(f"x-access-token:{GITHUB_TOKEN}".encode()).decode()
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "1",
           "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
           "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}"}
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True, timeout=600)


def mirror(repo: str, clone_url: str) -> Path:
    target = DATA / "mirrors" / f"{repo.replace('/', '__')}.git"
    if target.exists():
        # the mirror uses +refs/*:refs/*, which also brings refs/pull/<n>/head (PR from a fork)
        git("fetch", "--prune", "origin", cwd=target)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        git("clone", "--mirror", clone_url, str(target))
    return target


# ---------- LLM ----------

def configure_ocr():
    """Register the BYOK endpoint as an ocr custom provider. The key lands in the ocr config file (mode 0600)."""
    for key, value in (("provider", OCR_PROVIDER),
                       (f"custom_providers.{OCR_PROVIDER}.url", LLM_BASE_URL),
                       (f"custom_providers.{OCR_PROVIDER}.protocol", "openai"),
                       (f"custom_providers.{OCR_PROVIDER}.api_key_cmd", f"echo {LLM_API_KEY}")):
        subprocess.run(["ocr", "config", "set", key, value], check=True, capture_output=True, text=True, timeout=60)


def llm(messages: list[dict], as_json: bool = True, usage: Usage | None = None):
    body = {"model": MODEL, "messages": messages, "temperature": 0}
    if as_json:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(f"{LLM_BASE_URL}/chat/completions", json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"})
    response = json.load(urllib.request.urlopen(req, timeout=900))
    if usage:
        usage.add_chat(response)
    text = response["choices"][0]["message"]["content"]
    return json.loads(text) if as_json else text


def verify_finding(worktree: Path, finding: dict, usage: Usage) -> dict:
    file = (worktree / finding["path"]).resolve()
    if not file.is_relative_to(worktree.resolve()) or not file.is_file():
        return {"verdict": "uncertain", "severity": "minor", "reason": "finding file not found in this commit"}
    lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
    first = max(1, (finding.get("start_line") or 1) - 30)
    last = min(len(lines), (finding.get("end_line") or first) + 30)
    excerpt = "\n".join(f"{n}: {lines[n - 1]}" for n in range(first, last + 1))
    request = (
        "You verify a code review finding. Read the excerpt and decide whether the described defect exists in THIS code.\n"
        'Answer with JSON only: {"verdict": "confirmed"|"refuted"|"uncertain", '
        '"severity": "critical"|"major"|"minor", "reason": "<up to 3 sentences, cite the line>"}.\n'
        "critical = data loss, security or a production break; major = real defect with an effect; minor = the rest.\n"
        "Use 'uncertain' if the excerpt is not enough to decide.\n\n"
        f"Finding ({finding['path']}:{finding.get('start_line')}-{finding.get('end_line')}):\n{finding['content']}\n\n"
        f"Excerpt of {finding['path']} (lines {first}-{last}):\n{excerpt}")
    try:
        return normalize_verification(llm([{"role": "user", "content": request}], usage=usage))
    except Exception as e:  # a failed verifier does not sink the review
        return {"verdict": "uncertain", "severity": "minor", "reason": f"verifier failed: {e}"[:300]}


# ---------- review ----------

def review_commit(repo: str, sha: str, clone_url: str):
    folder = DATA / "reports" / repo.replace("/", "__")
    if (folder / f"{sha}.json").exists():
        log(f"{repo}@{sha[:7]} already reviewed")
        return
    status(repo, sha, "pending", f"reviewing with {MODEL}")
    usage = Usage()
    with MIRROR_LOCK:
        root = mirror(repo, clone_url)
    with tempfile.TemporaryDirectory() as tmp:
        wt = Path(tmp) / "wt"
        git("worktree", "add", "--detach", str(wt), sha, cwd=root)
        try:
            out = Path(tmp) / "ocr.json"
            proc = subprocess.run(["ocr", "review", "--commit", sha, "--provider", OCR_PROVIDER, "--model", MODEL,
                                   "--format", "json", "--output", str(out), "--timeout", "60"],
                                  cwd=wt, capture_output=True, text=True, timeout=4200,
                                  env={**os.environ, "OCR_LLM_TIMEOUT": "900"})
            if proc.returncode != 0 or not out.exists():
                raise RuntimeError(f"ocr exited with {proc.returncode}: {proc.stderr[-500:]}")
            ocr = json.loads(out.read_text(encoding="utf-8-sig"))
            summary = ocr.get("summary") or {}
            usage.add(summary.get("input_tokens"), summary.get("cache_read_tokens"), summary.get("output_tokens"))
            findings = ocr.get("comments") or []
            for f in findings:
                f["verification"] = verify_finding(wt, f, usage)
        finally:
            git("worktree", "remove", "--force", str(wt), cwd=root)

    state, description = final_state(findings)
    report = usage.report(PRICE)
    text = markdown(sha, findings, report)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{sha}.json").write_text(json.dumps({"repo": repo, "sha": sha, "model": MODEL, "state": state,
                                                    "price": PRICE, "usage": report, "findings": findings,
                                                    "ocr": {k: ocr.get(k) for k in ("status", "summary", "warnings")}},
                                                   indent=2, ensure_ascii=False), encoding="utf-8")
    (folder / f"{sha}.md").write_text(text, encoding="utf-8")
    save_usage("commit", repo, sha, report)
    with (DATA / "reports" / "index.md").open("a", encoding="utf-8") as f:
        f.write(f"- {datetime.now(timezone.utc):%Y-%m-%d %H:%M} {repo}@{sha[:7]} {state} — {description} · "
                f"{report['total_tokens']:,} tok · {report['cost_text']}\n")
    if any(f["verification"]["verdict"] != "refuted" for f in findings):
        # the report and the status are the durable result: a failed comment must not turn a finished review into "error"
        try:
            github("POST", f"/repos/{repo}/commits/{sha}/comments", {"body": text})
        except Exception as e:
            log(f"{repo}@{sha[:7]} comment failed: {e}")
    status(repo, sha, state, description)
    log(f"{repo}@{sha[:7]}: {state} ({description}, {report['total_tokens']} tokens, {report['cost_text']})")


def review_pr(repo: str, number: int, head: str, base: str, clone_url: str):
    folder = DATA / "audits" / repo.replace("/", "__")
    name = f"pr-{number}-{head[:7]}"
    if (folder / f"{name}.json").exists():
        log(f"{repo}#{number}@{head[:7]} already audited")
        return
    status(repo, head, "pending", f"auditing with {MODEL}", CONTEXT_PR,
           alvo=relatorio_url(repo, name))
    usage = Usage()
    with MIRROR_LOCK:
        root = mirror(repo, clone_url)
    messages = lambda system, user: [{"role": "system", "content": system}, {"role": "user", "content": user}]
    result = audit.audit_pr(root, base, head,
                            call=lambda s, u: llm(messages(s, u), as_json=False, usage=usage),
                            call_json=lambda s, u: llm(messages(s, u), usage=usage))
    state, score = audit.audit_state(result["findings"])
    report = usage.report(PRICE)
    text = audit.markdown(repo, number, head, base, MODEL, result["auditors"], result["findings"],
                          result["synthesis"], result["truncated"], report)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.json").write_text(json.dumps({"repo": repo, "pr": number, "head": head, "base": base,
                                                     "model": MODEL, "state": state, "price": PRICE,
                                                     "usage": report, **result},
                                                    indent=2, ensure_ascii=False), encoding="utf-8")
    (folder / f"{name}.md").write_text(text, encoding="utf-8")
    save_usage("pr", repo, name, report)
    with (DATA / "reports" / "index.md").open("a", encoding="utf-8") as f:
        f.write(f"- {datetime.now(timezone.utc):%Y-%m-%d %H:%M} {repo}#{number}@{head[:7]} audit {state} — {score} · "
                f"{report['total_tokens']:,} tok · {report['cost_text']}\n")
    # the PR review body is capped at 65536 characters; the full report stays on the server
    body = text if len(text) < 60000 else text[:60000] + f"\n\n… truncated. Full report on the server: {name}.md"
    try:
        github("POST", f"/repos/{repo}/pulls/{number}/reviews", {"body": body, "event": "COMMENT", "commit_id": head})
    except Exception as e:
        log(f"{repo}#{number}@{head[:7]} PR review failed: {e}")
    status(repo, head, state, score, CONTEXT_PR, alvo=relatorio_url(repo, name))
    log(f"{repo}#{number}@{head[:7]}: audit {state} ({score}, {report['total_tokens']} tokens, "
        f"{report['cost_text']})")


def worker():
    while True:
        function, *args = QUEUE.get()
        repo, sha = args[0], args[2] if function is review_pr else args[1]
        context = CONTEXT_PR if function is review_pr else CONTEXT
        chave = f"{repo}@{sha[:7]}"
        inicio = time.monotonic()
        with ANDAMENTO_LOCK:
            ANDAMENTO[chave] = inicio
            EM_CURSO[chave] = (function, *args)
        salva_fila()
        log(f"{chave}: início (fila={QUEUE.qsize()}, workers={WORKERS})")
        try:
            function(*args)
        except Exception as e:
            log(f"{repo}@{sha[:7]} error: {e}")
            try:
                status(repo, sha, "error", f"reviewer failed: {e}", context)
            except Exception as e2:
                log(f"error status also failed: {e2}")
        finally:
            with ANDAMENTO_LOCK:
                ANDAMENTO.pop(chave, None)
                EM_CURSO.pop(chave, None)
            QUEUE.task_done()
            salva_fila()
            log(f"{chave}: fim em {time.monotonic() - inicio:.0f}s (fila={QUEUE.qsize()})")


# ---------- webhook ----------

class Webhook(BaseHTTPRequestHandler):
    def reply(self, code: int, text: str):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(text.encode())

    def html(self, code: int, corpo: str):
        dados = corpo.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)

    def relatorio(self):
        """Página do relatório: é o destino do link "Details" do status."""
        partes = self.path.split("?")[0].strip("/").split("/")
        if len(partes) != 4:
            return self.reply(404, "not found")
        _, dono, repo, nome = partes
        if not (re.fullmatch(r"[\w.-]+", dono) and re.fullmatch(r"[\w.-]+", repo)
                and re.fullmatch(r"[0-9a-f]{7,40}|pr-\d+-[0-9a-f]{7}", nome)):
            return self.reply(404, "not found")
        alvo = None
        for pasta in ("reports", "audits"):
            candidato = DATA / pasta / f"{dono}__{repo}" / f"{nome}.md"
            if candidato.is_file():
                alvo = candidato
                break
        titulo = f"{dono}/{repo}@{nome[:7]}"
        if alvo is None:
            return self.html(200, render.pagina(titulo, f"## Sem relatório ainda\n\n"
                                                     f"Revisão em andamento para `{dono}/{repo}@{nome}`."))
        resumo = None
        json_alvo = alvo.with_suffix(".json")
        if json_alvo.is_file():
            try:
                resumo = json.loads(json_alvo.read_text(encoding="utf-8"))
            except Exception:
                resumo = None
        commit_url = f"https://github.com/{dono}/{repo}/commit/{nome}" if re.fullmatch(r"[0-9a-f]{40}", nome) else None
        return self.html(200, render.pagina(titulo, alvo.read_text(encoding="utf-8"), resumo, commit_url))

    def do_GET(self):
        if self.path.startswith("/reports/"):
            return self.relatorio()
        if self.path != "/status":
            return self.reply(404, "not found")
        corpo = json.dumps(estado(), ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def do_POST(self):
        size = int(self.headers.get("Content-Length") or 0)
        if self.path != "/webhook" or size > MAX_BODY:
            return self.reply(404, "not found")
        body = self.rfile.read(size)
        if not signature_valid(body, self.headers.get("X-Hub-Signature-256")):
            return self.reply(401, "invalid signature")
        event = self.headers.get("X-GitHub-Event")
        if event == "ping":
            return self.reply(200, "pong")
        if event == "pull_request":
            pr = pr_to_audit(json.loads(body))
            if pr:
                enfileira(review_pr, *pr)
                log(f"PR #{pr[1]} queued (queue={QUEUE.qsize()})")
            return self.reply(202, "PR queued" if pr else "PR ignored")
        if event != "push":
            return self.reply(202, f"event {event} ignored")
        new = push_commits(json.loads(body))
        for item in new:
            enfileira(review_commit, *item)
        log(f"push: {len(new)} commit(s) queued (queue={QUEUE.qsize()})")
        self.reply(202, f"{len(new)} commit(s) queued")

    def log_message(self, *args):  # no default access log
        pass


if __name__ == "__main__":
    (DATA / "reports").mkdir(parents=True, exist_ok=True)
    PRICE = load_price()
    configure_ocr()
    retomados = carrega_fila()
    for _ in range(WORKERS):
        threading.Thread(target=worker, daemon=True).start()
    log(f"listening on :{PORT}/webhook and GET /status; repos={sorted(REPOS)}; model={MODEL} at {LLM_BASE_URL}; "
        f"price={PRICE}; gate={GATE}; dry_run={DRY_RUN}; workers={WORKERS}; retomados={retomados}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Webhook).serve_forever()
