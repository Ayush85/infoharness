"""Full flow: API creates a scan -> worker runs it against a local fake docs site -> report renders."""
from fastapi.testclient import TestClient

from app import worker
from app.main import app


def test_full_scan(env, docs_site):
    client = TestClient(app)
    r = client.post("/api/scans", json={"url": f"{docs_site}/docs/"})
    assert r.status_code == 201, r.text
    scan_id = r.json()["id"]

    # Same URL again within the cache window returns the same scan (no re-crawl, no quota used)
    again = client.post("/api/scans", json={"url": f"{docs_site}/docs"})
    assert again.json()["id"] == scan_id and again.json()["cached"] is True

    assert worker.run_once() is True
    data = client.get(f"/api/scans/{scan_id}").json()
    assert data["status"] == "done", data
    types = {}
    for i in data["issues"]:
        types.setdefault(i["type"], []).append(i)

    # Known planted problems
    contra = types["contradiction"][0]
    assert "14" in contra["title"] and "30" in contra["title"]
    assert {contra["page_a"].rsplit("/", 1)[1], contra["page_b"].rsplit("/", 1)[1]} == {"refunds.html", "payments-faq.html"}
    dup = types["duplicate_page"][0]
    assert {p.rsplit("/", 1)[1] for p in dup["extra"]["pages"]} == {"setup.html", "setup-copy.html"}
    assert any("old-sdk" in i["page_a"] for i in types["stale"])
    assert any(i["page_b"].endswith("missing.html") for i in types["broken_link"])
    assert any(i["page_a"].endswith("thin.html") for i in types["thin_page"])
    assert any(i["page_a"].endswith("orphan.html") for i in types["orphan_page"])
    # Boilerplate footer must not produce duplicate-section noise
    assert not any("helpful" in (i["quote_a"] or "") for i in types.get("duplicate_section", []))
    assert 0 < data["score"] < 100 and data["grade"] in "ABCDF"

    html = client.get(f"/r/{scan_id}").text
    assert "Contradictions" in html and "14 days" in html
    badge = client.get(f"/badge/{scan_id}.svg")
    assert badge.status_code == 200 and "docs health" in badge.text


def test_rate_limit_and_validation(env, docs_site):
    client = TestClient(app)
    assert client.post("/api/scans", json={"url": "ftp://nope"}).status_code == 400
    codes = [client.post("/api/scans", json={"url": f"{docs_site}/docs/p{i}.html", "fresh": True}).status_code
             for i in range(4)]
    assert codes[:3] == [201, 201, 201] and codes[3] == 429


def test_unreachable_site_fails_cleanly(env):
    client = TestClient(app)
    r = client.post("/api/scans", json={"url": "http://127.0.0.1:9/docs/"})
    scan_id = r.json()["id"]
    worker.run_once()
    data = client.get(f"/api/scans/{scan_id}").json()
    assert data["status"] == "failed" and "connect" in data["error"]
    assert client.get(f"/r/{scan_id}").status_code == 200
    assert client.get("/r/doesnotexist").status_code == 404


def test_vanity_domain_redirect_is_followed(env, docs_site, redirect_site):
    client = TestClient(app)
    scan_id = client.post("/api/scans", json={"url": f"{redirect_site}/docs/"}).json()["id"]
    worker.run_once()
    data = client.get(f"/api/scans/{scan_id}").json()
    assert data["status"] == "done", data
    assert data["pages_crawled"] >= 8
    assert any(i["type"] == "contradiction" for i in data["issues"])
