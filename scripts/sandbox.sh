#!/usr/bin/env bash
# scripts/sandbox.sh
#
# A disposable development environment where Alex may have root.
#
# Why this exists
# ---------------
# The live machine holds real credentials (Gmail, Gemini, GitHub, Telegram) and
# the shop's ledgers, and the assistant reads untrusted email and Telegram
# content.  "Allow everything" is therefore not safe *there*, no matter who
# owns the laptop.  Inside a throwaway container it is perfectly safe, because
# there is nothing worth stealing and nothing worth destroying.
#
# What you get
# ------------
#   * a Debian + Python image with git, curl, sqlite3, ripgrep and build tools
#   * a `dev` user with passwordless sudo *inside the container only*
#   * a copy of the source tree with every secret and runtime artifact removed
#   * no host environment variables, no host home directory, no live .env
#
# Usage
# -----
#   ./scripts/sandbox.sh create    # copy the source and build the image
#   ./scripts/sandbox.sh shell     # start a shell as `dev` (sudo works)
#   ./scripts/sandbox.sh root      # start a shell as root
#   ./scripts/sandbox.sh reset     # delete the sandbox copy, keep the image
#   ./scripts/sandbox.sh destroy   # remove the image too
#
# Docker is normally usable through the `docker` group, which needs no sudo:
#
#   sudo usermod -aG docker "$USER"    # then log out and back in
#
# If you would rather keep using sudo for docker, set:
#
#   ALEX_SANDBOX_DOCKER="sudo docker" ./scripts/sandbox.sh shell
#
# This script never calls sudo on its own.

set -euo pipefail

IMAGE="alex-sandbox:latest"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SANDBOX_ROOT="${ALEX_SANDBOX_ROOT:-$HOME/alex-sandbox}"
WORK_DIR="$SANDBOX_ROOT/work"
PROJECT_DIR="$WORK_DIR/project"
DOCKER="${ALEX_SANDBOX_DOCKER:-docker}"

info() { printf '[sandbox] %s\n' "$*"; }
die()  { printf '[sandbox] error: %s\n' "$*" >&2; exit 1; }

require_docker() {
    command -v "${DOCKER%% *}" >/dev/null 2>&1 \
        || die "docker is not installed. Install it, then re-run this script."
    if ! $DOCKER info >/dev/null 2>&1; then
        cat >&2 <<'EOF'
[sandbox] error: cannot talk to the Docker daemon.

Pick one:

  sudo usermod -aG docker "$USER"    # then log out and back in
  ALEX_SANDBOX_DOCKER="sudo docker" ./scripts/sandbox.sh shell
EOF
        exit 1
    fi
}

copy_source() {
    mkdir -p "$PROJECT_DIR"

    # Never copy credentials or runtime state into the sandbox.  The live
    # .env files hold the account passwords; a "test" copy of them would make
    # the container just as dangerous as the machine it is meant to replace.
    local excludes=(
        --exclude=.git
        --exclude=.venv
        --exclude=venv
        --exclude=staging
        --exclude=__pycache__
        --exclude='*.pyc'
        --exclude='*.local'
        --exclude=.env
        --exclude='memory/*.db'
        --exclude='memory/*.db-*'
        --exclude='memory/*.log'
        --exclude='activity.log'
    )

    if ! command -v rsync >/dev/null 2>&1; then
        die "rsync is required to copy the source safely. Install it and retry."
    fi

    rsync -a --delete "${excludes[@]}" "$REPO_ROOT/" "$PROJECT_DIR/"

    # Guard against a future exclude typo: a real credential in the sandbox
    # defeats the entire point of the sandbox.
    local leaked
    leaked="$(find "$PROJECT_DIR" \( -name '.env' -o -name '.env.*' -o -name '*.local' \) -print -quit)"
    if [ -n "$leaked" ]; then
        rm -f "$leaked"
        die "a credential file reached the sandbox copy ($leaked); removed it and stopped."
    fi

    cat >"$PROJECT_DIR/.env.local.example" <<'EOF'
# Sandbox credentials only. Never copy a real key from the live machine.
#
#   GEMINI_API_KEY=...
#   LIVEKIT_URL=...
#   LIVEKIT_API_KEY=...
#   LIVEKIT_API_SECRET=...
#
# Everything else (email, GitHub, Telegram) needs no configuration unless you
# are deliberately testing that integration, and then use a throwaway account.
EOF

    info "source copied to $PROJECT_DIR (secrets excluded)"
}

build_image() {
    info "building $IMAGE"
    $DOCKER build -t "$IMAGE" - <<'DOCKERFILE'
FROM python:3.13-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_INPUT=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      sudo git curl ca-certificates sqlite3 ripgrep less nano procps \
      build-essential libssl-dev libffi-dev \
 && rm -rf /var/lib/apt/lists/*

# A normal user with passwordless sudo *inside this container*.
# This grants nothing on the host: the container has no host privileges.
RUN useradd --create-home --shell /bin/bash dev \
 && printf 'dev ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/dev \
 && chmod 0440 /etc/sudoers.d/dev

USER dev
WORKDIR /work
CMD ["/bin/bash"]
DOCKERFILE
}

cmd_create() {
    require_docker
    copy_source
    build_image
    info "ready. next:  ./scripts/sandbox.sh shell"
    info "inside, install deps with:  pip install -r project/requirements.txt"
}

cmd_shell() {
    require_docker
    [ -d "$PROJECT_DIR" ] || die "no sandbox yet. run: ./scripts/sandbox.sh create"
    $DOCKER run --rm -it \
        --hostname alex-sandbox \
        -v "$WORK_DIR:/work" \
        -w /work/project \
        "$IMAGE" \
        bash
}

cmd_root() {
    require_docker
    [ -d "$PROJECT_DIR" ] || die "no sandbox yet. run: ./scripts/sandbox.sh create"
    info "root shell inside the container (the host is unaffected)"
    $DOCKER run --rm -it \
        --hostname alex-sandbox \
        -v "$WORK_DIR:/work" \
        -w /work/project \
        --user root \
        "$IMAGE" \
        bash
}

cmd_reset() {
    [ -d "$WORK_DIR" ] || { info "nothing to reset"; return 0; }
    rm -rf "$WORK_DIR"
    info "removed $WORK_DIR"
}

cmd_destroy() {
    cmd_reset
    if command -v "${DOCKER%% *}" >/dev/null 2>&1; then
        $DOCKER image rm -f "$IMAGE" >/dev/null 2>&1 || true
    fi
    info "removed image $IMAGE"
}

case "${1:-}" in
    create)  cmd_create ;;
    shell)   cmd_shell ;;
    root)    cmd_root ;;
    reset)   cmd_reset ;;
    destroy) cmd_destroy ;;
    *)
        sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
        exit 1
        ;;
esac
