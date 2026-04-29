from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from app.listing import Listing
from app.utils import clean_line


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

CHROMIUM_LAUNCH_ARGS = [
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
]


# Light stealth: hides obvious "I'm a bot" signals (navigator.webdriver,
# empty plugins/languages, headless chrome UA hint). Not bulletproof against
# Cloudflare's full fingerprint check, but cheap and mostly free of side effects.
STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', {
  get: () => ['en-US', 'en']
});
Object.defineProperty(navigator, 'plugins', {
  get: () => [1, 2, 3, 4, 5]
});
window.chrome = window.chrome || { runtime: {} };
const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
if (originalQuery) {
  window.navigator.permissions.query = (parameters) => (
    parameters && parameters.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : originalQuery(parameters)
  );
}
"""


def parse_proxy(proxy_url: str | None) -> dict | None:
    """Convert a proxy URL like http://user:pass@host:port into Playwright's proxy dict."""
    if not proxy_url:
        return None
    from urllib.parse import urlparse

    parsed = urlparse(proxy_url)
    if not parsed.hostname:
        return None
    server = f"{parsed.scheme or 'http'}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"
    proxy: dict[str, str] = {"server": server}
    if parsed.username:
        proxy["username"] = parsed.username
    if parsed.password:
        proxy["password"] = parsed.password
    return proxy


def first_string(*values: Any) -> str:
    for value in values:
        text = clean_line(value)
        if text:
            return text
    return ""


def nested_get(data: dict[str, Any], path: Iterable[str]) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def first_image(value: Any) -> str | None:
    if isinstance(value, str) and value.startswith(("http://", "https://", "//")):
        return "https:" + value if value.startswith("//") else value
    if isinstance(value, dict):
        for key in ("url", "src", "image_url", "photo_url", "picUrl", "pic_url"):
            found = first_image(value.get(key))
            if found:
                return found
        for nested in value.values():
            found = first_image(nested)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = first_image(item)
            if found:
                return found
    return None


def normalize_url(url: str, base: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return base.rstrip("/") + url
    return url


async def save_debug(page: Page, name: str) -> None:
    Path("debug").mkdir(parents=True, exist_ok=True)
    safe_name = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name)[:80]
    await page.screenshot(path=f"debug/{safe_name}.png", full_page=True)


def compact_json(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False)[:500]
    except TypeError:
        return str(obj)[:500]


def valid_listing(listing: Listing) -> bool:
    return bool(listing.url and listing.title and listing.price)
