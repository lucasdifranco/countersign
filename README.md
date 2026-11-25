# revisor-commits

Automated code review for every push, running on **your own LLM key** — any OpenAI-compatible endpoint — and
recording the **token and dollar cost of every review**.

Two jobs in one webhook service:

- **commit review** — every commit in a push to a configured repository goes through
  [open-code-review](https://github.com/alibaba/open-code-review) (`ocr`), then every finding is verified by a
  second model call before it is published (the same idea as CodeRabbit's "unverified finding").
- **PR audit** — every pull request opened or updated against a base branch goes through the `/audit` method
  (Elephant-Goldfish): three isolated auditors, a synthesis, and one verifier per finding.

```
GitHub push ──webhook (HMAC)──> revisor-commits (Docker)
                                 ├─ status "pending" on the commit
                                 ├─ git fetch (bare mirror) + worktree at the commit
                                 ├─ ocr review --commit <sha>            (your model)
                                 ├─ verification of each finding        (2nd call)
                                 ├─ report: data/reports/<owner>__<repo>/<sha>.{json,md}
                                 ├─ usage: data/usage.jsonl + cost in the report and in the comment
                                 ├─ commit comment (findings not refuted)
                                 └─ final status: failure on a confirmed finding at or above GATE
```

One commit at a time, in arrival order. A commit already reviewed (same SHA) is skipped.

## Bring your own key

Three variables are the whole LLM configuration:

```dotenv
LLM_BASE_URL=https://api.deepseek.com/v1   # any OpenAI-compatible base URL
LLM_API_KEY=...
LLM_MODEL=deepseek-flash
```

At startup the service registers that endpoint in `ocr` as a custom provider named `byok` (protocol `openai`).
Nothing else in the code knows which vendor is behind it: swapping to OpenAI, OpenRouter, Groq, vLLM or an internal
gateway is a `.env` change, not a code change.

`LLM_PROVIDER` (default `deepseek`) is a [models.dev](https://models.dev) provider id. It is used only to look up
the price of `LLM_MODEL`; it does not affect the API calls.

**Where the key lives:** `ocr` can only read a custom provider key from its own config file, so the key is written
to `~/.opencodereview/config.json` inside the container (mode 0600, root) on every start. The verifier calls use it
from the environment only.

## Cost per review

Every call's usage is counted, not estimated:

| stage | source of the tokens |
|---|---|
| `ocr` review | `summary.input_tokens`, `output_tokens`, `cache_read_tokens` of the `ocr --format json` output |
| finding verification | `usage` of each chat completion (prompt, completion, cache hits) |
| PR audit | every auditor, synthesis and verifier call |

Price per million tokens comes from the models.dev catalog — the same catalog opencode reads — matched on
`LLM_PROVIDER`/`LLM_MODEL`, and cached in `data/prices.json`. `PRICE_INPUT`, `PRICE_OUTPUT` and `PRICE_CACHE_READ`
(in USD per million tokens) override it; with no price the review still runs and cost is reported as `n/a`.

Cache reads are priced separately when the catalog has a cache price, because they dominate a review: on a
single-file review measured on 2026-11-25, 14,464 of 17,170 input tokens were cache hits.

Cost is written in three places:

- `data/usage.jsonl` — one JSON line per review: date, kind (`commit`/`pr`), repo, ref, model, tokens, `cost_usd`;
- the report (`<sha>.json` keeps `price` and `usage`, so the number can be recomputed later);
- the GitHub comment and `data/reports/index.md` (`Tokens: 17,692 (14,464 cached) · Cost: $0.000762`).

Total spend over a period:

```bash
jq -s 'map(.cost_usd // 0) | add' data/usage.jsonl
```

## PR audit (method /audit)

For a PR opened, updated or reopened against `AUDIT_BASE` (default `main`), not a draft:

```
target = diff merge-base(base)..head + reading list (changed files + up to 8 files that mention a changed one)
   ├─ 3 isolated auditors in parallel: comprehension, correctness, contract (no PR title, no description)
   ├─ synthesis: merges, never creates a finding or changes a severity -> A1..An (JSON)
   ├─ 1 isolated verifier per finding (reading only, a different path from the auditor)
   └─ PR review + status "revisor-commits/audit" (failure only with a confirmed Blocker)
      + data/audits/<repo>/pr-<n>-<sha>.{json,md}
```

Differences from the skill, all by design:

- **One model in every role.** Contexts are isolated, the model's bias is not.
- **The verifier only reads.** It runs no code: the skill's verification is stronger, this server executes nothing.
  On a synthetic PR the verifier confirmed 7 of 7 findings, including one the auditor had marked "hypothesis".
- **No author reply and no re-audit.** Each `synchronize` (new push on the PR) produces a new audit of the head.
- The reading list is built by filename search (`git grep`), not by a human. Above 300k characters it is
  truncated and the report says so.

Prompts: copy of the skill's `auditor-*.md`, under Apache-2.0 (see `prompts/NOTICE.md`).

## Install

1. Copy this directory to the server and create `.env` from `.env.example`.
2. `docker compose up -d --build`
3. Expose `http://<host>:8080/webhook` on the internet: GitHub has to reach the URL. Options: a tunnel
   (Cloudflare Tunnel, ngrok) or a router port with HTTPS in front. Only `/webhook` answers, and only requests
   with a valid signature.
4. On GitHub, in each repository (Settings → Webhooks → Add webhook):
   - Payload URL: `https://<public address>/webhook`
   - Content type: `application/json`
   - Secret: the same `WEBHOOK_SECRET`
   - Events: "Let me select individual events" → **Pushes** and **Pull requests**
5. Add the repository to `REPOS` (`owner/repo`, comma separated) and restart (`docker compose up -d`).
6. Test: in Webhooks → Recent Deliveries the `ping` must return 200. After a push, `docker compose logs -f`.

GitHub token (`GITHUB_TOKEN`), fine-grained, restricted to the `REPOS` repositories:

- Contents: read (clone);
- Commit statuses: read and write (status check);
- Pull requests: read and write (audit review on the PR).

The commit comment also needs write permission. Check the GitHub documentation for which permission
`POST /repos/{owner}/{repo}/commits/{sha}/comments` requires from fine-grained tokens (not verified here).

## Test without touching GitHub

`DRY_RUN=1` in `.env`: status and comment go to the log only; the report is still written.
Unit tests: `python -m pytest -q test_app.py`.

## Known limits

- **The code goes to your LLM provider.** Each review sends the diff and the files the model asks for to the API in
  `LLM_BASE_URL`. Retention, training use and processing country are the provider's policy: check them before
  enabling this on a confidential repository.
- The verifier is the same model that found the defect, so it is not independent. It reduces false positives; it
  does not replace human review.
- GitHub sends at most 20 commits per push event. In a larger push, only those 20 are reviewed.
- A force push that rewrites history: the new SHAs are reviewed; the old ones stay in the reports.
- If the container restarts, queued commits are lost and keep the status "pending". Redeliver the event from
  GitHub (Recent Deliveries → Redeliver).
- Measured with `deepseek-flash` on 2026-11-25, one run each:
  - single-file synthetic commit: 34 s, 33.7k tokens;
  - single-file synthetic commit through this code path (ocr call + verification + price lookup): 17.7k tokens,
    $0.000762;
  - branch with 7 files: 11 min 44 s, 1.83M tokens;
  - synthetic PR (1 changed file + 1 boundary file): 102 s.

## License

MIT for this repository. The prompts under `prompts/` are adapted from
[google/elephant-goldfish-model](https://github.com/google/elephant-goldfish-model) and keep Apache-2.0; see
`prompts/NOTICE.md`.
