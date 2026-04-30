from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from app.database import Brand, Database, Store
from app.listing import Listing

logger = logging.getLogger(__name__)


def format_caption(listing: Listing) -> str:
    return "\n".join(
        [
            f"🏷 {listing.brand} | {listing.source}",
            f"👟 {listing.title}",
            f"💰 {listing.price}",
            f"🔗 {listing.url}",
        ]
    )


def _is_missing_thread(exc: TelegramBadRequest) -> bool:
    msg = (exc.message or "").lower()
    return (
        "message thread not found" in msg
        or "thread not found" in msg
        or "topic_closed" in msg
        or "topic closed" in msg
        or "chat not found" in msg
    )


class Publisher:
    def __init__(self, bot: Bot, db: Database, chat_id: int):
        self.bot = bot
        self.db = db
        self.chat_id = chat_id

    async def publish(self, brand: Brand, listing: Listing) -> None:
        try:
            await self._send(brand.thread_id, listing)
        except TelegramBadRequest as exc:
            if _is_missing_thread(exc):
                self._handle_missing_brand_thread(brand, listing, exc)
                return
            raise

    async def publish_to_thread(self, thread_id: int, listing: Listing) -> None:
        try:
            await self._send(thread_id, listing)
        except TelegramBadRequest as exc:
            if _is_missing_thread(exc):
                logger.warning(
                    "Telegram thread %s missing while publishing %s; marking as sent.",
                    thread_id, listing.url,
                )
                self._safe_mark_sent(listing)
                return
            raise

    async def publish_to_store(self, store: Store, listing: Listing) -> None:
        try:
            await self._send(store.thread_id, listing)
        except TelegramBadRequest as exc:
            if _is_missing_thread(exc):
                logger.warning(
                    "Telegram thread for store %s (thread_id=%s) is gone (%s). "
                    "Clearing thread_id; topic will be recreated on next worker restart.",
                    store.slug, store.thread_id, exc.message,
                )
                try:
                    self.db.clear_store_thread(store.slug)
                except Exception:
                    logger.warning(
                        "Failed to clear thread_id for store %s",
                        store.slug, exc_info=True,
                    )
                self._safe_mark_sent(listing)
                return
            raise

    def _handle_missing_brand_thread(
        self, brand: Brand, listing: Listing, exc: TelegramBadRequest,
    ) -> None:
        logger.warning(
            "Telegram thread for brand %s (thread_id=%s) is gone (%s). "
            "Clearing thread_id and marking listing as sent. "
            "Use /add_brand %s to recreate the topic.",
            brand.name, brand.thread_id, exc.message, brand.name,
        )
        try:
            self.db.clear_brand_thread(brand.name)
        except Exception:
            logger.warning(
                "Failed to clear thread_id for %s after missing-thread error",
                brand.name, exc_info=True,
            )
        self._safe_mark_sent(listing)

    def _safe_mark_sent(self, listing: Listing) -> None:
        try:
            self.db.mark_sent(listing)
        except Exception:
            logger.warning(
                "Failed to mark listing %s as sent", listing.url, exc_info=True,
            )

    async def _send(self, thread_id: int | None, listing: Listing) -> None:
        caption = format_caption(listing)
        kwargs: dict[str, object] = {"chat_id": self.chat_id}
        if thread_id:
            kwargs["message_thread_id"] = thread_id

        for attempt in range(3):
            try:
                if listing.image_url:
                    await self.bot.send_photo(photo=listing.image_url, caption=caption, **kwargs)
                else:
                    await self.bot.send_message(text=caption, **kwargs)
                self.db.mark_sent(listing)
                return
            except TelegramRetryAfter as exc:
                if attempt == 2:
                    raise
                logger.warning(
                    "Telegram rate limit for %s. Sleeping %s seconds before retry.",
                    listing.url,
                    exc.retry_after,
                )
                await asyncio.sleep(exc.retry_after)
            except TelegramForbiddenError:
                raise
            except TelegramBadRequest as exc:
                if _is_missing_thread(exc):
                    raise
                if listing.image_url:
                    logger.warning("Photo send failed for %s: %s. Falling back to text.", listing.url, exc)
                    try:
                        await self.bot.send_message(text=caption, **kwargs)
                        self.db.mark_sent(listing)
                        return
                    except TelegramBadRequest as inner:
                        if _is_missing_thread(inner):
                            raise
                        raise
                raise

        raise RuntimeError(f"Failed to publish listing after retries: {listing.url}")
