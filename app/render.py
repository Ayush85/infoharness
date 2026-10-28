"""Optional headless-browser rendering for pages that draw their text with JavaScript.

Off by default (RENDER_JS=false) and a no-op when Playwright or Chromium is not installed, so the scanner keeps
working either way. Only used for pages whose plain HTML has almost no text and looks like an app shell.
"""
from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlsplit

from app import config
from app.security import UnsafeURL, check_url_safe

log = logging.getLogger(__name__)

SPA_MARK = re.compile(r'id=["\'](?:root|__next|app|__nuxt|___gatsby)["\']|__NEXT_DATA__|window\.__NUXT__|ng-version|data-reactroot|'
                      r'data-server-rendered|<noscript[^>]*>[^<]*(?:enable|requires?)[^<]*javascript', re.I)
BLOCKED_TYPES = {"image", "media", "font", "stylesheet"}


def available() -> bool:
    if not config.settings.render_js:
        return False
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def needs_render(html: str, word_count: int) -> bool:
    """Plain HTML with (almost) no text that carries app-shell markers: the real content arrives via JavaScript."""
    return word_count < 80 and bool(SPA_MARK.search(html or ""))


class Renderer:
    """One shared headless Chromium per scan, one page at a time, with a page budget."""

    def __init__(self, user_agent: str):
        self.ua = user_agent
        self.budget = config.settings.render_max_pages
        self._pw = self._browser = None
        self._lock = asyncio.Lock()
        self.failed = False
        self.used = 0

    async def _start(self):
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])

    async def render(self, url: str) -> tuple[str, str] | None:
        """Returns (html, final_url) after scripts ran, or None. Never raises."""
        if self.failed or self.budget <= 0:
            return None
        async with self._lock:
            if self.budget <= 0:
                return None
            self.budget -= 1
            ctx = page = None
            try:
                if self._browser is None:
                    await self._start()
                ctx = await self._browser.new_context(user_agent=self.ua, java_script_enabled=True)
                page = await ctx.new_page()

                async def gate(route):
                    req = route.request
                    if req.resource_type in BLOCKED_TYPES or not req.url.startswith(("http://", "https://")):
                        return await route.abort()
                    try:
                        await asyncio.to_thread(check_url_safe, req.url)  # never let a page reach the internal network
                    except UnsafeURL:
                        return await route.abort()
                    await route.continue_()

                await page.route("**/*", gate)
                await page.goto(url, wait_until="domcontentloaded", timeout=config.settings.render_timeout_s * 1000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=4000)
                except Exception:
                    pass
                await page.wait_for_timeout(300)
                html = await page.content()
                self.used += 1
                return html, page.url
            except ImportError:
                self.failed = True
                return None
            except Exception as e:
                msg = str(e)
                if "Executable doesn't exist" in msg or "playwright install" in msg:
                    self.failed = True  # Chromium missing: stop trying for this scan
                log.info("render failed for %s: %s", url, msg[:120])
                return None
            finally:
                try:
                    if ctx:
                        await ctx.close()
                except Exception:
                    pass

    async def close(self):
        try:
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
