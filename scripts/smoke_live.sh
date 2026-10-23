#!/usr/bin/env bash
# Manual check against a live site: one real TOC sync and one chapter download through
# FlareSolverr. Prints the title, the chapter count and the first chapter's Markdown length (never
# the chapter text) and exits non-zero on any failure. Not run in CI.
#
#   NOVELHOG_FLARESOLVERR_URL=http://localhost:8191 scripts/smoke_live.sh <toc_url>
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: NOVELHOG_FLARESOLVERR_URL=<url> $0 <toc_url>" >&2
    exit 2
fi
if [[ -z "${NOVELHOG_FLARESOLVERR_URL:-}" ]]; then
    echo "NOVELHOG_FLARESOLVERR_URL must point at a running FlareSolverr" >&2
    exit 2
fi

cd "$(dirname "$0")/.."

# The scraper keeps its working files under NOVELHOG_SCRAPER_DIR; use a throwaway directory.
scraper_dir="$(mktemp -d)"
trap 'rm -rf "${scraper_dir}"' EXIT
export NOVELHOG_SCRAPER_DIR="${scraper_dir}"
export NOVELHOG_DATA_DIR="${scraper_dir}/data"

uv run python - "$1" <<'PY'
import sys
import time
import traceback

from novelhog import config, html_to_markdown
from novelhog.domain.errors import ValidationFailed
from novelhog.domain.urls import host_of, normalize_toc_url
from novelhog.scraper_adapter import ScraperAdapter
from novelhog.scraper_adapter.errors import InfrastructureError, SiteError

NOVEL_ID = "NVsmokelive00"


def main(toc_url: str) -> int:
    adapter = ScraperAdapter(config.load())
    url = normalize_toc_url(toc_url)
    host = host_of(url)
    if host not in adapter.supported_hosts():
        print(f"FAIL: {host} is not a supported host", file=sys.stderr)
        return 1
    toc = adapter.sync_toc(NOVEL_ID, host, url)
    if not toc.urls:
        print("FAIL: the table of contents has no chapters", file=sys.stderr)
        return 1
    time.sleep(adapter.request_delay(host))  # be as polite as the worker is
    chapter = adapter.fetch_chapter(NOVEL_ID, host, url, toc.urls[0])
    markdown = html_to_markdown.convert(chapter.body_html, lambda _src: None).markdown
    if not markdown.strip():
        print("FAIL: the first chapter converted to empty Markdown", file=sys.stderr)
        return 1
    print(f"title: {toc.title or '(none)'}")
    print(f"chapters: {len(toc.urls)}")
    print(f"first chapter markdown length: {len(markdown)}")
    return 0


try:
    sys.exit(main(sys.argv[1]))
except (SiteError, InfrastructureError, ValidationFailed) as error:
    print(f"FAIL: {type(error).__name__}: {str(error)[:300]}", file=sys.stderr)
    sys.exit(1)
except Exception:  # noqa: BLE001 - unexpected: show where, but never the page content
    traceback.print_exc()
    sys.exit(1)
PY
