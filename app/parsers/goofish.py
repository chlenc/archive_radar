from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, async_playwright

from app.listing import Listing
from app.parsers.base import (
    CHROMIUM_LAUNCH_ARGS,
    DEFAULT_USER_AGENT,
    STEALTH_INIT_SCRIPT,
    first_image,
    normalize_url,
    parse_proxy,
    save_debug,
    valid_listing,
)
from app.utils import clean_line, quote_query

logger = logging.getLogger(__name__)

# Goofish/Xianyu item ids appear as bare digits, as ?id=N (both the canonical
# https item URL and the fleamarket:// app deep link), or as /item/N.
_ITEM_ID_RE = re.compile(r"(?:[?&]id=|/item/)(\d+)")


def goofish_item_id(*sources: Any) -> str:
    """Best-effort numeric item id from any id field or URL form.

    The search API returns each item under several node shapes: a rich wrapper
    whose targetUrl is a ``fleamarket://item?id=N&referPageArgs=...`` app deep
    link, plus bare ``{itemId, title}`` sub-nodes. Collapsing everything to the
    numeric id lets us build one canonical URL and dedupe reliably.
    """
    for source in sources:
        text = clean_line(source)
        if not text:
            continue
        if text.isdigit():
            return text
        match = _ITEM_ID_RE.search(text)
        if match:
            return match.group(1)
    return ""


def goofish_item_url(item_id: str) -> str:
    return f"https://www.goofish.com/item?id={item_id}"


class GoofishParser:
    source = "Goofish"

    def __init__(
        self,
        storage_state: Path,
        headless: bool,
        timeout_ms: int,
        max_items: int,
        proxy_url: str | None = None,
    ):
        self.storage_state = storage_state
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.max_items = max_items
        self.proxy_url = proxy_url
        self._pw = None
        self._browser = None
        self._context: BrowserContext | None = None

    async def __aenter__(self) -> "GoofishParser":
        self._pw = await async_playwright().start()
        launch_kwargs: dict[str, Any] = {
            "headless": self.headless,
            "args": CHROMIUM_LAUNCH_ARGS,
        }
        proxy = parse_proxy(self.proxy_url)
        if proxy:
            launch_kwargs["proxy"] = proxy
            logger.info("Goofish using proxy %s", proxy.get("server"))
        self._browser = await self._pw.chromium.launch(**launch_kwargs)
        context_kwargs: dict[str, Any] = {
            "user_agent": DEFAULT_USER_AGENT,
            "viewport": {"width": 1365, "height": 900},
        }
        if self.storage_state.exists():
            context_kwargs["storage_state"] = str(self.storage_state)
        self._context = await self._browser.new_context(**context_kwargs)
        await self._context.add_init_script(STEALTH_INIT_SCRIPT)
        await self._context.route(
            "**/*",
            lambda route: (
                route.abort()
                if route.request.resource_type in {"image", "font", "stylesheet", "media"}
                else route.continue_()
            ),
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            if self._context is not None:
                try:
                    self.storage_state.parent.mkdir(parents=True, exist_ok=True)
                    await self._context.storage_state(path=str(self.storage_state))
                except Exception:
                    logger.warning("Failed to persist Goofish storage state", exc_info=True)
                await self._context.close()
        finally:
            if self._browser is not None:
                await self._browser.close()
            if self._pw is not None:
                await self._pw.stop()
            self._context = None
            self._browser = None
            self._pw = None

    async def fetch(
        self, brand: str, known_urls: set[str] | None = None,  # noqa: ARG002
    ) -> list[Listing]:
        if self._context is None:
            async with self:
                return await self._fetch_with_context(brand)
        return await self._fetch_with_context(brand)

    async def _fetch_with_context(self, brand: str) -> list[Listing]:
        assert self._context is not None
        captured: list[dict[str, Any]] = []
        page = await self._context.new_page()
        page.set_default_timeout(self.timeout_ms)

        async def handle_response(response):
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type and "javascript" not in content_type:
                return
            if "goofish.com" not in response.url and "taobao.com" not in response.url:
                return
            try:
                captured.append(await response.json())
            except Exception:
                return

        page.on("response", handle_response)

        try:
            url = f"https://www.goofish.com/search?q={quote_query(brand)}"
            await page.goto(url, wait_until="domcontentloaded")
            await self._wait_for_search_results(page)
            await self._try_click_newest(page)
            # The newest-sort fires a second search; response bodies are read
            # into `captured` by an async handler. Poll a short window instead
            # of racing a fixed sleep (a too-short sleep silently yields 0).
            await page.wait_for_timeout(1200)
            listings: list[Listing] = []
            for _ in range(6):
                listings = self._extract_json_listings(brand, captured)
                if listings:
                    break
                await page.wait_for_timeout(700)
            if not listings:
                listings = await self._extract_dom_listings(page, brand)
            return listings[: self.max_items]
        except Exception:
            logger.exception("Goofish parser failed for %s", brand)
            try:
                await save_debug(page, f"goofish_{brand}")
            except Exception:
                logger.debug("Failed to save Goofish debug screenshot", exc_info=True)
            raise
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def login(self) -> None:
        self.storage_state.parent.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False, args=CHROMIUM_LAUNCH_ARGS)
            context = await browser.new_context(user_agent=DEFAULT_USER_AGENT)
            page = await context.new_page()
            await page.goto("https://www.goofish.com", wait_until="domcontentloaded")
            print("Log in to Goofish in the opened browser, then press Enter here.")
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, input)
            await context.storage_state(path=str(self.storage_state))
            await browser.close()
            print(f"Saved Goofish storage state to {self.storage_state}")

    async def _wait_for_search_results(self, page) -> None:
        """Block on the PC search results API instead of a blind sleep.

        The search page is a SPA: after domcontentloaded it issues the
        ``mtop.taobao.idlemtopsearch.pc.search`` XHR whose JSON carries the
        items. A fixed 2.5s wait raced that request and sometimes extracted
        nothing; waiting for the response makes each fetch deterministic.
        """
        try:
            await page.wait_for_response(
                lambda r: "idlemtopsearch.pc.search/" in r.url and r.status == 200,
                timeout=min(self.timeout_ms, 12000),
            )
            # let the response handler finish reading the body into `captured`
            await page.wait_for_timeout(500)
        except Exception:
            await page.wait_for_timeout(2500)

    async def _try_click_newest(self, page) -> None:
        for text in ("新发", "最新", "New"):
            try:
                await page.get_by_text(text, exact=False).first.click(timeout=1500)
                return
            except Exception:
                continue

    def _extract_json_listings(self, brand: str, payloads: list[dict[str, Any]]) -> list[Listing]:
        raw_items: list[dict[str, Any]] = []
        for payload in payloads[-12:]:
            self._walk_json(payload, raw_items)

        listings: list[Listing] = []
        for item in raw_items:
            listing = self._listing_from_goofish_item(brand, item)
            if listing and valid_listing(listing):
                listings.append(listing)
        return self._dedupe(listings)

    def _walk_json(self, value: Any, out: list[dict[str, Any]]) -> None:
        if isinstance(value, list):
            for item in value:
                self._walk_json(item, out)
            return
        if not isinstance(value, dict):
            return

        ex_content = (
            value.get("data", {})
            .get("item", {})
            .get("main", {})
            .get("exContent")
            if isinstance(value.get("data"), dict)
            else None
        )
        if isinstance(ex_content, dict) and (ex_content.get("itemId") or ex_content.get("title")):
            out.append(value)

        if value.get("itemId") and value.get("title"):
            out.append(value)

        for nested in value.values():
            self._walk_json(nested, out)

    def _listing_from_goofish_item(self, brand: str, item: dict[str, Any]) -> Listing | None:
        main = item.get("data", {}).get("item", {}).get("main", {}) if isinstance(item.get("data"), dict) else {}
        content = main.get("exContent") if isinstance(main, dict) else None
        if not isinstance(content, dict):
            content = item

        main_target = main.get("targetUrl") if isinstance(main, dict) else None
        item_id = goofish_item_id(
            content.get("itemId"),
            content.get("id"),
            main_target,
            content.get("targetUrl"),
            content.get("url"),
        )
        title = clean_line(content.get("title") or content.get("name"))
        price = self._format_price(content.get("price") or content.get("priceInfo") or content.get("soldPrice"))
        image_url = first_image(content.get("picUrl") or content.get("image") or content.get("images"))
        if item_id:
            # Always the canonical web URL — never the fleamarket:// deep link or
            # its per-search tracking params (they break both Telegram links and
            # the cross-run "already seen" comparison).
            url = goofish_item_url(item_id)
        else:
            raw_url = clean_line(main_target) or clean_line(content.get("url") or content.get("targetUrl"))
            url = normalize_url(raw_url, "https://www.goofish.com")
        if not title or not price or not url:
            return None
        return Listing(self.source, brand, title, price, url, image_url)

    def _format_price(self, value: Any) -> str:
        if isinstance(value, list):
            joined = "".join(clean_line(item.get("text") if isinstance(item, dict) else item) for item in value)
            return joined if joined.startswith("¥") else f"¥{joined}" if joined else ""
        if isinstance(value, dict):
            for key in ("text", "price", "amount", "value"):
                text = clean_line(value.get(key))
                if text:
                    return text if text.startswith("¥") else f"¥{text}"
        text = clean_line(value)
        if not text:
            return ""
        return text if text.startswith("¥") else f"¥{text}"

    async def _extract_dom_listings(self, page, brand: str) -> list[Listing]:
        await page.mouse.wheel(0, 700)
        await asyncio.sleep(0.8)
        anchors = await page.locator('a[href*="item"]').all()
        listings: list[Listing] = []
        for anchor in anchors[:80]:
            try:
                data = await anchor.evaluate(
                    """
                    (el) => {
                      let node = el;
                      let card = el;
                      for (let i = 0; i < 8 && node; i++) {
                        const text = node.innerText || "";
                        if (node.querySelector("img") && /[¥￥]\\s*\\d/.test(text)) {
                          card = node;
                          break;
                        }
                        node = node.parentElement;
                      }
                      const img = card.querySelector("img");
                      return {
                        href: el.href,
                        text: card.innerText || el.innerText || "",
                        image: img ? (img.currentSrc || img.src) : null
                      };
                    }
                    """
                )
            except Exception:
                continue
            listing = self._listing_from_dom_data(brand, data)
            if listing:
                listings.append(listing)
        return self._dedupe(listings)

    def _listing_from_dom_data(self, brand: str, data: dict[str, Any]) -> Listing | None:
        lines = [line.strip() for line in re.split(r"[\n\r]+", data.get("text") or "") if line.strip()]
        price = next((line for line in lines if re.search(r"[¥￥]\s*\d", line)), "")
        title_candidates = [
            line
            for line in lines
            if line != price
            and "小时前" not in line
            and "分钟前" not in line
            and "包邮" not in line
            and not re.search(r"[¥￥]\s*\d", line)
        ]
        title = title_candidates[0] if title_candidates else ""
        href = data.get("href") or ""
        item_id = goofish_item_id(href)
        url = goofish_item_url(item_id) if item_id else normalize_url(href, "https://www.goofish.com")
        image_url = first_image(data.get("image"))
        if not title or not price or not url:
            return None
        return Listing(self.source, brand, title, price, url, image_url)

    def _dedupe(self, listings: list[Listing]) -> list[Listing]:
        seen: set[str] = set()
        result: list[Listing] = []
        for listing in listings:
            # Same item surfaces under several node shapes with different URL
            # forms; collapse on the numeric id (falls back to the URL itself).
            key = goofish_item_id(listing.url) or listing.url
            if key in seen:
                continue
            seen.add(key)
            result.append(listing)
        return result
