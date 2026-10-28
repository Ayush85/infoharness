"""JavaScript-rendered pages are read when RENDER_JS is on (needs Playwright and Chromium; skipped otherwise)."""
import asyncio
import http.server
import threading

import pytest

pytest.importorskip("playwright")

SHELL = """<!doctype html><html><head><title>App docs</title></head><body><div id="root"></div>
<script>
const pages = {"/": ["Welcome", "The Acme app explains setup, billing and the API in separate guides that you can open below. Every guide is written for engineers who want short answers and exact limits for each plan."],
 "/guide-a": ["Billing guide", "Invoices are issued monthly and payments are due within fourteen days of the invoice date, unless your contract says otherwise. Contact billing for custom terms and refunds on annual plans."],
 "/guide-b": ["API guide", "The API accepts requests from authenticated clients. Each workspace may send one thousand requests per minute, and responses include headers that show the remaining quota for the current window."]};
const p = pages[location.pathname] || pages["/"];
document.getElementById("root").innerHTML = "<h1>" + p[0] + "</h1><p>" + p[1] + "</p><p>" + p[1] + " More detail follows here for completeness of the page." +
  "</p><ul><li><a href='/guide-a'>Billing</a></li><li><a href='/guide-b'>API</a></li></ul>";
</script><noscript>You need to enable JavaScript to run this app.</noscript></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/robots.txt", "/sitemap.xml"):
            self.send_response(404); self.end_headers(); return
        body = SHELL.encode()
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture()
def spa():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_spa_is_read_only_when_rendering_is_on(env, spa, monkeypatch):
    from app import config
    from app.crawler import Crawler
    monkeypatch.setenv("RENDER_JS", "false")
    config.reload_settings()
    off = asyncio.run(Crawler(spa + "/").run())
    assert all(not p.extracted or p.extracted.word_count < 20 for p in off.pages.values())

    monkeypatch.setenv("RENDER_JS", "true")
    config.reload_settings()
    on = asyncio.run(Crawler(spa + "/").run())
    if all(not p.rendered for p in on.pages.values()):
        pytest.skip("Chromium is not installed here")
    assert len(on.pages) >= 3 and any("Invoices are issued monthly" in p.extracted.blocks[1].text or
                                       "Invoices" in " ".join(b.text for b in p.extracted.blocks) for p in on.pages.values())
