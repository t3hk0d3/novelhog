"""Per-host fixes for scraper defects, registered when the adapter is imported (spec 6.7)."""

from novelhog.scraper_adapter.overrides import fanmtl_com

# Hosts whose requests always go through FlareSolverr, whatever the scraper's host config says.
FORCE_FLARESOLVERR_HOSTS = frozenset({fanmtl_com.HOST})


def register_all() -> None:
    """Install every host override (idempotent)."""
    fanmtl_com.register()


__all__ = ["FORCE_FLARESOLVERR_HOSTS", "register_all"]
