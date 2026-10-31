#!/bin/bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
# Under the lock: if this is the run worktree (ra_run), move it to the latest committed main first.
# Only committed code runs; a failed sync is logged and the run continues on the current checkout.
exec flock -n /tmp/remote_apply.lock bash -c '
  if [ -f .git ]; then
    git checkout -q --detach main 2>&1 && echo "$(date "+%F %H:%M") sync: run worktree at $(git rev-parse --short HEAD)" \
      || echo "$(date "+%F %H:%M") sync FAILED, running current checkout $(git rev-parse --short HEAD)"
  fi
  exec ./ra nightly'
