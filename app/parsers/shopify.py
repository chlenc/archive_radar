from __future__ import annotations

import logging
from typing import Any

import aiohttp

from app.listing import Listing
from app.parsers.base import DEFAULT_USER_AGENT
from app.stores import StoreConfig
from app.utils import clean_line

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 50


class ShopifyParser:
    source = "Shopify"

    def __init__(self, timeout_ms: int, max_items: int):
        self.timeout_seconds = max(timeout_ms // 1000, 5)
        self.max_items = max_items
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "ShopifyParser":
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        # Shopify (or their Cloudflare) started rejecting "Accept: application/json"
        # as a bot signature in mid-May 2026. Browser-like headers pass cleanly.
        # NOTE: omit "br" from Accept-Encoding — aiohttp can't decode brotli
        # without the Brotli package, and adding a Python dep just for this
        # isn't worth it; gzip/deflate are sufficient.
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Accept-Encoding": "gzip, deflate",
            },
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def fetch(self, store: StoreConfig) -> list[Listing]:
        if self._session is None:
            async with self:
                return await self._fetch_with_session(store)
        return await self._fetch_with_session(store)

    async def _fetch_with_session(self, store: StoreConfig) -> list[Listing]:
        assert self._session is not None
        endpoint = self._products_json_url(store)
        try:
            async with self._session.get(endpoint) as response:
                if response.status != 200:
                    raise RuntimeError(
                        f"Shopify endpoint returned HTTP {response.status} for {endpoint}"
                    )
                content_type = response.headers.get("content-type", "")
                if "json" not in content_type.lower():
                    raise RuntimeError(
                        f"Shopify endpoint returned non-JSON ({content_type}) for {endpoint}"
                    )
                payload = await response.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise RuntimeError(f"Shopify request failed for {endpoint}: {exc}") from exc

        products = payload.get("products") if isinstance(payload, dict) else None
        if not isinstance(products, list):
            raise RuntimeError(f"Shopify response has no 'products' array for {endpoint}")

        products_sorted = sorted(
            products,
            key=lambda item: item.get("published_at") or item.get("created_at") or "",
            reverse=True,
        )

        listings: list[Listing] = []
        for product in products_sorted:
            listing = self._listing_from_product(store, product)
            if listing is not None:
                listings.append(listing)
            if len(listings) >= self.max_items:
                break
        return listings

    def _products_json_url(self, store: StoreConfig) -> str:
        root = store.root.rstrip("/")
        if store.collection:
            return f"{root}/collections/{store.collection}/products.json?limit={DEFAULT_LIMIT}"
        return f"{root}/products.json?limit={DEFAULT_LIMIT}"

    def _listing_from_product(
        self, store: StoreConfig, product: dict[str, Any]
    ) -> Listing | None:
        title = clean_line(product.get("title"))
        handle = clean_line(product.get("handle"))
        if not title or not handle:
            return None

        url = f"{store.root.rstrip('/')}/products/{handle}"
        price = self._format_price(product)
        if not price:
            return None
        image_url = self._first_image(product)

        return Listing(
            source=self.source,
            brand=store.name,
            title=title,
            price=price,
            url=url,
            image_url=image_url,
            store=store.slug,
        )

    def _format_price(self, product: dict[str, Any]) -> str:
        variants = product.get("variants")
        if not isinstance(variants, list) or not variants:
            return ""
        available = next(
            (variant for variant in variants if variant.get("available")),
            variants[0],
        )
        raw = available.get("price")
        text = clean_line(raw)
        if not text:
            return ""
        if text.replace(".", "", 1).isdigit():
            return f"${text}"
        return text

    def _first_image(self, product: dict[str, Any]) -> str | None:
        images = product.get("images")
        if isinstance(images, list):
            for image in images:
                if isinstance(image, dict):
                    src = clean_line(image.get("src"))
                    if src:
                        return src
                elif isinstance(image, str) and image:
                    return image
        featured = product.get("featured_image") or product.get("image")
        if isinstance(featured, dict):
            src = clean_line(featured.get("src"))
            if src:
                return src
        if isinstance(featured, str) and featured:
            return featured
        return None
