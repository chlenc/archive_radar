from __future__ import annotations

import logging
import re

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import Message

from app.config import Settings
from app.database import Database

logger = logging.getLogger(__name__)


def is_private_chat(message: Message) -> bool:
    return message.chat.type == "private"


def is_admin(message: Message, settings: Settings) -> bool:
    user = message.from_user
    if not user:
        return False
    if settings.telegram_admin_id and user.id == settings.telegram_admin_id:
        return True
    if settings.telegram_admin_username and user.username:
        return user.username.lower() == settings.telegram_admin_username.lower()
    return False


def command_arg(message: Message) -> str:
    text = message.text or ""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def parse_thread_id(value: str) -> int | None:
    text = value.strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    match = re.search(r"/c/\d+/(\d+)(?:/\d+)?/?$", text)
    if match:
        return int(match.group(1))
    return None


def create_dispatcher(settings: Settings, db: Database, bot: Bot) -> Dispatcher:
    router = Router()

    async def deny_if_not_admin(message: Message) -> bool:
        if not is_private_chat(message):
            return True
        if is_admin(message, settings):
            return False
        await message.answer("Access denied.")
        return True

    @router.message(Command("whoami"))
    async def whoami(message: Message) -> None:
        if not is_private_chat(message):
            return
        user = message.from_user
        if not user:
            await message.answer("No Telegram user in this update.")
            return
        await message.answer(f"id={user.id}\nusername=@{user.username}" if user.username else f"id={user.id}")

    @router.message(Command("add_brand"))
    async def add_brand(message: Message) -> None:
        if await deny_if_not_admin(message):
            return
        name = command_arg(message)
        if not name:
            await message.answer("Usage: /add_brand <brand name>")
            return

        existing = db.get_brand(name)
        if existing and existing.thread_id:
            brand = db.add_brand(name, existing.thread_id)
            await message.answer(f"Brand enabled: {brand.name}. Existing topic reused.")
            return

        try:
            topic = await bot.create_forum_topic(chat_id=settings.telegram_chat_id, name=name)
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            logger.exception("Failed to create forum topic")
            await message.answer(
                "Could not create topic. Check that the target chat is a forum supergroup "
                f"and the bot can manage topics.\n\nTelegram error: {exc}"
            )
            return

        brand = db.add_brand(name, topic.message_thread_id)
        await message.answer(f"Brand added: {brand.name}\nthread_id={topic.message_thread_id}")

    @router.message(Command("bind_brand"))
    async def bind_brand(message: Message) -> None:
        if await deny_if_not_admin(message):
            return
        text = command_arg(message)
        if not text:
            await message.answer("Usage: /bind_brand <brand name> <thread_id or topic link>")
            return
        parts = text.rsplit(maxsplit=1)
        if len(parts) != 2:
            await message.answer("Usage: /bind_brand <brand name> <thread_id or topic link>")
            return
        name, thread_ref = parts
        thread_id = parse_thread_id(thread_ref)
        if not thread_id:
            await message.answer("Could not parse thread id. Send a numeric id or a t.me/c/... topic link.")
            return
        brand = db.add_brand(name, thread_id)
        db.set_brand_thread(brand.name, thread_id)
        await message.answer(f"Brand bound: {brand.name}\nthread_id={thread_id}")

    @router.message(Command("remove_brand"))
    async def remove_brand(message: Message) -> None:
        if await deny_if_not_admin(message):
            return
        name = command_arg(message)
        if not name:
            await message.answer("Usage: /remove_brand <brand name>")
            return
        if db.remove_brand(name):
            await message.answer(f"Brand disabled: {name}")
        else:
            await message.answer(f"Active brand not found: {name}")

    @router.message(Command("list_brands"))
    async def list_brands(message: Message) -> None:
        if await deny_if_not_admin(message):
            return
        brands = db.active_brands()
        if not brands:
            await message.answer("No active brands.")
            return
        lines = [
            f"- {brand.name} (thread_id={brand.thread_id or 'missing'})"
            for brand in brands
        ]
        await message.answer("Active brands:\n" + "\n".join(lines))

    @router.message(Command("list_stores"))
    async def list_stores(message: Message) -> None:
        if await deny_if_not_admin(message):
            return
        stores = db.all_stores()
        if not stores:
            await message.answer("No stores registered yet.")
            return
        lines = []
        for store in stores:
            seeded = "seeded" if store.seeded else "seeding"
            thread = store.thread_id if store.thread_id else "missing"
            lines.append(f"- {store.name} (thread_id={thread}, {seeded})")
        await message.answer(
            "Stores (hardcoded, not editable):\n" + "\n".join(lines)
        )

    @router.message(Command("status"))
    async def status(message: Message) -> None:
        if await deny_if_not_admin(message):
            return
        brands = db.active_brands()
        stores = db.all_stores()
        statuses = db.statuses()
        lines = [
            f"Active brands: {len(brands)}",
            f"Stores: {len(stores)}",
            f"Unsent listings: {db.unsent_count()}",
        ]
        if statuses:
            lines.append("")
            for item in statuses[-40:]:
                marker = "OK" if item.ok else "FAIL"
                suffix = f" | {item.last_error}" if item.last_error else ""
                lines.append(f"{marker} {item.brand} / {item.source} / {item.last_run_at}{suffix}")
        await message.answer("\n".join(lines))

    @router.message(F.text)
    async def fallback(message: Message) -> None:
        if await deny_if_not_admin(message):
            return
        await message.answer(
            "Commands: /add_brand, /bind_brand, /remove_brand, /list_brands, "
            "/list_stores, /status, /whoami"
        )

    dp = Dispatcher()
    dp.include_router(router)
    return dp
