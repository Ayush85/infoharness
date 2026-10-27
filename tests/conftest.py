"""Test fixtures: a small fake docs site with known problems, served on localhost."""
from __future__ import annotations

import functools
import http.server
import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

NAV = '<nav class="sidebar"><a href="/docs/">Home</a> <a href="/docs/getting-started.html">Start</a> ' \
      '<a href="/docs/refunds.html">Refunds</a> <a href="/docs/payments-faq.html">Payments</a> ' \
      '<a href="/docs/setup.html">Setup</a> <a href="/docs/api.html">API</a></nav>'
FOOT = "<p>Was this article helpful? Let us know so we can improve these docs for everyone.</p>"

SETUP_BODY = """<h2>Install the agent</h2>
<p>Download the installer for your operating system from the dashboard and run it with administrator rights.
The installer configures the background service, creates a default configuration file and registers the
machine with your workspace using the token shown on the setup screen.</p>
<p>After installation, open the dashboard and confirm that the machine appears in the device list. Devices
normally appear within a minute. If the device does not appear, restart the service and check that outbound
connections on port 443 are allowed by your firewall. This guide applies to SDK v5 and the current agent.</p>
<p>To uninstall, stop the service, remove the application folder and delete the device from the dashboard so
its license is released back to your workspace and can be reused by another machine.</p>"""


def page(title: str, body: str, links: str = "") -> str:
    return f"""<!doctype html><html><head><title>{title}</title></head><body>
<header class="site-header"><a href="/">Acme</a></header>{NAV}
<main><h1>{title}</h1>{body}{links}{FOOT}</main>
<footer>© Acme Inc</footer></body></html>"""


def build_site(root: Path, base: str) -> None:
    d = root / "docs"
    d.mkdir(parents=True)
    pages = {
        "index.html": page("Acme Help Center", "<p>Welcome to the Acme help center. Find guides for billing, setup "
                          "and the API. Everything you need to get started with Acme is listed below.</p>",
                          '<ul><li><a href="old-sdk.html">Old SDK</a></li><li><a href="thin.html">Roadmap</a></li>'
                          '<li><a href="setup-copy.html">Setup (copy)</a></li><li><a href="missing.html">Missing</a></li>'
                          '<li><a href="/wp-admin/post.php?post=7&action=edit">Edit this page</a></li>'
                          '<li><a href="https://www.google.com/maps/place/x">Our office</a></li></ul>'),
        "getting-started.html": page("Getting started", "<p>Create an account, invite your team and install the "
                                     "agent. Acme works with SDK v5 on Linux, macOS and Windows. Most teams finish "
                                     "setup in under ten minutes and can start sending data right away.</p>"),
        "refunds.html": page("Refund policy", "<h2>Refund window</h2><p>You can request a full refund within 14 days "
                             "of purchase for any annual subscription by contacting our billing team.</p>"
                             "<p>Refunds are returned to the original payment method and usually arrive in "
                             "five to ten business days depending on your bank.</p>"),
        "payments-faq.html": page("Payments FAQ", "<h2>Can I get my money back?</h2><p>You can request a full "
                                  "refund within 30 days of purchase for any annual subscription by contacting our "
                                  "billing team.</p><p>We accept all major credit cards and bank transfers for "
                                  "invoices above one thousand dollars.</p>"),
        "setup.html": page("Setup guide", SETUP_BODY),
        "setup-copy.html": page("Setup guide (old link)", SETUP_BODY),
        "api.html": page("API reference", "<p>The REST API is available on every paid plan. Authenticate with an "
                         "API key from the dashboard. Requests are rate limited per workspace and the official "
                         "client libraries for version 5 handle retries for you automatically.</p>"),
        "old-sdk.html": page("Legacy SDK guide", "<p>Last updated: March 2019. This guide explains how to "
                             "configure the v2 SDK, including the deprecated polling mode and the legacy XML "
                             "configuration format that older installations used before the current agent.</p>"),
        "thin.html": page("Roadmap", "<p>Coming soon.</p>"),
        "orphan.html": page("Data retention", "<p>Workspace data is retained while your subscription is active. "
                            "When an account is closed, all records are deleted from our primary systems within "
                            "a short period and from backups on the normal rotation schedule.</p>"),
    }
    for name, html in pages.items():
        (d / name).write_text(html)
    sm = "".join(f"<url><loc>{base}/docs/{n}</loc></url>" for n in pages)
    (root / "sitemap.xml").write_text(f'<?xml version="1.0"?><urlset>{sm}</urlset>')
    (root / "robots.txt").write_text(f"User-agent: *\nDisallow: /private/\nSitemap: {base}/sitemap.xml\n")


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


@pytest.fixture(scope="session")
def docs_site(tmp_path_factory):
    root = tmp_path_factory.mktemp("site")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=str(root)))
    base = f"http://127.0.0.1:{server.server_address[1]}"
    build_site(root, base)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield base
    server.shutdown()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Fresh settings + empty SQLite DB for each test."""
    from app import config, db
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("ALLOW_PRIVATE_HOSTS", "true")
    monkeypatch.setenv("CHECK_EXTERNAL_LINKS", "false")
    monkeypatch.setenv("LLM_PROVIDER", "none")
    monkeypatch.setenv("SCANS_PER_IP_PER_DAY", "3")
    for k, v in os.environ.items():
        pass
    config.reload_settings()
    db.init_engine()
    yield config.settings


@pytest.fixture(scope="session")
def redirect_site(docs_site):
    """A vanity domain that 301-redirects everything to the real docs server (different host:port)."""
    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(301)
            self.send_header("Location", docs_site + self.path)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
