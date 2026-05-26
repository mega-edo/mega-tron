#!/usr/bin/env sh
# mega-tron one-shot installer.
#
# Usage (from a git clone):
#     sh install.sh
#
# This script:
#   1. Ensures `uv` is installed (installs it the official way if missing).
#   2. Runs `uv tool install mega-tron` to place the `mega-tron` binary.
#   3. Resolves the binary by absolute path (since ~/.local/bin may not be
#      on the current shell's $PATH yet) and runs `mega-tron setup`.
#   4. Prints a single clear next-step: open a new terminal.
#
# The script is intentionally POSIX `sh` — no bash-isms — so it runs on
# whatever default shell the user happens to have. It never reads stdin,
# so piping from curl is safe.

set -eu

log() {
    # All log lines go to stderr so a future `... | sh` users on a pipe
    # can still see progress while stdout stays clean.
    printf '[mega-tron] %s\n' "$*" >&2
}

err() {
    printf '[mega-tron] error: %s\n' "$*" >&2
    exit 1
}

# ---------------------------------------------------------------- step 1
# Make sure `uv` exists. We don't pin a version — the official installer
# picks the latest stable.
if ! command -v uv >/dev/null 2>&1; then
    log "uv not found; installing via https://astral.sh/uv/install.sh"
    # The official uv installer is the same trust model the user already
    # accepted by running this script. It drops `uv` into ~/.local/bin
    # and prints its own PATH guidance.
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # `uv` was just placed in ~/.local/bin but isn't on $PATH for this
    # shell. Add it explicitly so the next command resolves.
    PATH="$HOME/.local/bin:$PATH"
    export PATH
fi

if ! command -v uv >/dev/null 2>&1; then
    err "uv install appears to have failed (still not on PATH). Install uv manually from https://docs.astral.sh/uv/ and re-run."
fi

# ---------------------------------------------------------------- step 2
# `--upgrade` makes this script idempotent across versions: a first-time
# user gets a fresh install; an existing user re-running the same command
# gets the latest PyPI release transparently. uv treats `install --upgrade`
# as a no-op when already at latest, so the steady-state run is still fast.
log "installing or upgrading the mega-tron Python package via \`uv tool install --upgrade\`..."
uv tool install --quiet --upgrade mega-tron || err "\`uv tool install --upgrade mega-tron\` failed; see output above"

# ---------------------------------------------------------------- step 3
# Find the binary. `uv tool install` always places it under
# ~/.local/bin on Linux/macOS. We don't assume the user's $PATH yet.
MEGA_TRON_BIN=""
for candidate in "$HOME/.local/bin/mega-tron" "$(command -v mega-tron 2>/dev/null || true)"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
        MEGA_TRON_BIN="$candidate"
        break
    fi
done

if [ -z "$MEGA_TRON_BIN" ]; then
    err "mega-tron binary was not found after install. Expected at ~/.local/bin/mega-tron."
fi

log "wiring host CLIs (Codex / Claude / Gemini) and shell PATH..."
"$MEGA_TRON_BIN" setup --target auto || err "\`mega-tron setup\` failed; see output above"

# ---------------------------------------------------------------- step 4
cat >&2 <<'EOF'

[mega-tron] done.

Open a new terminal so `mega-tron` resolves on PATH everywhere
(including hook subprocesses spawned by Codex / Claude / Gemini).

Try it:
    mega-tron search "validate webhook signature"
EOF
