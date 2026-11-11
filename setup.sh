#!/usr/bin/env bash
# Interactive installer for the Claude Code <-> Telegram bridge.
#
# What this script does:
#   - checks python3, systemd and the `claude` CLI are present
#   - asks for your bot token / Telegram ID / service name and verifies the
#     token with Telegram before anything is installed
#   - writes bridge.env (bot token, owner id, service name, claude path;
#     mode 600) and a systemd unit from claude-telegram-bridge.service.example
#     that reads it, then installs the unit as a system service
#   - installs a sudoers rule allowing only `systemctl restart <service>`,
#     which /restart and /update need
#   - optionally installs faster-whisper for voice messages (into a private
#     virtualenv, so it also works on distributions that forbid pip --user)
#
# Claude itself is authorised later, from Telegram: every chat (the owner
# included) has its own isolated Claude login, and the bot walks you through
# it on your first message.
#
# Safe to re-run: bridge.env keeps every key this script does not manage
# (for example TELEGRAM_API_URL after a switch to a local Bot API server), and
# an empty answer keeps the current token / owner / service name.
#
# Every question can be answered through the environment instead (BOT_TOKEN,
# OWNER_ID, SERVICE_NAME, LOCAL_BOT_API_SETUP=y|n, INSTALL_VOICE=y|n,
# ASSUME_YES=1), which makes an unattended install possible.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

die() { echo "ERROR: $*" >&2; exit 1; }
as_root() { if [[ $EUID -eq 0 ]]; then "$@"; else sudo "$@"; fi; }
# ask VAR "prompt" [default] [secret] -- a value already in the environment
# wins, then an interactive answer, then the default.
ask() {
    local variable="$1" prompt="$2" fallback="${3:-}" secret="${4:-}" value="${!1:-}"
    if [[ -z "$value" && -t 0 ]]; then
        if [[ -n "$secret" ]]; then read -r -s -p "$prompt" value || value=""; echo
        else read -r -p "$prompt" value || value=""; fi
    fi
    printf -v "$variable" '%s' "${value:-$fallback}"
}
# env_file_set FILE KEY VALUE -- replace one key, keep every other line.
env_file_set() {
    python3 - "$1" "$2" "$3" <<'PY'
import os, sys
path, key, value = sys.argv[1:4]
try:
    lines = open(path, encoding="utf-8").read().splitlines()
except FileNotFoundError:
    lines = []
lines = [line for line in lines if not line.startswith(key + "=")]
lines.append(f"{key}={value}")
tmp = path + ".tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    handle.write("\n".join(lines) + "\n")
os.replace(tmp, path)
PY
}
env_file_get() { [[ -f "$1" ]] && sed -n "s/^$2=//p" "$1" | tail -n 1 || true; }

echo "== Claude Code <-> Telegram bridge setup =="
echo

# --- prerequisites -----------------------------------------------------

command -v python3 >/dev/null 2>&1 \
    || die "python3 not found. Install it first (e.g. 'sudo apt install python3') and re-run this script."
SYSTEMCTL_BIN="$(command -v systemctl || true)"
[[ -n "$SYSTEMCTL_BIN" ]] \
    || die "systemd (systemctl) not found. This installer sets the bridge up as a systemd service; see 'Manual install' in the README for other setups."
[[ $EUID -eq 0 ]] || command -v sudo >/dev/null 2>&1 \
    || die "sudo not found. It is needed to install the service unit and for the bot's /restart command."

CLAUDE_BIN="$(command -v claude || true)"
if [ -z "$CLAUDE_BIN" ] && [ -x "$HOME/.local/bin/claude" ]; then
    CLAUDE_BIN="$HOME/.local/bin/claude"
fi
if [ -z "$CLAUDE_BIN" ]; then
    echo "Claude Code CLI not found."
    echo "Install it first:"
    echo "  curl -fsSL https://claude.ai/install.sh | bash"
    echo "then re-run this script."
    exit 1
fi
echo "Found Claude Code CLI: $CLAUDE_BIN"
echo

# --- gather config -------------------------------------------------------

INSTALL_USER="$(id -un)"
INSTALL_HOME="$HOME"
INSTALL_DIR="$SCRIPT_DIR"
ENV_FILE="$INSTALL_DIR/bridge.env"
PREV_TOKEN="$(env_file_get "$ENV_FILE" TELEGRAM_BOT_TOKEN)"
PREV_OWNER="$(env_file_get "$ENV_FILE" OWNER_ID)"
PREV_SERVICE="$(env_file_get "$ENV_FILE" SERVICE_NAME)"
PREV_SERVICE="${PREV_SERVICE%.service}"
EXISTING_API_URL="$(env_file_get "$ENV_FILE" TELEGRAM_API_URL)"

ask BOT_TOKEN "Telegram bot token (from @BotFather)${PREV_TOKEN:+ [keep current]}: " "$PREV_TOKEN" secret
[[ -n "$BOT_TOKEN" ]] || die "A bot token is required."
[[ "$BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]] || die "That does not look like a bot token (expected <digits>:<letters>)."

# A typo here would otherwise leave a 'running' service and a silent bot.
if [[ "${SKIP_TOKEN_CHECK:-}" != 1 ]]; then
    TELEGRAM_TOKEN_TO_CHECK="$BOT_TOKEN" TELEGRAM_API_BASE_TO_CHECK="${EXISTING_API_URL%/}" python3 - <<'PY' \
        || die "Bot token check failed (set SKIP_TOKEN_CHECK=1 to install without contacting Telegram)."
import json, os, sys, urllib.error, urllib.request
base = os.environ["TELEGRAM_API_BASE_TO_CHECK"] or "https://api.telegram.org"
token = os.environ["TELEGRAM_TOKEN_TO_CHECK"]
try:
    with urllib.request.urlopen(f"{base}/bot{token}/getMe", timeout=15) as response:
        result = json.load(response)
except urllib.error.HTTPError as exc:
    print(f"Telegram rejected this bot token (HTTP {exc.code}).", file=sys.stderr)
    raise SystemExit(1)
except Exception as exc:
    print(f"Could not reach Telegram at {base}: {exc}", file=sys.stderr)
    raise SystemExit(1)
if not result.get("ok"):
    print("Telegram rejected this bot token.", file=sys.stderr)
    raise SystemExit(1)
print("Telegram bot token: valid (@%s)" % result["result"].get("username", "unknown"))
PY
fi

ask OWNER_ID "Your Telegram numeric user ID (message @userinfobot to get it)${PREV_OWNER:+ [$PREV_OWNER]}: " "$PREV_OWNER"
[[ "$OWNER_ID" =~ ^[0-9]+$ ]] || die "Owner ID must be numeric."

ask SERVICE_NAME "Service name [${PREV_SERVICE:-claude-telegram-bridge}]: " "${PREV_SERVICE:-claude-telegram-bridge}"
SERVICE_NAME="${SERVICE_NAME%.service}"
[[ "$SERVICE_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}$ ]] \
    || die "Service name may only contain letters, digits, '_', '.', '@' and '-'."

echo
echo "Will install as systemd service '$SERVICE_NAME', running as user '$INSTALL_USER',"
echo "from '$INSTALL_DIR'. Secrets go to $ENV_FILE (mode 600), not into the unit."
if [[ "${ASSUME_YES:-}" != 1 ]]; then
    [[ -t 0 ]] || die "No terminal: set ASSUME_YES=1 to install unattended."
    read -rp "Continue? [y/N] " CONFIRM || CONFIRM=""
    [[ "$CONFIRM" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }
fi

# /update can optionally build and switch to the loopback-only Telegram Bot
# API server.  Its service still needs a separate, narrowly scoped restart
# grant; do not create that grant for installations that opt out here.
TELEGRAM_BOT_API_UNIT="${TELEGRAM_BOT_API_UNIT:-telegram-bot-api}"
[[ "$TELEGRAM_BOT_API_UNIT" =~ ^[A-Za-z0-9_.@-]+$ ]] \
    || die "Telegram Bot API unit name may only contain letters, digits, '_', '.', '@' and '-'."
PREV_LOCAL_UNIT="$(env_file_get "$ENV_FILE" TELEGRAM_BOT_API_UNIT)"
ask LOCAL_BOT_API_SETUP "Allow /update to configure the optional local Bot API server (files up to 2 GB)? [y/N] " "${PREV_LOCAL_UNIT:+y}"

# --- optional: local voice transcription ---------------------------------
# faster-whisper goes into a private virtualenv: `pip install --user` is
# refused outright on PEP 668 distributions (Debian 12+, Ubuntu 23.04+).

PYTHON_BIN="$(command -v python3)"
VOICE_VENV="$INSTALL_DIR/.venv"
if [[ -x "$VOICE_VENV/bin/python" ]] && "$VOICE_VENV/bin/python" -c "import faster_whisper" >/dev/null 2>&1; then
    PYTHON_BIN="$VOICE_VENV/bin/python"
    echo "faster-whisper is installed in $VOICE_VENV: voice messages will be transcribed."
elif python3 -c "import faster_whisper" >/dev/null 2>&1; then
    echo "faster-whisper is installed: voice messages will be transcribed."
else
    ask INSTALL_VOICE "Install faster-whisper for voice messages (optional, large download)? [y/N] " ""
    if [[ "$INSTALL_VOICE" =~ ^[Yy]$ ]]; then
        if python3 -m venv "$VOICE_VENV" \
                && "$VOICE_VENV/bin/python" -m pip install --quiet --upgrade pip \
                && "$VOICE_VENV/bin/python" -m pip install --quiet faster-whisper; then
            PYTHON_BIN="$VOICE_VENV/bin/python"
            echo "faster-whisper installed into $VOICE_VENV."
        else
            echo "faster-whisper install failed (Debian/Ubuntu may need: sudo apt install python3-venv)"
            echo "-- voice messages will get an explanatory reply instead."
        fi
    else
        echo "Skipping: voice messages will get an explanatory reply instead of a transcript."
    fi
fi

# --- generate + install the environment file, unit and sudoers rule -------

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

env_file_set "$ENV_FILE" TELEGRAM_BOT_TOKEN "$BOT_TOKEN"
env_file_set "$ENV_FILE" OWNER_ID "$OWNER_ID"
env_file_set "$ENV_FILE" SERVICE_NAME "$SERVICE_NAME.service"
env_file_set "$ENV_FILE" CLAUDE_BIN "$CLAUDE_BIN"
if [[ "$LOCAL_BOT_API_SETUP" =~ ^[Yy]$ ]]; then
    env_file_set "$ENV_FILE" TELEGRAM_BOT_API_UNIT "$TELEGRAM_BOT_API_UNIT"
fi
chmod 600 "$ENV_FILE"

sed \
    -e "s|__USER__|${INSTALL_USER}|g" \
    -e "s|__HOME__|${INSTALL_HOME}|g" \
    -e "s|__INSTALL_DIR__|${INSTALL_DIR}|g" \
    -e "s|__ENV_FILE__|${ENV_FILE}|g" \
    -e "s|__PYTHON__|${PYTHON_BIN}|g" \
    claude-telegram-bridge.service.example > "$WORK_DIR/unit.service"

echo
echo "Installing the service unit (requires sudo):"
as_root install -m 644 -o root -g root "$WORK_DIR/unit.service" "/etc/systemd/system/${SERVICE_NAME}.service"

if [[ $EUID -ne 0 ]]; then
    # /restart and /update restart the bridge through `sudo -n`; if the owner
    # opted into the local server, grant exactly its restart too and nothing else.
    {
        printf '%s ALL=(root) NOPASSWD: %s restart %s.service\n' \
            "$INSTALL_USER" "$SYSTEMCTL_BIN" "$SERVICE_NAME"
        if [[ "$LOCAL_BOT_API_SETUP" =~ ^[Yy]$ ]]; then
            printf '%s ALL=(root) NOPASSWD: %s restart %s.service\n' \
                "$INSTALL_USER" "$SYSTEMCTL_BIN" "$TELEGRAM_BOT_API_UNIT"
        fi
    } > "$WORK_DIR/sudoers"
    if ! visudo -cf "$WORK_DIR/sudoers" >/dev/null 2>&1 && ! sudo visudo -cf "$WORK_DIR/sudoers" >/dev/null; then
        die "Generated sudoers rule failed validation -- not installing it."
    fi
    sudo install -m 440 -o root -g root "$WORK_DIR/sudoers" "/etc/sudoers.d/${SERVICE_NAME//./_}-restart"
fi
as_root systemctl daemon-reload
as_root systemctl enable "${SERVICE_NAME}.service"
# restart, not just start: a re-run must pick up the new bridge.env.
as_root systemctl restart "${SERVICE_NAME}.service"

echo
echo "Done. Checking status:"
as_root systemctl status "${SERVICE_NAME}.service" --no-pager || true

echo
echo "Next: send any message to your bot on Telegram. The first time, it replies"
echo "with a login button: authorise Claude in the browser and paste the code back"
echo "into the chat. (This is the bridge's own isolated Claude login; it does not"
echo "use or change the one in ~/.claude.)"
echo "Logs:   journalctl -u ${SERVICE_NAME}.service -f"
echo "Whitelist for other users: edit whitelist.txt in $INSTALL_DIR (comma/newline-separated IDs, no restart needed)."

# --- optional: example personality file -----------------------------------
if [ -t 0 ] && [ -f "$SCRIPT_DIR/personality.example.md" ]; then
    echo
    echo "The bridged assistant has no voice of its own beyond your CLAUDE.md."
    echo "personality.example.md in this repo is a starting point you can install."
    read -rp "Install it to  [1] ~/.claude/CLAUDE.md  [2] a path you choose  [3] skip : " P_CHOICE || P_CHOICE=3
    case "${P_CHOICE:-3}" in
        1) P_DEST="$HOME/.claude/CLAUDE.md" ;;
        2) read -rp "Path for the personality file: " P_DEST || P_DEST="" ;;
        *) P_DEST="" ;;
    esac
    if [ -n "${P_DEST:-}" ]; then
        mkdir -p "$(dirname "$P_DEST")"
        P_BODY="$(sed '/^<!--/,/-->/d' "$SCRIPT_DIR/personality.example.md")"
        if [ -f "$P_DEST" ] && grep -q 'BEGIN personality.example' "$P_DEST"; then
            echo "$P_DEST already has a personality.example block - left as is."
        else
            {
                [ -f "$P_DEST" ] && printf '\n'
                printf '<!-- BEGIN personality.example -->\n'
                printf '%s\n' "$P_BODY"
                printf '<!-- END personality.example -->\n'
            } >> "$P_DEST"
            echo "Installed the example personality to $P_DEST"
        fi
    fi
fi

# --- optional: graphify code-map -------------------------------------------
# graphify (PyPI package "graphifyy", github.com/Graphify-Labs/graphify) turns
# this repo into a queryable knowledge graph under graphify-out/. Optional.
setup_graphify() {
    local platform="$1"
    if command -v graphify >/dev/null 2>&1; then
        echo "graphify: already on PATH ($(command -v graphify))"
    elif command -v uv >/dev/null 2>&1; then
        uv tool install graphifyy || { echo "graphify: install failed, skipping"; return 0; }
    elif command -v pipx >/dev/null 2>&1; then
        pipx install graphifyy || { echo "graphify: install failed, skipping"; return 0; }
    else
        echo "graphify: needs 'uv' or 'pipx' to install - skipping"
        return 0
    fi
    graphify install --platform "$platform" >/dev/null 2>&1 || true
    graphify update . >/dev/null 2>&1 || true
    echo "graphify: code map built under graphify-out/ (re-run 'graphify update .' after edits;"
    echo "          a post-commit hook keeps it fresh if graphify installed one)"
}

if [ -t 0 ]; then
    read -rp "Set up the graphify code-map for this repo? [y/N] " _SETUP_GRAPHIFY || _SETUP_GRAPHIFY=""
    case "${_SETUP_GRAPHIFY:-}" in
        [Yy]*) setup_graphify "claude" ;;
    esac
fi
