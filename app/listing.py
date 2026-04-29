from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256


@dataclass(frozen=True)
class Listing:
    source: str
    brand: str
    title: str
    price: str
    url: str
    image_url: str | None = None

    @property
    def id(self) -> str:
        return sha256(f"{self.source}:{self.url}".encode("utf-8")).hexdigest()

