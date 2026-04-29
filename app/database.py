from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Protocol, TYPE_CHECKING

import psycopg
import yaml
from psycopg.rows import dict_row

from app.listing import Listing
from app.utils import slugify

if TYPE_CHECKING:
    from app.config import Settings


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Brand:
    name: str
    slug: str
    active: bool
    thread_id: int | None


@dataclass(frozen=True)
class ParserStatus:
    source: str
    brand: str
    ok: bool
    last_run_at: str | None
    last_error: str | None


class Database(Protocol):
    def init(self) -> None: ...

    def seed_from_yaml(self, brands_file: Path) -> None: ...

    def add_brand(self, name: str, thread_id: int | None) -> Brand: ...

    def set_brand_thread(self, name: str, thread_id: int) -> None: ...

    def remove_brand(self, name: str) -> bool: ...

    def get_brand(self, name: str) -> Brand | None: ...

    def active_brands(self) -> list[Brand]: ...

    def insert_listing_if_new(self, listing: Listing) -> bool: ...

    def mark_sent(self, listing: Listing) -> None: ...

    def pending_listings(self, limit: int) -> list[Listing]: ...

    def record_status(self, source: str, brand: str, ok: bool, error: str | None = None) -> None: ...

    def statuses(self) -> list[ParserStatus]: ...

    def unsent_count(self) -> int: ...


class SQLiteDatabase:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self) -> None:
        with self._lock, self.connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode = WAL;

                CREATE TABLE IF NOT EXISTS brands (
                    name TEXT PRIMARY KEY,
                    slug TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    thread_id INTEGER,
                    created_at TEXT NOT NULL,
                    removed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS listings (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    brand TEXT NOT NULL,
                    title TEXT NOT NULL,
                    price TEXT NOT NULL,
                    url TEXT NOT NULL,
                    image_url TEXT,
                    posted_at TEXT,
                    sent INTEGER NOT NULL DEFAULT 0,
                    first_seen_at TEXT NOT NULL,
                    UNIQUE(source, url)
                );

                CREATE TABLE IF NOT EXISTS parser_status (
                    source TEXT NOT NULL,
                    brand TEXT NOT NULL,
                    ok INTEGER NOT NULL,
                    last_run_at TEXT NOT NULL,
                    last_error TEXT,
                    PRIMARY KEY (source, brand)
                );
                """
            )

    def seed_from_yaml(self, brands_file: Path) -> None:
        for name in load_brand_names(brands_file):
            self.add_brand(name, thread_id=None)

    def add_brand(self, name: str, thread_id: int | None) -> Brand:
        clean_name = name.strip()
        slug = slugify(clean_name)
        timestamp = now_iso()
        with self._lock, self.connect() as conn:
            existing = conn.execute(
                "SELECT name, slug, active, thread_id FROM brands WHERE lower(name) = lower(?)",
                (clean_name,),
            ).fetchone()
            if existing:
                final_thread_id = thread_id if thread_id is not None else existing["thread_id"]
                conn.execute(
                    """
                    UPDATE brands
                    SET active = 1, thread_id = ?, removed_at = NULL
                    WHERE name = ?
                    """,
                    (final_thread_id, existing["name"]),
                )
                return brand_from_row(existing, final_thread_id)

            conn.execute(
                """
                INSERT INTO brands (name, slug, active, thread_id, created_at)
                VALUES (?, ?, 1, ?, ?)
                """,
                (clean_name, slug, thread_id, timestamp),
            )
            return Brand(clean_name, slug, True, thread_id)

    def set_brand_thread(self, name: str, thread_id: int) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                "UPDATE brands SET thread_id = ? WHERE lower(name) = lower(?)",
                (thread_id, name.strip()),
            )

    def remove_brand(self, name: str) -> bool:
        with self._lock, self.connect() as conn:
            cur = conn.execute(
                """
                UPDATE brands
                SET active = 0, removed_at = ?
                WHERE lower(name) = lower(?) AND active = 1
                """,
                (now_iso(), name.strip()),
            )
            return cur.rowcount > 0

    def get_brand(self, name: str) -> Brand | None:
        with self._lock, self.connect() as conn:
            row = conn.execute(
                "SELECT name, slug, active, thread_id FROM brands WHERE lower(name) = lower(?)",
                (name.strip(),),
            ).fetchone()
        return brand_from_row(row) if row else None

    def active_brands(self) -> list[Brand]:
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                "SELECT name, slug, active, thread_id FROM brands WHERE active = 1 ORDER BY name"
            ).fetchall()
        return [brand_from_row(row) for row in rows]

    def insert_listing_if_new(self, listing: Listing) -> bool:
        with self._lock, self.connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO listings
                    (id, source, brand, title, price, url, image_url, sent, first_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
                    """,
                    (
                        listing.id,
                        listing.source,
                        listing.brand,
                        listing.title,
                        listing.price,
                        listing.url,
                        listing.image_url,
                        now_iso(),
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def mark_sent(self, listing: Listing) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                "UPDATE listings SET sent = 1, posted_at = ? WHERE id = ?",
                (now_iso(), listing.id),
            )

    def pending_listings(self, limit: int) -> list[Listing]:
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                """
                SELECT l.source, l.brand, l.title, l.price, l.url, l.image_url
                FROM listings AS l
                JOIN brands AS b ON b.name = l.brand
                WHERE l.sent = 0 AND b.active = 1
                ORDER BY l.first_seen_at ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return listings_from_rows(rows)

    def record_status(self, source: str, brand: str, ok: bool, error: str | None = None) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                """
                INSERT INTO parser_status (source, brand, ok, last_run_at, last_error)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source, brand) DO UPDATE SET
                    ok = excluded.ok,
                    last_run_at = excluded.last_run_at,
                    last_error = excluded.last_error
                """,
                (source, brand, int(ok), now_iso(), error),
            )

    def statuses(self) -> list[ParserStatus]:
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                """
                SELECT source, brand, ok, last_run_at, last_error
                FROM parser_status
                ORDER BY brand, source
                """
            ).fetchall()
        return statuses_from_rows(rows)

    def unsent_count(self) -> int:
        with self._lock, self.connect() as conn:
            row = conn.execute("SELECT count(*) AS count FROM listings WHERE sent = 0").fetchone()
        return int(row["count"])


class PostgresDatabase:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self._lock = RLock()

    def connect(self):
        return psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row)

    def init(self) -> None:
        with self._lock, self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS brands (
                    name TEXT PRIMARY KEY,
                    slug TEXT NOT NULL,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    thread_id BIGINT,
                    created_at TIMESTAMPTZ NOT NULL,
                    removed_at TIMESTAMPTZ
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS listings (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    brand TEXT NOT NULL,
                    title TEXT NOT NULL,
                    price TEXT NOT NULL,
                    url TEXT NOT NULL,
                    image_url TEXT,
                    posted_at TIMESTAMPTZ,
                    sent BOOLEAN NOT NULL DEFAULT FALSE,
                    first_seen_at TIMESTAMPTZ NOT NULL,
                    UNIQUE(source, url)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS parser_status (
                    source TEXT NOT NULL,
                    brand TEXT NOT NULL,
                    ok BOOLEAN NOT NULL,
                    last_run_at TIMESTAMPTZ NOT NULL,
                    last_error TEXT,
                    PRIMARY KEY (source, brand)
                )
                """
            )

    def seed_from_yaml(self, brands_file: Path) -> None:
        for name in load_brand_names(brands_file):
            self.add_brand(name, thread_id=None)

    def add_brand(self, name: str, thread_id: int | None) -> Brand:
        clean_name = name.strip()
        slug = slugify(clean_name)
        timestamp = now_iso()
        with self._lock, self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT name, slug, active, thread_id FROM brands WHERE lower(name) = lower(%s)",
                (clean_name,),
            )
            existing = cur.fetchone()
            if existing:
                final_thread_id = thread_id if thread_id is not None else existing["thread_id"]
                cur.execute(
                    """
                    UPDATE brands
                    SET active = TRUE, thread_id = %s, removed_at = NULL
                    WHERE name = %s
                    """,
                    (final_thread_id, existing["name"]),
                )
                return brand_from_row(existing, final_thread_id)

            cur.execute(
                """
                INSERT INTO brands (name, slug, active, thread_id, created_at)
                VALUES (%s, %s, TRUE, %s, %s)
                """,
                (clean_name, slug, thread_id, timestamp),
            )
            return Brand(clean_name, slug, True, thread_id)

    def set_brand_thread(self, name: str, thread_id: int) -> None:
        with self._lock, self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE brands SET thread_id = %s WHERE lower(name) = lower(%s)",
                (thread_id, name.strip()),
            )

    def remove_brand(self, name: str) -> bool:
        with self._lock, self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE brands
                SET active = FALSE, removed_at = %s
                WHERE lower(name) = lower(%s) AND active = TRUE
                """,
                (now_iso(), name.strip()),
            )
            return cur.rowcount > 0

    def get_brand(self, name: str) -> Brand | None:
        with self._lock, self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT name, slug, active, thread_id FROM brands WHERE lower(name) = lower(%s)",
                (name.strip(),),
            )
            row = cur.fetchone()
        return brand_from_row(row) if row else None

    def active_brands(self) -> list[Brand]:
        with self._lock, self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT name, slug, active, thread_id FROM brands WHERE active = TRUE ORDER BY name"
            )
            rows = cur.fetchall()
        return [brand_from_row(row) for row in rows]

    def insert_listing_if_new(self, listing: Listing) -> bool:
        with self._lock, self.connect() as conn, conn.cursor() as cur:
            try:
                cur.execute(
                    """
                    INSERT INTO listings
                    (id, source, brand, title, price, url, image_url, sent, first_seen_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, FALSE, %s)
                    """,
                    (
                        listing.id,
                        listing.source,
                        listing.brand,
                        listing.title,
                        listing.price,
                        listing.url,
                        listing.image_url,
                        now_iso(),
                    ),
                )
                return True
            except psycopg.IntegrityError:
                return False

    def mark_sent(self, listing: Listing) -> None:
        with self._lock, self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE listings SET sent = TRUE, posted_at = %s WHERE id = %s",
                (now_iso(), listing.id),
            )

    def pending_listings(self, limit: int) -> list[Listing]:
        with self._lock, self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT l.source, l.brand, l.title, l.price, l.url, l.image_url
                FROM listings AS l
                JOIN brands AS b ON b.name = l.brand
                WHERE l.sent = FALSE AND b.active = TRUE
                ORDER BY l.first_seen_at ASC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        return listings_from_rows(rows)

    def record_status(self, source: str, brand: str, ok: bool, error: str | None = None) -> None:
        with self._lock, self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO parser_status (source, brand, ok, last_run_at, last_error)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT(source, brand) DO UPDATE SET
                    ok = excluded.ok,
                    last_run_at = excluded.last_run_at,
                    last_error = excluded.last_error
                """,
                (source, brand, ok, now_iso(), error),
            )

    def statuses(self) -> list[ParserStatus]:
        with self._lock, self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT source, brand, ok, last_run_at, last_error
                FROM parser_status
                ORDER BY brand, source
                """
            )
            rows = cur.fetchall()
        return statuses_from_rows(rows)

    def unsent_count(self) -> int:
        with self._lock, self.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) AS count FROM listings WHERE sent = FALSE")
            row = cur.fetchone()
        return int(row["count"])


def build_database(settings: Settings) -> Database:
    if settings.database_url:
        return PostgresDatabase(settings.database_url)
    return SQLiteDatabase(settings.database_path)


def migrate_sqlite_to_postgres(sqlite_path: Path, postgres: PostgresDatabase) -> dict[str, int]:
    sqlite_db = SQLiteDatabase(sqlite_path)
    postgres.init()

    with sqlite_db.connect() as sqlite_conn:
        brand_rows = sqlite_conn.execute(
            """
            SELECT name, slug, active, thread_id, created_at, removed_at
            FROM brands
            ORDER BY name
            """
        ).fetchall()
        listing_rows = sqlite_conn.execute(
            """
            SELECT
                id,
                source,
                brand,
                title,
                price,
                url,
                image_url,
                posted_at,
                sent,
                first_seen_at
            FROM listings
            ORDER BY first_seen_at, id
            """
        ).fetchall()
        status_rows = sqlite_conn.execute(
            """
            SELECT source, brand, ok, last_run_at, last_error
            FROM parser_status
            ORDER BY brand, source
            """
        ).fetchall()

    with postgres._lock, postgres.connect() as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO brands (name, slug, active, thread_id, created_at, removed_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT(name) DO UPDATE SET
                slug = excluded.slug,
                active = excluded.active,
                thread_id = excluded.thread_id,
                created_at = excluded.created_at,
                removed_at = excluded.removed_at
            """,
            [
                (
                    row["name"],
                    row["slug"],
                    bool(row["active"]),
                    row["thread_id"],
                    row["created_at"],
                    row["removed_at"],
                )
                for row in brand_rows
            ],
        )
        cur.executemany(
            """
            INSERT INTO listings (
                id,
                source,
                brand,
                title,
                price,
                url,
                image_url,
                posted_at,
                sent,
                first_seen_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(id) DO UPDATE SET
                source = excluded.source,
                brand = excluded.brand,
                title = excluded.title,
                price = excluded.price,
                url = excluded.url,
                image_url = excluded.image_url,
                posted_at = excluded.posted_at,
                sent = excluded.sent,
                first_seen_at = excluded.first_seen_at
            """,
            [
                (
                    row["id"],
                    row["source"],
                    row["brand"],
                    row["title"],
                    row["price"],
                    row["url"],
                    row["image_url"],
                    row["posted_at"],
                    bool(row["sent"]),
                    row["first_seen_at"],
                )
                for row in listing_rows
            ],
        )
        cur.executemany(
            """
            INSERT INTO parser_status (source, brand, ok, last_run_at, last_error)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT(source, brand) DO UPDATE SET
                ok = excluded.ok,
                last_run_at = excluded.last_run_at,
                last_error = excluded.last_error
            """,
            [
                (
                    row["source"],
                    row["brand"],
                    bool(row["ok"]),
                    row["last_run_at"],
                    row["last_error"],
                )
                for row in status_rows
            ],
        )

    return {
        "brands": len(brand_rows),
        "listings": len(listing_rows),
        "parser_status": len(status_rows),
    }


def load_brand_names(brands_file: Path) -> list[str]:
    if not brands_file.exists():
        return []
    with brands_file.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    names = data.get("brands") or []
    if not isinstance(names, list):
        return []
    return [name.strip() for name in names if isinstance(name, str) and name.strip()]


def brand_from_row(row, thread_id: int | None = None) -> Brand:
    return Brand(
        name=row["name"],
        slug=row["slug"],
        active=bool(row["active"]),
        thread_id=thread_id if thread_id is not None else row["thread_id"],
    )


def listings_from_rows(rows) -> list[Listing]:
    return [
        Listing(
            source=row["source"],
            brand=row["brand"],
            title=row["title"],
            price=row["price"],
            url=row["url"],
            image_url=row["image_url"],
        )
        for row in rows
    ]


def statuses_from_rows(rows) -> list[ParserStatus]:
    return [
        ParserStatus(
            source=row["source"],
            brand=row["brand"],
            ok=bool(row["ok"]),
            last_run_at=row["last_run_at"],
            last_error=row["last_error"],
        )
        for row in rows
    ]


def unique_listings(listings: Iterable[Listing]) -> list[Listing]:
    seen: set[str] = set()
    result: list[Listing] = []
    for listing in listings:
        if listing.url in seen:
            continue
        seen.add(listing.url)
        result.append(listing)
    return result
