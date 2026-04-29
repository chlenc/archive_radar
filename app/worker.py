from __future__ import annotations

import asyncio
import contextlib
import logging

from app.config import Settings
from app.database import Brand, Database
from app.parsers import GoofishParser, GrailedParser
from app.publisher import Publisher

logger = logging.getLogger(__name__)


class ParserWorker:
    def __init__(self, settings: Settings, db: Database, publisher: Publisher):
        self.settings = settings
        self.db = db
        self.publisher = publisher
        self._lock = asyncio.Lock()
        self.grailed = GrailedParser(
            storage_state=settings.grailed_storage_state,
            headless=settings.grailed_headless,
            timeout_ms=settings.parser_timeout_ms,
            max_items=settings.max_items_per_source,
            proxy_url=settings.grailed_proxy_url,
        )
        self.goofish = GoofishParser(
            storage_state=settings.goofish_storage_state,
            headless=settings.goofish_headless,
            timeout_ms=settings.parser_timeout_ms,
            max_items=settings.max_items_per_source,
            proxy_url=settings.goofish_proxy_url,
        )

    def _record_status(self, source: str, brand: str, ok: bool, error: str | None = None) -> None:
        try:
            self.db.record_status(source, brand, ok=ok, error=error)
        except Exception:
            logger.warning(
                "Failed to record parser status (%s/%s ok=%s); continuing.",
                source, brand, ok, exc_info=True,
            )

    async def run_once(self) -> None:
        if self._lock.locked():
            logger.info("Parser run skipped because previous run is still active")
            return
        async with self._lock:
            try:
                brands = self.db.active_brands()
            except Exception:
                logger.exception("Failed to load active brands; skipping cycle.")
                return
            if not brands:
                logger.info("No active brands configured")
                return
            brands_by_name = {brand.name: brand for brand in brands}

            grailed_ctx = self.grailed if self.settings.grailed_enabled else None
            goofish_ctx = self._goofish_or_none()

            async with contextlib.AsyncExitStack() as stack:
                if grailed_ctx is not None:
                    try:
                        await stack.enter_async_context(grailed_ctx)
                    except Exception:
                        logger.exception("Failed to start Grailed browser; skipping Grailed this cycle.")
                        grailed_ctx = None
                if goofish_ctx is not None:
                    try:
                        await stack.enter_async_context(goofish_ctx)
                    except Exception:
                        logger.exception("Failed to start Goofish browser; skipping Goofish this cycle.")
                        goofish_ctx = None

                for brand in brands:
                    await self.run_brand(brand, grailed_ctx, goofish_ctx)

            await self.flush_pending(brands_by_name)

    def _goofish_or_none(self) -> GoofishParser | None:
        if not self.settings.goofish_enabled:
            return None
        if not self.settings.goofish_storage_state.exists():
            return None
        return self.goofish

    async def run_brand(
        self,
        brand: Brand,
        grailed: GrailedParser | None,
        goofish: GoofishParser | None,
    ) -> None:
        if self.settings.grailed_enabled:
            if not self.settings.grailed_storage_state.exists() and (
                self.settings.grailed_email or self.settings.grailed_password
            ):
                self._record_status(
                    self.grailed.source,
                    brand.name,
                    ok=False,
                    error=(
                        "Grailed credentials are set, but no storage state exists yet. "
                        "Run `python -m app grailed-login` locally and upload the resulting state."
                    ),
                )
            elif grailed is not None:
                await self._run_source(grailed, brand)
        if self.settings.goofish_enabled:
            if not self.settings.goofish_storage_state.exists():
                self._record_status(
                    self.goofish.source,
                    brand.name,
                    ok=False,
                    error="Goofish storage state is missing. Run `python -m app goofish-login`.",
                )
                logger.info("Goofish/%s skipped: storage state is missing", brand.name)
                return
            if goofish is not None:
                await self._run_source(goofish, brand)

    async def _run_source(self, parser, brand: Brand) -> None:
        source = parser.source
        try:
            listings = await parser.fetch(brand.name)
            queued = 0
            for listing in listings:
                try:
                    if not self.db.insert_listing_if_new(listing):
                        continue
                    queued += 1
                except Exception:
                    logger.exception(
                        "Failed to persist listing %s; skipping.", listing.url,
                    )
            self._record_status(source, brand.name, ok=True)
            logger.info("%s/%s: fetched=%s queued=%s", source, brand.name, len(listings), queued)
        except Exception as exc:
            logger.exception("%s/%s failed", source, brand.name)
            self._record_status(source, brand.name, ok=False, error=str(exc)[:1000])

    async def flush_pending(self, brands_by_name: dict[str, Brand]) -> None:
        try:
            pending = self.db.pending_listings(self.settings.max_sends_per_run)
        except Exception:
            logger.exception("Failed to load pending listings; skipping flush.")
            return
        if not pending:
            return

        logger.info("Publishing up to %s pending listings", len(pending))
        for listing in pending:
            brand = brands_by_name.get(listing.brand)
            if not brand or not brand.thread_id:
                logger.warning("Skipping pending listing without active thread: %s", listing.url)
                continue
            try:
                await self.publisher.publish(brand, listing)
            except Exception as exc:
                logger.exception("Publish failed for %s/%s", listing.source, listing.brand)
                self._record_status(listing.source, listing.brand, ok=False, error=str(exc)[:1000])
