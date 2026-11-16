"""Who Was That? entry point for the hub registry."""

from __future__ import annotations

from .logic import ensure_schema
from .routes import blueprint


def init_app(_app) -> None:
    ensure_schema()


__all__ = ["blueprint", "init_app"]
