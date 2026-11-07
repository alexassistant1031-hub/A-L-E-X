#!/bin/sh
# Git askpass helper for GitHub HTTPS.
#
# Reads GH_TOKEN from .env.local at call time. The token is never stored in
# this file, in the remote URL, or in .git/config.
#
# Usage:
#   GIT_ASKPASS="$PWD/scripts/git_askpass.sh" GIT_TERMINAL_PROMPT=0 git push

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"

case "$1" in
  *Username*|*username*)
    printf '%s\n' "x-access-token"
    ;;
  *)
    sed -n 's/^GH_TOKEN=//p' "$ROOT/.env.local" | head -n 1
    ;;
esac
