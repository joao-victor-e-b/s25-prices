"""Headless-browser rendering via Playwright.

Some sources (e.g. Americanas) render prices client-side and block plain HTTP.
This renders the page in headless Chromium and returns the final HTML. Note:
the strongest bot walls (Magalu, Mercado Livre) fingerprint headless Chromium
too and are NOT defeated by this — those stay parked.

Kept deliberately simple: launch → navigate → wait → return HTML → close. The
browser is only imported/launched when a source actually needs it, so plain
`requests` sources don't pay the Playwright cost.
"""
from __future__ import annotations

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


def render(url: str, wait_ms: int = 4000, wait_selector: str | None = None,
           timeout: int = 35000) -> str:
    """Return fully-rendered HTML for a URL. Raises if Playwright/Chromium
    isn't available so the orchestrator counts it as a fetch failure."""
    from playwright.sync_api import sync_playwright  # lazy import

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",  # required when running as root in CI
            ],
        )
        try:
            ctx = browser.new_context(
                user_agent=_UA, locale="pt-BR",
                viewport={"width": 1366, "height": 900},
            )
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=wait_ms)
                except Exception:
                    pass  # fall through with whatever rendered
            else:
                page.wait_for_timeout(wait_ms)
            return page.content()
        finally:
            browser.close()
