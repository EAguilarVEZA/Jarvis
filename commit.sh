#!/usr/bin/env bash
# Jarvis commit helper.
#
# Fixes the two recurring papercuts in this repo:
#   1. Stale .git/*.lock files that block commits (left by sandboxed tooling).
#   2. Keeping the *served* copy (~/Downloads/martin_app.html) in sync with the
#      repo copy so the running app always shows the latest UI.
#
# Usage:
#   ./commit.sh "your commit message"                # commit modified tracked files
#   ./commit.sh "message" brain/ newfile.py          # also stage new paths
#
# It stages all *modified tracked* files automatically (git add -u); pass extra
# paths as additional args to include brand-new files.

set -euo pipefail
cd "$(dirname "$0")"

MSG="${1:-"WIP: update"}"
shift || true

# 1) Clear stale locks (ignore errors if they don't exist).
rm -f .git/HEAD.lock .git/index.lock .git/objects/maintenance.lock 2>/dev/null || true

# 2) Sync the served app copy with the repo copy.
if [ -f martin_app.html ]; then
  cp -f martin_app.html "$HOME/Downloads/martin_app.html" 2>/dev/null \
    && echo "↪ Synced martin_app.html → ~/Downloads/"
fi

# 3) Stage modified tracked files, plus any explicit new paths.
git add -u
if [ "$#" -gt 0 ]; then
  git add -- "$@"
fi

# 4) Commit (skip cleanly if there's nothing staged).
if git diff --cached --quiet; then
  echo "Nothing to commit — working tree clean."
  exit 0
fi

git commit -m "$MSG"
echo "✅ Committed: $MSG"
