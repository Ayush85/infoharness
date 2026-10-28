import asyncio
import json

import httpx
import pytest

from app import config, llm
from app.analysis import (Chunk, PageDoc, build_chunks, check_staleness, check_thin_pages, extract_facts, numeric_conflicts)
from app.extract import Block, extract
from app.security import UnsafeURL, check_url_safe, normalize_input_url


def test_normalize_input_url():
    assert normalize_input_url("docs.example.com/help") == "https://docs.example.com/help"
    assert normalize_input_url("HTTP://Example.com") == "http://example.com/"
    for bad in ["", "ftp://x.com", "https://user:pw@x.com/"]:
        with pytest.raises(UnsafeURL):
            normalize_input_url(bad)


def test_ssrf_blocked(monkeypatch):
    monkeypatch.setenv("ALLOW_PRIVATE_HOSTS", "false")
    config.reload_settings()
    for url in ["http://127.0.0.1/", "http://localhost/", "http://10.0.0.5/", "http://169.254.169.254/latest",
                "http://[::1]/", "http://example.com:6379/"]:
        with pytest.raises(UnsafeURL):
            check_url_safe(url)


def test_extract_drops_chrome_keeps_headings():
    html = """<html><head><title>T</title></head><body><nav><a href="/a">Nav</a></nav>
    <main><h2>Billing</h2><p>Pay by card.</p><div class="sidebar">Ignore me</div><ul><li>One item here</li></ul></main>
    <footer>Footer text</footer></body></html>"""
    ex = extract(html, "https://x.com/docs/")
    texts = [b.text for b in ex.blocks]
    assert "Pay by card." in texts and "One item here" in texts
    assert not any("Footer" in t or "Ignore" in t or "Nav" in t for t in texts)
    assert ex.blocks[0].heading == "Billing"
    assert "https://x.com/a" in ex.links


def _chunks(*texts):
    return [Chunk(page=i, heading="h", text=t) for i, t in enumerate(texts)]


def test_numeric_conflict_found():
    facts = extract_facts(_chunks(
        "You can request a full refund within 14 days of purchase for any annual subscription.",
        "You can request a full refund within 30 days of purchase for any annual subscription."))
    conflicts = numeric_conflicts(facts)
    assert len(conflicts) == 1
    a, b, _ = conflicts[0]
    assert {a.value, b.value} == {14.0, 30.0}


def test_numeric_conflict_respects_plan_qualifiers():
    facts = extract_facts(_chunks(
        "The free plan includes up to 5 users in a single shared workspace.",
        "The business plan includes up to 50 users in a single shared workspace."))
    assert numeric_conflicts(facts) == []


def test_staleness_declared_year():
    p = PageDoc(url="u", title="t", blocks=[Block("Last updated: March 2019. Old guide content here.", "t", "text")],
                word_count=8)
    f = check_staleness([p])
    assert f and f[0].type == "stale" and f[0].extra["year"] == 2019


def test_boilerplate_excluded_from_chunks():
    foot = Block("Was this article helpful? Tell us.", "h", "text")
    pages = [PageDoc(url=str(i), title="t", blocks=[Block(f"Unique content number {i} for page.", "h", "text"), foot],
                     word_count=10) for i in range(5)]
    chunks = build_chunks(pages)
    assert all("helpful" not in c.text for c in chunks)


def test_llm_quote_verification(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    config.reload_settings()
    a = {"url": "a", "heading": "h", "text": "Refunds are possible within 14 days of purchase."}
    b = {"url": "b", "heading": "h", "text": "Refunds are possible within 30 days of purchase."}

    def handler_factory(claim_b):
        def handler(request: httpx.Request):
            assert request.url.path == "/v1/messages"
            body = {"contradiction": True, "confidence": 0.9, "claim_a": a["text"], "claim_b": claim_b,
                    "explanation": "Refund window differs"}
            return httpx.Response(200, json={"content": [{"type": "text", "text": json.dumps(body)}]})
        return handler

    async def run(claim_b):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler_factory(claim_b))) as c:
            return await llm.judge(c, a, b)

    ok = asyncio.run(run(b["text"]))
    assert ok and ok.contradiction
    hallucinated = asyncio.run(run("Refunds are possible within 90 days of purchase."))
    assert hallucinated is None


def test_thin_page_ignores_generated_and_code_pages():
    code = Block(" ".join(["x = foo(bar)"] * 40), "t", "code")
    def pg(url, blocks):
        return PageDoc(url=url, title="t", blocks=blocks, word_count=0, link_count=0)
    pages = [
        pg("https://d.io/en/latest/_modules/pkg/api", []),              # generated source listing
        pg("https://d.io/en/latest/genindex", []),                       # generated index
        pg("https://d.io/en/latest/examples", [code]),                   # code-only reference page
        pg("https://d.io/en/latest/roadmap", [Block("Coming soon.", "t", "text")]),  # real placeholder
    ]
    flagged = [f.page_a for f in check_thin_pages(pages)]
    assert flagged == ["https://d.io/en/latest/roadmap"]


def test_bot_hostile_hosts_and_admin_links():
    from app.crawler import is_bot_hostile
    from app.analysis import admin_target_kind, check_orphans
    assert is_bot_hostile("www.google.com") and is_bot_hostile("maps.google.co.uk") and is_bot_hostile("l.facebook.com")
    assert not is_bot_hostile("docs.example.com") and not is_bot_hostile("notgoogle.org")
    assert admin_target_kind("https://x.com/wp-admin/post.php?post=1&action=edit", "x.com")
    assert admin_target_kind("https://staging.x.com/a", "x.com")
    assert admin_target_kind("https://x.com/docs/a", "x.com") is None
    assert check_orphans([], "https://x.com/", partial=True) == []


def test_benchmark_false_positive_guards():
    from datetime import datetime, timezone
    from app.analysis import (PageDoc, Block, is_historical, same_family, check_staleness, check_thin_pages,
                              extract_facts, build_chunks, numeric_conflicts, VERSION_RE)
    assert is_historical("https://x.dev/doc/go1.12") and is_historical("https://x.com/blog/2021/post") \
        and is_historical("https://x.com/docs/changelog/migration_10.html") and not is_historical("https://x.com/docs/refunds")
    assert same_family("https://x.com/a/b?platform=mac", "https://x.com/a/b") and same_family("https://x.dev/go1.7", "https://x.dev/go1.19")
    assert not same_family("https://x.com/docs/refunds", "https://x.com/docs/payments-faq")
    assert not VERSION_RE.search("get list v1/Node : get") and VERSION_RE.search("the SDK v5 supports") 

    def page(url, title, text):
        return PageDoc(url=url, title=title, blocks=[Block("text", text, "")] if False else [], word_count=len(text.split()))
    old = PageDoc(url="https://x.com/changelog/old", title="Old", blocks=[Block(kind="text", text="Document last updated: June 9, 2015 and more words here to fill the page out nicely.", heading="")], word_count=20)
    assert check_staleness([old]) == []
    txt = PageDoc(url="https://x.com/docs/page/llms.txt", title="t", blocks=[Block(kind="text", text="{count}", heading="")], word_count=1)
    assert check_thin_pages([txt]) == []


def test_fix_steps_cover_every_finding_type():
    from app.analysis import Finding
    from app.suggest import fix_steps
    for t, ex in [("contradiction", {"values": [14, 30]}), ("duplicate_page", {"pages": ["https://x.com/a", "https://x.com/a-copy"]}),
                  ("duplicate_section", {}), ("stale", {"method": "version_drift", "latest": 5}), ("stale", {}),
                  ("broken_link", {"internal": True, "site_wide": True, "sources_total": 40}), ("broken_link", {"internal": False}),
                  ("admin_link", {}), ("thin_page", {}), ("orphan_page", {})]:
        steps = fix_steps(Finding(type=t, severity="low", title="x", page_a="https://x.com/a", page_b="https://x.com/b", extra=ex))
        assert 1 <= len(steps) <= 5 and all(isinstance(s, str) and s for s in steps), t
    assert "once in the template" in fix_steps(Finding(type="broken_link", severity="low", title="x", extra={"internal": True, "site_wide": True, "sources_total": 40}))[0]
