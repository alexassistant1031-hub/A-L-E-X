#!/usr/bin/env bash
# controller/setup.sh
#
# One-time setup for Alex's self-modification controller.
#
# Creates the private system directories with the exact ownership and mode the
# controller expects:
#
#   /var/backups/alex         0700, owned by the user running Alex
#   /var/log/alex-controller  0700, ditto
#   <project>/staging         0700, ditto
#
# Run it with sudo:
#
#   sudo bash controller/setup.sh
#
# The directories are owned by the *service account* (the user who runs Alex),
# not by root, because the controller runs as that user.  Root ownership would
# make the backups and audit log unwritable.  The security boundary is the 0700
# mode plus the controller's write-path guard, which denies the agent access to
# these paths even when it runs as the same user.
#
# Override the locations with ALEX_BACKUP_ROOT / ALEX_AUDIT_ROOT.  Remember to
# pass -E to sudo when using custom values.

set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "error: run this script with sudo" >&2
    echo "       sudo bash controller/setup.sh" >&2
    exit 1
fi

TARGET_USER="${SUDO_USER:-${USER}}"
if [[ "${TARGET_USER}" == "root" ]]; then
    echo "error: refusing to give root ownership to the controller" >&2
    echo "       run the script via sudo from the normal account" >&2
    exit 1
fi
TARGET_GROUP="$(id -gn "${TARGET_USER}")"

BACKUP_ROOT="${ALEX_BACKUP_ROOT:-/var/backups/alex}"
AUDIT_ROOT="${ALEX_AUDIT_ROOT:-/var/log/alex-controller}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGING_DIR="${ALEX_STAGING_ROOT:-${PROJECT_DIR}/staging}"

install -d -m 0700 -o "${TARGET_USER}" -g "${TARGET_GROUP}" "${BACKUP_ROOT}"
install -d -m 0700 -o "${TARGET_USER}" -g "${TARGET_GROUP}" "${AUDIT_ROOT}"
install -d -m 0700 -o "${TARGET_USER}" -g "${TARGET_GROUP}" "${STAGING_DIR}"

cat <<EOF
Controller directories are ready:

  backups  ${BACKUP_ROOT}  ($(stat -c '%A %U:%G' "${BACKUP_ROOT}"))
  audit    ${AUDIT_ROOT}  ($(stat -c '%A %U:%G' "${AUDIT_ROOT}"))
  staging  ${STAGING_DIR}  ($(stat -c '%A %U:%G' "${STAGING_DIR}"))

Verify with:

  cd "${PROJECT_DIR}"
  .venv/bin/python -m controller status
EOF
