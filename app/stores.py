from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StoreConfig:
    slug: str
    name: str
    root: str
    collection: str | None
    topic_name: str


STORES: list[StoreConfig] = [
    StoreConfig(
        slug="archive_reloaded",
        name="Archive Reloaded",
        root="https://archivereloaded.com",
        collection=None,
        topic_name="Archive Reloaded",
    ),
    StoreConfig(
        slug="archived_co",
        name="Archived",
        root="https://archived.co",
        collection="all",
        topic_name="Archived",
    ),
    StoreConfig(
        slug="elevated_archive",
        name="Elevated Archive",
        root="https://elevated-archives.com",
        collection="all-products-1",
        topic_name="Elevated Archive",
    ),
    StoreConfig(
        slug="lowplacelikehome",
        name="Low Place Like Home",
        root="https://lowplacelikehome.com",
        collection="all-in-stock",
        topic_name="Low Place Like Home",
    ),
    StoreConfig(
        slug="lostfilesnyc",
        name="Lost Files NYC",
        root="https://lostfilesnyc.com",
        collection=None,
        topic_name="Lost Files NYC",
    ),
    StoreConfig(
        slug="glam_archive",
        name="Glam Archive",
        root="https://glam-archive.com",
        collection="designer",
        topic_name="Glam Archive",
    ),
    StoreConfig(
        slug="twofold",
        name="Twofold Vintage",
        root="https://twofoldvintage.com",
        collection="all",
        topic_name="Twofold Vintage",
    ),
]


STORES_BY_SLUG: dict[str, StoreConfig] = {store.slug: store for store in STORES}
