#!/bin/bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
timeout 10800 .venv/bin/warn-scraper all --data-dir data/warn --cache-dir data/warn_cache -l warning
