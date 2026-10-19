"""Per-host fixes for scraper defects, registered when the adapter is imported (spec 6.7)."""

from novelhog.scraper_adapter.overrides import fanmtl_com


def register_all() -> None:
    """Install every host override (idempotent)."""
    fanmtl_com.register()


__all__ = ["register_all"]
