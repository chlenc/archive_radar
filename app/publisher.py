from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from app.database import Brand, Database
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


class Publisher:
    def __init__(self, bot: Bot, db: Database, chat_id: int):
        self.bot = bot
        self.db = db
        self.chat_id = chat_id

    async def publish(self, brand: Brand, listing: Listing) -> None:
        caption = format_caption(listing)
        kwargs: dict[str, object] = {"chat_id": self.chat_id}
        if brand.thread_id:
            kwargs["message_thread_id"] = brand.thread_id

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
            except TelegramBadRequest as exc:
                if listing.image_url:
                    logger.warning("Photo send failed for %s: %s. Falling back to text.", listing.url, exc)
                    await self.bot.send_message(text=caption, **kwargs)
                    self.db.mark_sent(listing)
                    return
                raise

        raise RuntimeError(f"Failed to publish listing after retries: {listing.url}")
