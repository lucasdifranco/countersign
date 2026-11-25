"""
PR audit for the base branch: the /audit method (Elephant-Goldfish), run server-side on the configured model.

  1. target = diff merge-base(base, head)..head; reading list = changed files (head version) + boundaries
     (head files that mention the name of a changed file). Nothing from the author is used: not the PR title
     nor its description.
  2. three auditors (comprehension, correctness, contract), each in an isolated call that only sees target + reading list;
  3. synthesis receives the raw reports and returns findings A1..An as JSON;
  4. one verifier per finding, isolated, without the auditor's reasoning beyond the finding text;
  5. report with a score; status failure if a Blocker is confirmed.

Differences from the skill (see README): no tools and no code execution (reading-only verification), the same model in
every role, no author reply and no re-audit.
"""
import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PROMPTS = Path(__file__).parent / "prompts"
AUDITORS = ["auditor-comprehension", "auditor-correctness", "auditor-contract"]
ORDER = {"Blocker": 0, "Major": 1, "Minor": 2}

ADAPTATION = """
## Server adaptation (overrides any tool rule)
You have NO tools and run no command. The target and the reading list are complete in this prompt.
Where the rules mention `git show`, `git diff`, `Bash` or a command, use only the text you received.
Possible evidence: a quoted excerpt (with file:line) or a mental run with a concrete value.
A file that is not in the prompt is "not verified".
"""

SYNTHESIS_JSON = """
## Format (server)
Answer with ONE JSON object only:
{"auditor_status": [{"auditor": "...", "state": "ran|did not run", "findings": <n>}],
 "findings": [{"id": "A1", "title": "...", "severity": "Blocker|Major|Minor", "where": "file:line",
               "found_by": ["auditor-..."], "state": "demonstrated|hypothesis", "finding": "...", "evidence": "...",
               "divergence": "..."}],
 "paraphrases": "<literal paraphrases from the comprehension auditor>"}
"""

VERIFIER_JSON = """
## Format (server)
Without execution: take a path the auditor did not take, for example reading the definition of the called function or
following who calls and who reads the data in the files received. Answer with ONE JSON object only:
{"verdict": "confirmed|refuted|inconclusive", "method": "...", "evidence": "...",
 "killing_line": "...", "missing": "...", "adjustment": "...", "out_of_scope": "...", "not_verified": "..."}
"""


def prompt(name: str, extra: str = "") -> str:
    text = (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    body = text.split("---", 2)[2] if text.startswith("---") else text  # drop the frontmatter
    return body + ADAPTATION + extra


# ---------- pure functions (tested in test_app.py) ----------

def cited_files(where: str) -> list[str]:
    """'a/b.py:10, c.py:3-5' -> ['a/b.py', 'c.py'] (only paths with an extension)."""
    return list(dict.fromkeys(re.findall(r"([\w./-]+\.\w+)(?::\d+)", where or "")))


def audit_state(findings: list[dict]) -> tuple[str, str]:
    blockers = [f for f in findings if f["severity"] == "Blocker" and f["verification"]["verdict"] == "confirmed"]
    score = {v: sum(f["verification"]["verdict"] == v for f in findings)
             for v in ("confirmed", "refuted", "inconclusive", "not verified")}
    text = (f"{score['confirmed']} confirmed, {score['refuted']} refuted, "
            f"{score['inconclusive']} inconclusive, {score['not verified']} not verified")
    return ("failure" if blockers else "success"), text


def normalize_findings(synthesis: dict) -> list[dict]:
    findings = []
    for i, f in enumerate(synthesis.get("findings") or [], 1):
        severity = f.get("severity") if f.get("severity") in ORDER else "Major"  # when in doubt, do not downgrade
        findings.append({**f, "id": f"A{i}", "severity": severity})
    return sorted(findings, key=lambda f: ORDER[f["severity"]])


def markdown(repo: str, number: int, head: str, base: str, model: str, auditors: dict,
             findings: list[dict], synthesis: dict, truncated: bool, usage: dict) -> str:
    state, score = audit_state(findings)
    cost = usage["cost_text"]
    l = [f"## Audit (method /audit) — PR #{number} @ `{head[:7]}` ({model})", "",
         "> Automated audit by context-free agents. Not a definitive verdict and no replacement for human review.",
         "> Differences from the skill: one model in every role; reading-only verification, no code execution.", "",
         f"Target: diff merge-base(`{base[:7]}`)..`{head[:7]}`." + (" **Reading list truncated by size.**" if truncated else ""),
         "Auditors: " + ", ".join(f"{n.replace('auditor-', '')} ({'ran' if t else 'did not run'})"
                                  for n, t in auditors.items()) + ".",
         f"Score: {score}. Status: **{state}** (failure only with a confirmed Blocker).",
         f"Tokens: {usage['total_tokens']:,} ({usage['cached_input_tokens']:,} cached) · Cost: {cost}", ""]
    for f in findings:
        v = f["verification"]
        l += [f"### {f['id']}. {f.get('title', '')} — {f['severity']} — {v['verdict']}",
              f"- Where: `{f.get('where', '')}` · found by: {', '.join(f.get('found_by') or [])} · {f.get('state', '')}",
              f"- Finding: {f.get('finding', '')}", f"- Auditor evidence: {f.get('evidence', '')}",
              f"- Verifier ({v.get('method', '')}): {v.get('evidence', '')}"]
        if v["verdict"] == "refuted" and v.get("killing_line"):
            l.append(f"- Killing line: {v['killing_line']}")
        if v.get("adjustment") and v["adjustment"] != "none":
            l.append(f"- Verifier adjustment: {v['adjustment']}")
        l.append("")
    if not findings:
        l += ["No findings.", ""]
    if synthesis.get("paraphrases"):
        l += ["<details><summary>Paraphrases (comprehension auditor)</summary>", "", str(synthesis["paraphrases"]), "", "</details>"]
    return "\n".join(l)


# ---------- target collection ----------

def git_out(root: Path, *args) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300).stdout


def build_target(root: Path, base: str, head: str, limit: int, max_boundaries: int) -> tuple[str, str, dict, bool]:
    """(diff, reading list, head files by path, truncated?)."""
    mb = git_out(root, "merge-base", base, head).strip()
    diff = git_out(root, "diff", mb, head)
    changed = [p for p in git_out(root, "diff", "--name-only", "--diff-filter=d", mb, head).splitlines() if p]
    files = {p: git_out(root, "show", f"{head}:{p}") for p in changed}
    boundaries: list[str] = []
    for p in changed:
        name = Path(p).stem
        if len(name) < 4:  # a short name matches everything
            continue
        r = subprocess.run(["git", "grep", "-l", "-F", name, head], cwd=root, capture_output=True, text=True)
        for line in r.stdout.splitlines():
            path = line.split(":", 1)[1]
            if path not in changed and path not in boundaries:
                boundaries.append(path)
    for p in boundaries[:max_boundaries]:
        files[p] = git_out(root, "show", f"{head}:{p}")
    parts, total, truncated = [], len(diff), False
    for p, content in files.items():
        kind = "changed (head version)" if p in changed else "boundary (mentions a changed file)"
        block = f"\n### {p} — {kind}\n```\n" + "\n".join(f"{i}: {t}" for i, t in enumerate(content.splitlines(), 1)) + "\n```\n"
        if total + len(block) > limit:
            truncated = True
            continue
        parts.append(block)
        total += len(block)
    return diff, "".join(parts), files, truncated


# ---------- execution ----------

def with_retry(fn, *args):
    """The skill re-runs a failing role once; on the second failure it returns None."""
    for _ in range(2):
        try:
            return fn(*args)
        except Exception:
            continue
    return None


def audit_pr(root: Path, base: str, head: str, call, call_json, limit: int = 300_000, max_boundaries: int = 8):
    """call(system, user) -> text; call_json(system, user) -> dict."""
    diff, reading, files, truncated = build_target(root, base, head, limit, max_boundaries)
    target = f"## Target: diff\n```diff\n{diff}\n```\n\n## Reading list (head files, with line numbers)\n{reading}"

    with ThreadPoolExecutor(3) as ex:
        reports = dict(zip(AUDITORS, ex.map(lambda n: with_retry(call, prompt(n), target), AUDITORS)))

    raw = "\n\n".join(f"=== {n} — state: {'ran' if t else 'did not run'} ===\n{t or ''}" for n, t in reports.items())
    synthesis = with_retry(call_json, prompt("auditor-synthesis", SYNTHESIS_JSON), raw) or {}
    findings = normalize_findings(synthesis)

    def verify(f: dict) -> dict:
        cited = {p: files[p] for p in cited_files(f.get("where", "")) if p in files}
        context = "".join(f"\n### {p}\n```\n" + "\n".join(f"{i}: {t}" for i, t in enumerate(c.splitlines(), 1)) + "\n```\n"
                          for p, c in cited.items())
        request = (f"Finding: {f['severity']} — {f.get('title', '')}\n- Where: {f.get('where', '')}\n"
                   f"- Finding: {f.get('finding', '')}\n- Auditor evidence: {f.get('evidence', '')} ({f.get('state', '')})\n\n"
                   f"## Target diff\n```diff\n{diff}\n```\n\n## Cited files (head)\n{context or '(no target file cited)'}\n\n"
                   f"## Reading list\n{reading}")
        v = with_retry(call_json, prompt("auditor-verifier", VERIFIER_JSON), request)
        if v is None:
            return {"verdict": "not verified", "method": "verifier failed twice", "evidence": ""}
        if v.get("verdict") not in ("confirmed", "refuted", "inconclusive"):
            v["verdict"] = "inconclusive"
        return v

    with ThreadPoolExecutor(6) as ex:
        for f, v in zip(findings, ex.map(verify, findings)):
            f["verification"] = v
    return {"auditors": reports, "synthesis": synthesis, "findings": findings, "truncated": truncated}
