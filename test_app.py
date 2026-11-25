import hashlib, hmac, os
os.environ.update(WEBHOOK_SECRET="secret", GITHUB_TOKEN="t", LLM_API_KEY="k", REPOS="Org/Repo")
import app

SHA = "a" * 40
PRICE = {"input": 0.15, "output": 0.6, "cache_read": 0.003}

def test_signature():
    body = b'{"x":1}'
    good = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert app.signature_valid(body, good)
    assert not app.signature_valid(body, "sha256=" + "0" * 64)
    assert not app.signature_valid(body, None)
    assert not app.signature_valid(body + b" ", good)

def test_push_commits():
    ev = {"repository": {"full_name": "org/repo", "clone_url": "u"}, "commits": [{"id": SHA}, {"id": "not-a-sha"}]}
    assert app.push_commits(ev) == [("org/repo", SHA, "u")]
    assert app.push_commits({**ev, "repository": {"full_name": "other/repo", "clone_url": "u"}}) == []
    assert app.push_commits({**ev, "deleted": True}) == []

def finding(verdict, severity):
    return {"path": "a.py", "content": "x",
            "verification": {"verdict": verdict, "severity": severity, "reason": ""}}

def test_final_state_gate_major():
    assert app.final_state([])[0] == "success"
    assert app.final_state([finding("confirmed", "minor")])[0] == "success"
    assert app.final_state([finding("confirmed", "major")])[0] == "failure"
    assert app.final_state([finding("refuted", "critical"), finding("uncertain", "critical")])[0] == "success"

def test_normalize_verification():
    assert app.normalize_verification({"verdict": "maybe", "severity": "huge"}) == \
        {"verdict": "uncertain", "severity": "minor", "reason": ""}

def usage(total=0, cached=0, cost=None, cost_text="n/a (no price)"):
    return {"input_tokens": 0, "cached_input_tokens": cached, "output_tokens": 0,
            "total_tokens": total, "cost_usd": cost, "cost_text": cost_text}

def test_markdown_hides_refuted():
    md = app.markdown(SHA, [finding("refuted", "major"), {**finding("confirmed", "major"), "content": "VISIBLE"}],
                      usage())
    assert "VISIBLE" in md and md.count("### ") == 1
    assert "$" not in md and "no price" in md

def test_cost_none_without_price():
    assert app.cost_usd(1000, 0, 500, None) is None
    assert app.cost_usd(1000, 0, 500, {"input": 1}) is None

def test_cost_uses_cache_price_and_clamps_cached():
    # (600*0.15 + 400*0.003 + 500*0.6) / 1e6
    assert app.cost_usd(1000, 400, 500, PRICE) == 0.000391
    # a provider that reports more cached tokens than input must not subtract twice
    assert app.cost_usd(1000, 5000, 0, PRICE) == round((1000 * 0.003) / 1e6, 6)

def test_pick_price():
    catalog = {"deepseek": {"models": {"deepseek-flash": {"cost": {"input": 0.15, "output": 0.6, "cache_read": 0.003}}}},
               "gateway": {"models": {"other": {"cost": {"input": 1, "output": 2},
                                                "canonical_model_id": "x/deepseek-flash"}}}}
    assert app.pick_price(catalog, "deepseek", "deepseek-flash") == PRICE
    assert app.pick_price(catalog, "unknown", "deepseek-flash") == PRICE
    assert app.pick_price(catalog, "unknown", "nothing") is None

def test_usage_counts_cache_hits_from_both_shapes():
    u = app.Usage()
    u.add_chat({"usage": {"prompt_tokens": 100, "completion_tokens": 10, "prompt_cache_hit_tokens": 40}})
    u.add_chat({"usage": {"prompt_tokens": 50, "completion_tokens": 5,
                          "prompt_tokens_details": {"cached_tokens": 20}}})
    report = u.report(PRICE)
    assert (report["input_tokens"], report["cached_input_tokens"], report["output_tokens"]) == (150, 60, 15)
    assert report["total_tokens"] == 165
    assert report["cost_usd"] == app.cost_usd(150, 60, 15, PRICE)

def test_money_keeps_resolution_on_cheap_reviews():
    assert app.money(None) == "n/a (no price)"
    assert app.money(0.000939) == "$0.000939"
    assert app.money(1.2345) == "$1.2345"

import audit

def test_cited_files():
    assert audit.cited_files("`app/main.py:10`, stress/runner.py:3-5; app/main.py:12") == ["app/main.py", "stress/runner.py"]
    assert audit.cited_files("section 2") == []

def with_verdict(severity, verdict):
    return {"severity": severity, "verification": {"verdict": verdict}}

def test_audit_state():
    assert audit.audit_state([with_verdict("Major", "confirmed")])[0] == "success"
    assert audit.audit_state([with_verdict("Blocker", "refuted")])[0] == "success"
    state, score = audit.audit_state([with_verdict("Blocker", "confirmed"), with_verdict("Minor", "not verified")])
    assert state == "failure" and "1 not verified" in score

def test_normalize_findings_order_and_unknown_severity():
    s = {"findings": [{"severity": "Minor"}, {"severity": "Critical?"}, {"severity": "Blocker"}]}
    assert [f["severity"] for f in audit.normalize_findings(s)] == ["Blocker", "Major", "Minor"]

def test_audit_markdown_shows_cost():
    findings = [{**with_verdict("Major", "confirmed"), "id": "A1", "title": "t", "where": "a.py:1",
                 "found_by": ["auditor-correctness"], "state": "demonstrated", "finding": "f", "evidence": "e",
                 "verification": {"verdict": "confirmed", "method": "m", "evidence": "e"}}]
    md = audit.markdown("org/repo", 7, SHA, "b" * 40, "deepseek-flash", {"auditor-contract": "ran"},
                        findings, {"paraphrases": "p"}, False,
                        usage(total=1200, cached=300, cost=0.0005, cost_text="$0.0005"))
    assert "Tokens: 1,200 (300 cached)" in md and "$0.0005" in md and "contract (ran)" in md
