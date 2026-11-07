#!/bin/sh
# Push the current branch to Alex's GitHub repository.
#
# Requires GH_TOKEN in .env.local with Contents: write and Workflows: write.
# The token is read by scripts/git_askpass.sh and is never stored in Git.

set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT"

BRANCH="${1:-main}"

GIT_ASKPASS="$ROOT/scripts/git_askpass.sh" \
GIT_TERMINAL_PROMPT=0 \
  git push -u origin "$BRANCH"
