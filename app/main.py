from __future__ import annotations

import argparse
import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.bot import create_dispatcher
from app.config import Settings, load_settings
from app.database import Database, PostgresDatabase, build_database, migrate_sqlite_to_postgres
from app.parsers.goofish import GoofishParser
from app.publisher import Publisher
from app.stores import STORES
from app.utils import ensure_dirs, setup_logging, write_json_env_file
from app.worker import ParserWorker

logger = logging.getLogger(__name__)


async def ensure_store_topics(bot: Bot, db: Database, settings: Settings) -> None:
    if not settings.telegram_chat_id:
        logger.warning("TELEGRAM_CHAT_ID is not set; skipping store topic bootstrap.")
        return
    for config in STORES:
        store = db.upsert_store(config.slug, config.name)
        if store.thread_id:
            continue
        try:
            topic = await bot.create_forum_topic(
                chat_id=settings.telegram_chat_id,
                name=config.topic_name,
            )
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            logger.warning(
                "Could not create forum topic for store %s: %s", config.name, exc,
            )
            continue
        db.set_store_thread(config.slug, topic.message_thread_id)
        logger.info(
            "Created forum topic for store %s (thread_id=%s)",
            config.name, topic.message_thread_id,
        )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("worker")
    sub.add_parser("run-once")
    sub.add_parser("test-telegram")
    sub.add_parser("goofish-login")
    sub.add_parser("grailed-login")
    sub.add_parser("grailed-warmup")
    sub.add_parser("migrate-to-postgres")
    return parser


async def init_runtime():
    settings = load_settings()
    setup_logging(settings.log_file)
    ensure_paths = [
        settings.goofish_storage_state,
        settings.grailed_storage_state,
        settings.log_file,
    ]
    if not settings.uses_postgres:
        ensure_paths.insert(0, settings.database_path)
    ensure_dirs(*ensure_paths)
    write_json_env_file(settings.goofish_storage_state_json, settings.goofish_storage_state)
    write_json_env_file(settings.grailed_storage_state_json, settings.grailed_storage_state)
    db = build_database(settings)
    db.init()
    db.seed_from_yaml(settings.brands_file)
    bot = Bot(token=settings.telegram_bot_token)
    await ensure_store_topics(bot, db, settings)
    publisher = Publisher(bot, db, settings.telegram_chat_id)
    worker = ParserWorker(settings, db, publisher)
    return settings, db, bot, publisher, worker


async def cmd_worker() -> None:
    settings, db, bot, _publisher, worker = await init_runtime()
    dp = create_dispatcher(settings, db, bot)
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        worker.run_once,
        "interval",
        seconds=settings.poll_interval_seconds,
        id="marketplace-parser",
        max_instances=1,
        coalesce=True,
    )
    # Optional separate publish schedule. Smooths out the hourly burst by
    # running flush_pending on a tighter cadence (e.g. every 10 min) without
    # hitting the proxy for additional parsing.
    if settings.flush_interval_seconds < settings.poll_interval_seconds:
        scheduler.add_job(
            worker.flush_only,
            "interval",
            seconds=settings.flush_interval_seconds,
            id="publish-flush",
            max_instances=1,
            coalesce=True,
        )
        logger.info(
            "Smooth publishing enabled. Flush interval: %s seconds",
            settings.flush_interval_seconds,
        )
    scheduler.start()
    logger.info("Worker started. Poll interval: %s seconds", settings.poll_interval_seconds)

    async def first_tick() -> None:
        try:
            await worker.run_once()
        except Exception:
            logger.exception("Initial worker tick failed; will retry on schedule.")

    asyncio.create_task(first_tick())

    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)


async def cmd_run_once() -> None:
    _settings, _db, bot, _publisher, worker = await init_runtime()
    try:
        await worker.run_once()
    finally:
        await bot.session.close()


async def cmd_test_telegram() -> None:
    settings, _db, bot, _publisher, _worker = await init_runtime()
    try:
        me = await bot.get_me()
        try:
            chat = await bot.get_chat(settings.telegram_chat_id)
            member = await bot.get_chat_member(settings.telegram_chat_id, me.id)
            await bot.send_message(
                chat_id=settings.telegram_chat_id,
                text=f"Bot connected: @{me.username or me.first_name}",
            )
        except TelegramAPIError as exc:
            print(
                "Telegram bot token is valid, but the test message failed.\n"
                f"Bot: @{me.username or me.first_name}\n"
                f"Chat: {settings.telegram_chat_id}\n"
                f"Error: {exc}\n"
                "Check that this exact bot is added to the forum group as admin."
            )
            return
        print("Telegram test message sent.")
        print(f"Chat: {chat.title} | is_forum={getattr(chat, 'is_forum', None)}")
        print(
            "Bot permissions: "
            f"status={member.status}, "
            f"can_manage_topics={getattr(member, 'can_manage_topics', None)}"
        )
    finally:
        await bot.session.close()


async def cmd_goofish_login() -> None:
    settings = load_settings()
    setup_logging(settings.log_file)
    parser = GoofishParser(
        storage_state=settings.goofish_storage_state,
        headless=False,
        timeout_ms=settings.parser_timeout_ms,
        max_items=settings.max_items_per_source,
    )
    await parser.login()


async def cmd_grailed_warmup() -> None:
    settings = load_settings()
    setup_logging(settings.log_file)
    from app.parsers.grailed import GrailedParser

    parser = GrailedParser(
        storage_state=settings.grailed_storage_state,
        headless=False,
        timeout_ms=settings.parser_timeout_ms,
        max_items=settings.max_items_per_source,
    )
    await parser.warmup()


async def cmd_grailed_login() -> None:
    settings = load_settings()
    setup_logging(settings.log_file)
    if not settings.grailed_email or not settings.grailed_password:
        raise RuntimeError("GRAILED_EMAIL and GRAILED_PASSWORD are required for grailed-login")
    from app.parsers.grailed import GrailedParser

    parser = GrailedParser(
        storage_state=settings.grailed_storage_state,
        headless=False,
        timeout_ms=settings.parser_timeout_ms,
        max_items=settings.max_items_per_source,
    )
    await parser.login(settings.grailed_email, settings.grailed_password)


async def cmd_migrate_to_postgres() -> None:
    settings = load_settings()
    setup_logging(settings.log_file)
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required for migrate-to-postgres")
    if not settings.database_path.exists():
        raise RuntimeError(f"SQLite source database not found: {settings.database_path}")

    summary = migrate_sqlite_to_postgres(
        settings.database_path,
        PostgresDatabase(settings.database_url),
    )
    print(
        "Migrated SQLite to Postgres: "
        f"brands={summary['brands']}, "
        f"stores={summary['stores']}, "
        f"listings={summary['listings']}, "
        f"parser_status={summary['parser_status']}"
    )


async def main() -> None:
    args = build_arg_parser().parse_args()
    if args.command == "worker":
        await cmd_worker()
    elif args.command == "run-once":
        await cmd_run_once()
    elif args.command == "test-telegram":
        await cmd_test_telegram()
    elif args.command == "goofish-login":
        await cmd_goofish_login()
    elif args.command == "grailed-login":
        await cmd_grailed_login()
    elif args.command == "grailed-warmup":
        await cmd_grailed_warmup()
    elif args.command == "migrate-to-postgres":
        await cmd_migrate_to_postgres()


if __name__ == "__main__":
    asyncio.run(main())
