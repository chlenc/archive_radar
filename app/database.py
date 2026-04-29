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
class Store:
    slug: str
    name: str
    thread_id: int | None
    seeded: bool


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

    def upsert_store(self, slug: str, name: str) -> Store: ...

    def set_store_thread(self, slug: str, thread_id: int) -> None: ...

    def mark_store_seeded(self, slug: str) -> None: ...

    def get_store(self, slug: str) -> Store | None: ...

    def all_stores(self) -> list[Store]: ...

    def insert_listing_if_new(self, listing: Listing) -> bool: ...

    def mark_existing_listings_sent_for_store(self, store_slug: str) -> int: ...

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

                CREATE TABLE IF NOT EXISTS stores (
                    slug TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    thread_id INTEGER,
                    seeded INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
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
            cols = conn.execute("PRAGMA table_info(listings)").fetchall()
            if not any(row["name"] == "store" for row in cols):
                conn.execute("ALTER TABLE listings ADD COLUMN store TEXT")
            store_cols = conn.execute("PRAGMA table_info(stores)").fetchall()
            if not any(row["name"] == "seeded" for row in store_cols):
                conn.execute(
                    "ALTER TABLE stores ADD COLUMN seeded INTEGER NOT NULL DEFAULT 0"
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

    def upsert_store(self, slug: str, name: str) -> Store:
        timestamp = now_iso()
        with self._lock, self.connect() as conn:
            existing = conn.execute(
                "SELECT slug, name, thread_id, seeded FROM stores WHERE slug = ?",
                (slug,),
            ).fetchone()
            if existing:
                if existing["name"] != name:
                    conn.execute(
                        "UPDATE stores SET name = ? WHERE slug = ?",
                        (name, slug),
                    )
                return Store(
                    slug=slug,
                    name=name,
                    thread_id=existing["thread_id"],
                    seeded=bool(existing["seeded"]),
                )
            conn.execute(
                "INSERT INTO stores (slug, name, thread_id, seeded, created_at) VALUES (?, ?, NULL, 0, ?)",
                (slug, name, timestamp),
            )
            return Store(slug=slug, name=name, thread_id=None, seeded=False)

    def set_store_thread(self, slug: str, thread_id: int) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                "UPDATE stores SET thread_id = ? WHERE slug = ?",
                (thread_id, slug),
            )

    def mark_store_seeded(self, slug: str) -> None:
        with self._lock, self.connect() as conn:
            conn.execute(
                "UPDATE stores SET seeded = 1 WHERE slug = ?",
                (slug,),
            )

    def get_store(self, slug: str) -> Store | None:
        with self._lock, self.connect() as conn:
            row = conn.execute(
                "SELECT slug, name, thread_id, seeded FROM stores WHERE slug = ?",
                (slug,),
            ).fetchone()
        return store_from_row(row) if row else None

    def all_stores(self) -> list[Store]:
        with self._lock, self.connect() as conn:
            rows = conn.execute(
                "SELECT slug, name, thread_id, seeded FROM stores ORDER BY name"
            ).fetchall()
        return [store_from_row(row) for row in rows]

    def insert_listing_if_new(self, listing: Listing) -> bool:
        with self._lock, self.connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO listings
                    (id, source, brand, store, title, price, url, image_url, sent, first_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                    """,
                    (
                        listing.id,
                        listing.source,
                        listing.brand,
                        listing.store,
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

    def mark_existing_listings_sent_for_store(self, store_slug: str) -> int:
        with self._lock, self.connect() as conn:
            cur = conn.execute(
                "UPDATE listings SET sent = 1, posted_at = ? WHERE store = ? AND sent = 0",
                (now_iso(), store_slug),
            )
            return cur.rowcount

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
                SELECT l.source, l.brand, l.store, l.title, l.price, l.url, l.image_url
                FROM listings AS l
                LEFT JOIN brands AS b ON l.store IS NULL AND b.name = l.brand
                LEFT JOIN stores AS s ON l.store IS NOT NULL AND s.slug = l.store
                WHERE l.sent = 0
                  AND (
                    (l.store IS NULL AND b.active = 1)
                    OR (l.store IS NOT NULL AND s.thread_id IS NOT NULL)
                  )
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
                CREATE TABLE IF NOT EXISTS stores (
                    slug TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    thread_id BIGINT,
                    seeded BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            cur.execute(
                "ALTER TABLE stores ADD COLUMN IF NOT EXISTS seeded BOOLEAN NOT NULL DEFAULT FALSE"
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
            cur.execute("ALTER TABLE listings ADD COLUMN IF NOT EXISTS store TEXT")
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

    def upsert_store(self, slug: str, name: str) -> Store:
        timestamp = now_iso()
        with self._lock, self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT slug, name, thread_id, seeded FROM stores WHERE slug = %s",
                (slug,),
            )
            existing = cur.fetchone()
            if existing:
                if existing["name"] != name:
                    cur.execute(
                        "UPDATE stores SET name = %s WHERE slug = %s",
                        (name, slug),
                    )
                return Store(
                    slug=slug,
                    name=name,
                    thread_id=existing["thread_id"],
                    seeded=bool(existing["seeded"]),
                )
            cur.execute(
                "INSERT INTO stores (slug, name, thread_id, seeded, created_at) "
                "VALUES (%s, %s, NULL, FALSE, %s)",
                (slug, name, timestamp),
            )
            return Store(slug=slug, name=name, thread_id=None, seeded=False)

    def set_store_thread(self, slug: str, thread_id: int) -> None:
        with self._lock, self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE stores SET thread_id = %s WHERE slug = %s",
                (thread_id, slug),
            )

    def mark_store_seeded(self, slug: str) -> None:
        with self._lock, self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE stores SET seeded = TRUE WHERE slug = %s",
                (slug,),
            )

    def get_store(self, slug: str) -> Store | None:
        with self._lock, self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT slug, name, thread_id, seeded FROM stores WHERE slug = %s",
                (slug,),
            )
            row = cur.fetchone()
        return store_from_row(row) if row else None

    def all_stores(self) -> list[Store]:
        with self._lock, self.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT slug, name, thread_id, seeded FROM stores ORDER BY name")
            rows = cur.fetchall()
        return [store_from_row(row) for row in rows]

    def insert_listing_if_new(self, listing: Listing) -> bool:
        with self._lock, self.connect() as conn, conn.cursor() as cur:
            try:
                cur.execute(
                    """
                    INSERT INTO listings
                    (id, source, brand, store, title, price, url, image_url, sent, first_seen_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, FALSE, %s)
                    """,
                    (
                        listing.id,
                        listing.source,
                        listing.brand,
                        listing.store,
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

    def mark_existing_listings_sent_for_store(self, store_slug: str) -> int:
        with self._lock, self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE listings SET sent = TRUE, posted_at = %s WHERE store = %s AND sent = FALSE",
                (now_iso(), store_slug),
            )
            return cur.rowcount

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
                SELECT l.source, l.brand, l.store, l.title, l.price, l.url, l.image_url
                FROM listings AS l
                LEFT JOIN brands AS b ON l.store IS NULL AND b.name = l.brand
                LEFT JOIN stores AS s ON l.store IS NOT NULL AND s.slug = l.store
                WHERE l.sent = FALSE
                  AND (
                    (l.store IS NULL AND b.active = TRUE)
                    OR (l.store IS NOT NULL AND s.thread_id IS NOT NULL)
                  )
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
        listings_cols = sqlite_conn.execute("PRAGMA table_info(listings)").fetchall()
        has_store_col = any(row["name"] == "store" for row in listings_cols)
        listings_select_store = "store" if has_store_col else "NULL AS store"
        listing_rows = sqlite_conn.execute(
            f"""
            SELECT
                id,
                source,
                brand,
                {listings_select_store},
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
        try:
            store_cols = sqlite_conn.execute("PRAGMA table_info(stores)").fetchall()
            store_select_seeded = (
                "seeded" if any(row["name"] == "seeded" for row in store_cols) else "0 AS seeded"
            )
            store_rows = sqlite_conn.execute(
                f"SELECT slug, name, thread_id, {store_select_seeded}, created_at "
                "FROM stores ORDER BY slug"
            ).fetchall()
        except sqlite3.OperationalError:
            store_rows = []
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
            INSERT INTO stores (slug, name, thread_id, seeded, created_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT(slug) DO UPDATE SET
                name = excluded.name,
                thread_id = excluded.thread_id,
                seeded = excluded.seeded,
                created_at = excluded.created_at
            """,
            [
                (
                    row["slug"],
                    row["name"],
                    row["thread_id"],
                    bool(row["seeded"]),
                    row["created_at"],
                )
                for row in store_rows
            ],
        )
        cur.executemany(
            """
            INSERT INTO listings (
                id,
                source,
                brand,
                store,
                title,
                price,
                url,
                image_url,
                posted_at,
                sent,
                first_seen_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(id) DO UPDATE SET
                source = excluded.source,
                brand = excluded.brand,
                store = excluded.store,
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
                    row["store"],
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
        "stores": len(store_rows),
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


def store_from_row(row) -> Store:
    return Store(
        slug=row["slug"],
        name=row["name"],
        thread_id=row["thread_id"],
        seeded=bool(row["seeded"]),
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
            store=row["store"],
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
