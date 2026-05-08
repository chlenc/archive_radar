from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from playwright.async_api import BrowserContext, async_playwright

from app.listing import Listing
from app.parsers.base import (
    CHROMIUM_LAUNCH_ARGS,
    DEFAULT_USER_AGENT,
    STEALTH_INIT_SCRIPT,
    first_image,
    first_string,
    normalize_url,
    parse_proxy,
    save_debug,
    valid_listing,
)
from app.utils import clean_line, quote_query, slugify

logger = logging.getLogger(__name__)


class GrailedParser:
    source = "Grailed"

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

    async def __aenter__(self) -> "GrailedParser":
        self._pw = await async_playwright().start()
        launch_kwargs: dict[str, Any] = {
            "headless": self.headless,
            "args": CHROMIUM_LAUNCH_ARGS,
        }
        proxy = parse_proxy(self.proxy_url)
        if proxy:
            launch_kwargs["proxy"] = proxy
            logger.info("Grailed using proxy %s", proxy.get("server"))
        self._browser = await self._pw.chromium.launch(**launch_kwargs)
        context_kwargs: dict[str, Any] = {
            "user_agent": DEFAULT_USER_AGENT,
            "viewport": {"width": 1365, "height": 900},
        }
        if self.storage_state.exists():
            context_kwargs["storage_state"] = str(self.storage_state)
        self._context = await self._browser.new_context(**context_kwargs)
        await self._context.add_init_script(STEALTH_INIT_SCRIPT)
        # Block heavy assets to slash residential-proxy bandwidth (~10x reduction).
        # JSON/JS/HTML still pass through, which is what the parser actually needs.
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
                    logger.warning("Failed to persist Grailed storage state", exc_info=True)
                await self._context.close()
        finally:
            if self._browser is not None:
                await self._browser.close()
            if self._pw is not None:
                await self._pw.stop()
            self._context = None
            self._browser = None
            self._pw = None

    async def fetch(self, brand: str, known_urls: set[str] | None = None) -> list[Listing]:
        if self._context is None:
            async with self:
                return await self._fetch_with_context(brand, known_urls)
        return await self._fetch_with_context(brand, known_urls)

    async def _fetch_with_context(
        self, brand: str, known_urls: set[str] | None = None,
    ) -> list[Listing]:
        assert self._context is not None
        captured: list[dict[str, Any]] = []
        slug = slugify(brand)
        urls = [
            f"https://www.grailed.com/designers/{slug}?sort=newly_listed",
            f"https://www.grailed.com/shop/{slug}?sort=newly_listed",
            f"https://www.grailed.com/search?query={quote_query(brand)}&sort=newly_listed",
        ]

        page = await self._context.new_page()
        page.set_default_timeout(self.timeout_ms)

        async def handle_response(response):
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type:
                return
            url = response.url
            # Grailed proxies search through algolia.net; capture both.
            if "grailed.com" not in url and "algolia" not in url:
                return
            try:
                captured.append(await response.json())
            except Exception:
                return

        page.on("response", handle_response)

        listings: list[Listing] = []
        try:
            for url in urls:
                try:
                    await page.goto(url, wait_until="domcontentloaded")
                except Exception as exc:
                    logger.warning("Grailed goto failed for %s (%s): %s", brand, url, exc)
                    continue
                await page.wait_for_timeout(2500)
                await self._try_sort_newly_listed(page)
                await page.wait_for_timeout(1500)
                listings = self._extract_json_listings(brand, captured)
                if len(listings) >= 3:
                    # Got initial page; scroll to load more until we hit max_items
                    # or we recognize that we've caught up to known listings.
                    listings = await self._scroll_for_more(
                        page, brand, captured, listings, known_urls,
                    )
                    break
                listings = await self._extract_dom_listings(page, brand)
                if listings:
                    break
            return listings[: self.max_items]
        except Exception:
            logger.exception("Grailed parser failed for %s", brand)
            try:
                await save_debug(page, f"grailed_{brand}")
            except Exception:
                logger.debug("Failed to save Grailed debug screenshot", exc_info=True)
            raise
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def warmup(self) -> None:
        self.storage_state.parent.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False, args=CHROMIUM_LAUNCH_ARGS)
            context = await browser.new_context(user_agent=DEFAULT_USER_AGENT)
            page = await context.new_page()
            await page.goto("https://www.grailed.com", wait_until="domcontentloaded")
            print("Complete Grailed security verification if shown, then press Enter here.")
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, input)
            await context.storage_state(path=str(self.storage_state))
            await browser.close()
            print(f"Saved Grailed storage state to {self.storage_state}")

    async def login(self, email: str, password: str) -> None:
        self.storage_state.parent.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False, args=CHROMIUM_LAUNCH_ARGS)
            context = await browser.new_context(user_agent=DEFAULT_USER_AGENT)
            page = await context.new_page()
            try:
                await page.goto("https://www.grailed.com/users/sign_in", wait_until="domcontentloaded")
                await page.wait_for_timeout(2500)
                await self._wait_for_verification(page)
                try:
                    await self._fill_login_form(page, email, password)
                except RuntimeError:
                    print(
                        "Grailed login form did not load automatically. "
                        "Log in manually in the opened browser, then press Enter here."
                    )
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, input)
                await self._wait_for_signed_in_state(page)
                await context.storage_state(path=str(self.storage_state))
                print(f"Saved Grailed storage state to {self.storage_state}")
            finally:
                await browser.close()

    async def _wait_for_verification(self, page) -> None:
        body_text = clean_line(await page.locator("body").inner_text(timeout=3000))
        if "security verification" not in body_text.lower() and "just a moment" not in body_text.lower():
            return
        print("Complete Grailed security verification in the browser, then press Enter here.")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, input)
        await page.goto("https://www.grailed.com/users/sign_in", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

    async def _fill_login_form(self, page, email: str, password: str) -> None:
        email_selectors = [
            'input[type="email"]',
            'input[name="email"]',
            'input[autocomplete="email"]',
        ]
        password_selectors = [
            'input[type="password"]',
            'input[name="password"]',
            'input[autocomplete="current-password"]',
        ]
        email_locator = None
        for selector in email_selectors:
            locator = page.locator(selector).first
            try:
                await locator.wait_for(timeout=3000)
                email_locator = locator
                break
            except Exception:
                continue
        if email_locator is None:
            raise RuntimeError("Grailed login form did not load.")
        await email_locator.fill(email)

        password_locator = None
        for selector in password_selectors:
            locator = page.locator(selector).first
            try:
                await locator.wait_for(timeout=2000)
                password_locator = locator
                break
            except Exception:
                continue
        if password_locator is None:
            raise RuntimeError("Grailed password field did not load.")
        await password_locator.fill(password)

        submit_candidates = [
            page.get_by_role("button", name=re.compile("sign in|log in", re.I)).first,
            page.locator('button[type="submit"]').first,
            page.locator('input[type="submit"]').first,
        ]
        for locator in submit_candidates:
            try:
                await locator.click(timeout=2000)
                return
            except Exception:
                continue
        raise RuntimeError("Grailed login submit button not found.")

    async def _wait_for_signed_in_state(self, page) -> None:
        try:
            await page.wait_for_url(re.compile(r"grailed\.com/(?!users/sign_in).*"), timeout=20000)
            return
        except Exception:
            body_text = clean_line(await page.locator("body").inner_text(timeout=2000))
            if "incorrect" in body_text.lower() or "invalid" in body_text.lower():
                raise RuntimeError("Grailed login failed. Check GRAILED_EMAIL / GRAILED_PASSWORD.")
            print("If Grailed opened a post-login checkpoint, complete it in the browser, then press Enter here.")
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, input)

    async def _try_sort_newly_listed(self, page) -> None:
        candidates = [
            "text=Newly Listed",
            "select",
        ]
        for selector in candidates:
            try:
                if selector == "select":
                    selects = await page.locator("select").all()
                    for select in selects:
                        try:
                            await select.select_option(label="Newly Listed")
                            return
                        except Exception:
                            continue
                else:
                    await page.locator(selector).first.click(timeout=1200)
                    return
            except Exception:
                continue

    async def _scroll_for_more(
        self,
        page,
        brand: str,
        captured: list[dict[str, Any]],
        current: list[Listing],
        known_urls: set[str] | None = None,
        max_scrolls: int = 8,
    ) -> list[Listing]:
        """Trigger Algolia infinite scroll until we have max_items listings or scrolls stall.

        At slow polling intervals (e.g. hourly): without scrolling we'd only see the
        first ~12-40 listings on the page, missing anything older that appeared
        between cycles. So we scroll deep on first pass.

        Once we know about prior listings (known_urls), we can stop early: if 2
        consecutive scrolls add only listings we've already seen in DB, we've
        caught up to the historical archive — no point burning more proxy
        bandwidth fetching pages full of dupes.
        """
        listings = current
        target = self.max_items
        if len(listings) >= target:
            return listings
        last_response_count = len(captured)
        stalls = 0
        # Pre-compute known paths once. Grailed URLs have per-request tracking
        # params (g_aidx, g_aqid) so naive set lookup misses everything.
        known_paths = (
            {u.split("?", 1)[0] for u in known_urls} if known_urls else None
        )
        for _ in range(max_scrolls):
            if len(listings) >= target:
                break
            try:
                await page.mouse.wheel(0, 2500)
            except Exception:
                break
            await page.wait_for_timeout(1500)
            if len(captured) > last_response_count:
                last_response_count = len(captured)
                stalls = 0
            else:
                stalls += 1
                if stalls >= 2:
                    break
            listings = self._extract_json_listings(brand, captured)

            # Ratio-based early exit: once we have a meaningful page (>=15 listings)
            # AND fewer than 20% are new vs DB, we've crossed into the historical
            # archive. Any further scrolling is mostly fetching dupes through the
            # paid proxy.
            if known_paths is not None and len(listings) >= 15:
                new_count = sum(
                    1 for x in listings if x.url.split("?", 1)[0] not in known_paths
                )
                new_ratio = new_count / len(listings)
                if new_ratio < 0.2:
                    break
        return listings

    def _extract_json_listings(self, brand: str, payloads: list[dict[str, Any]]) -> list[Listing]:
        raw_items: list[dict[str, Any]] = []
        for payload in payloads[-12:]:
            self._walk_json(payload, raw_items)

        listings: list[Listing] = []
        for item in raw_items:
            listing = self._listing_from_json_item(brand, item)
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

        keys = set(value)
        has_listing_shape = (
            {"title", "id"} <= keys
            or {"name", "id"} <= keys
            or "listing" in keys
            or "listing_id" in keys
        )
        text = " ".join(str(key).lower() for key in keys)
        if has_listing_shape and ("price" in text or "photo" in text or "image" in text):
            out.append(value)

        for item in value.values():
            self._walk_json(item, out)

    def _listing_from_json_item(self, brand: str, item: dict[str, Any]) -> Listing | None:
        listing = item.get("listing") if isinstance(item.get("listing"), dict) else item
        listing_id = first_string(
            listing.get("id"),
            listing.get("listing_id"),
            listing.get("objectID"),
            listing.get("objectId"),
        )
        url = first_string(listing.get("url"), listing.get("permalink"), listing.get("href"))
        if not url and listing_id:
            url = f"https://www.grailed.com/listings/{listing_id}"
        url = normalize_url(url, "https://www.grailed.com")

        title = first_string(
            listing.get("title"),
            listing.get("name"),
            listing.get("listing_title"),
            listing.get("short_title"),
        )
        title = self._clean_title(title, url, brand)
        price_value = first_string(
            listing.get("price"),
            listing.get("price_text"),
            listing.get("formatted_price"),
            listing.get("current_price"),
        )
        if isinstance(listing.get("price"), dict):
            price_value = first_string(
                listing["price"].get("formatted"),
                listing["price"].get("display"),
                listing["price"].get("amount"),
            )
        if price_value and not price_value.startswith("$") and price_value.replace(".", "", 1).isdigit():
            price_value = f"${price_value}"

        image_url = first_image(
            listing.get("image_url")
            or listing.get("image")
            or listing.get("photo")
            or listing.get("photos")
            or listing.get("cover_photo")
        )

        if not title or not price_value or not url:
            return None
        if not self._matches_brand(brand, listing, url, title):
            return None
        return Listing(
            source=self.source,
            brand=brand,
            title=title,
            price=price_value,
            url=url,
            image_url=image_url,
        )

    async def _extract_dom_listings(self, page, brand: str) -> list[Listing]:
        try:
            await page.wait_for_selector('a[href*="/listings/"]', timeout=self.timeout_ms)
        except Exception:
            body_text = clean_line(await page.locator("body").inner_text(timeout=2000))
            if "security verification" in body_text.lower() or "cloudflare" in body_text.lower():
                raise RuntimeError(
                    "Grailed security verification is blocking the page. "
                    "Run `python -m app grailed-warmup` or set GRAILED_HEADLESS=false."
                )
            return []
        await page.mouse.wheel(0, 500)
        await asyncio.sleep(0.8)
        anchors = await page.locator('a[href*="/listings/"]').all()
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
                        if (node.querySelector("img") && /[$€£¥]/.test(text)) {
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
        text = clean_line(data.get("text"))
        lines = [line.strip() for line in re.split(r"[\n\r]+", data.get("text") or "") if line.strip()]
        price = next((line for line in lines if re.search(r"[$€£¥]\s*\d", line)), "")
        title_candidates = [
            line
            for line in lines
            if line != price
            and "located in" not in line.lower()
            and "about " not in line.lower()
            and not re.search(r"[$€£¥]\s*\d", line)
        ]
        title = title_candidates[-1] if title_candidates else text[:80]
        url = normalize_url(data.get("href") or "", "https://www.grailed.com")
        title = self._clean_title(title, url, brand)
        image_url = first_image(data.get("image"))
        if not title or not price or not url:
            return None
        if not self._matches_brand(brand, {}, url, title):
            return None
        return Listing(self.source, brand, title, price, url, image_url)

    def _clean_title(self, title: str, url: str, brand: str) -> str:
        if title and not re.fullmatch(r"\d+%\s*off", title.strip(), re.IGNORECASE):
            return title

        parsed = urlparse(url)
        slug = unquote(parsed.path.rsplit("/", 1)[-1])
        slug = re.sub(r"^\d+-", "", slug)
        brand_slug = slugify(brand)
        if slug.startswith(f"{brand_slug}-"):
            slug = slug[len(brand_slug) + 1 :]
        words = [word for word in slug.split("-") if word]
        return " ".join(words).title() if words else title

    def _matches_brand(self, brand: str, item: dict[str, Any], url: str, title: str) -> bool:
        brand_slug = slugify(brand)
        brand_lower = brand.strip().lower()
        haystacks = [
            url.lower(),
            title.lower(),
            str(item.get("designer", "")).lower(),
            str(item.get("designers", "")).lower(),
            str(item.get("brand", "")).lower(),
            str(item.get("brands", "")).lower(),
        ]
        return any(brand_slug in text or brand_lower in text for text in haystacks)

    def _dedupe(self, listings: list[Listing]) -> list[Listing]:
        seen: set[str] = set()
        result: list[Listing] = []
        for listing in listings:
            if listing.url in seen:
                continue
            seen.add(listing.url)
            result.append(listing)
        return result
