#!/usr/bin/env bash
# Interactive installer for the Claude Code <-> Telegram bridge.
#
# What this script does:
#   - checks python3 and the `claude` CLI are present
#   - checks you're already logged in via `claude auth login` (this part is
#     NOT automated -- it's an interactive browser OAuth flow, run it
#     yourself first if needed)
#   - asks for your bot token / Telegram ID / install directory
#   - writes bridge.env (bot token, owner id, service name, claude path;
#     mode 600) and a systemd unit from claude-telegram-bridge.service.example
#     that reads it, then installs the unit as a system service
#   - installs a sudoers rule allowing only `systemctl restart <service>`,
#     which /restart and /update need
#   - optionally installs faster-whisper for voice messages
#
# Safe to re-run: it only overwrites bridge.env, the generated unit and
# its sudoers rule.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "== Claude Code <-> Telegram bridge setup =="
echo

# --- prerequisites -----------------------------------------------------

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found. Install it first (e.g. 'sudo apt install python3') and re-run this script."
    exit 1
fi

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

if ! "$CLAUDE_BIN" auth status 2>/dev/null | grep -q '"loggedIn": true\|loggedIn.*true'; then
    echo
    echo "You're not logged in to Claude Code yet (needs a Pro/Team/Max subscription --"
    echo "this bridge runs on your subscription, not a metered API key)."
    echo "Run this yourself first (it opens a browser for OAuth login):"
    echo "  $CLAUDE_BIN auth login --claudeai"
    echo "Then re-run this script."
    exit 1
fi
echo "Claude Code is authenticated."
echo

# --- gather config -------------------------------------------------------

read -rp "Telegram bot token (from @BotFather): " BOT_TOKEN
if [ -z "$BOT_TOKEN" ]; then
    echo "A bot token is required."
    exit 1
fi

read -rp "Your Telegram numeric user ID (message @userinfobot to get it): " OWNER_ID
if ! [[ "$OWNER_ID" =~ ^[0-9]+$ ]]; then
    echo "Owner ID must be numeric."
    exit 1
fi

read -rp "Service name [claude-telegram-bridge]: " SERVICE_NAME
SERVICE_NAME="${SERVICE_NAME:-claude-telegram-bridge}"
SERVICE_NAME="${SERVICE_NAME%.service}"
if ! [[ "$SERVICE_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}$ ]]; then
    echo "Service name may only contain letters, digits, '_', '.', '@' and '-'."
    exit 1
fi

INSTALL_USER="$(whoami)"
INSTALL_HOME="$HOME"
INSTALL_DIR="$SCRIPT_DIR"
ENV_FILE="$INSTALL_DIR/bridge.env"
SYSTEMCTL_BIN="$(command -v systemctl)"

echo
echo "Will install as systemd service '$SERVICE_NAME', running as user '$INSTALL_USER',"
echo "from '$INSTALL_DIR'. Secrets go to $ENV_FILE (mode 600), not into the unit."
read -rp "Continue? [y/N] " CONFIRM
if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 0
fi

# /update can optionally build and switch to the loopback-only Telegram Bot
# API server.  Its service still needs a separate, narrowly scoped restart
# grant; do not create that grant for installations that opt out here.
TELEGRAM_BOT_API_UNIT="${TELEGRAM_BOT_API_UNIT:-telegram-bot-api}"
if ! [[ "$TELEGRAM_BOT_API_UNIT" =~ ^[A-Za-z0-9_.@-]+$ ]]; then
    echo "Telegram Bot API unit name may only contain letters, digits, '_', '.', '@' and '-'."
    exit 1
fi
read -rp "Allow /update to configure the optional local Bot API server (files up to 2 GB)? [y/N] " LOCAL_BOT_API_SETUP

# --- optional: local voice transcription ---------------------------------

if python3 -c "import faster_whisper" >/dev/null 2>&1; then
    echo "faster-whisper is installed: voice messages will be transcribed."
else
    read -rp "Install faster-whisper for voice messages (optional, large download)? [y/N] " VOICE
    if [[ "$VOICE" =~ ^[Yy]$ ]]; then
        python3 -m pip install --user faster-whisper \
            || echo "faster-whisper install failed -- voice messages will get an explanatory reply instead."
    else
        echo "Skipping: voice messages will get an explanatory reply instead of a transcript."
    fi
fi

# --- generate + install the environment file, unit and sudoers rule -------

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

umask 077
{
    printf 'TELEGRAM_BOT_TOKEN=%s\n' "$BOT_TOKEN"
    printf 'OWNER_ID=%s\n' "$OWNER_ID"
    printf 'SERVICE_NAME=%s.service\n' "$SERVICE_NAME"
    printf 'CLAUDE_BIN=%s\n' "$CLAUDE_BIN"
    if [[ "$LOCAL_BOT_API_SETUP" =~ ^[Yy]$ ]]; then
        printf 'TELEGRAM_BOT_API_UNIT=%s\n' "$TELEGRAM_BOT_API_UNIT"
    fi
} > "$WORK_DIR/bridge.env"
install -m 600 "$WORK_DIR/bridge.env" "$ENV_FILE"
umask 022

sed \
    -e "s|__USER__|${INSTALL_USER}|g" \
    -e "s|__HOME__|${INSTALL_HOME}|g" \
    -e "s|__INSTALL_DIR__|${INSTALL_DIR}|g" \
    -e "s|__ENV_FILE__|${ENV_FILE}|g" \
    claude-telegram-bridge.service.example > "$WORK_DIR/unit.service"

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
    echo "Generated sudoers rule failed validation -- not installing it."
    exit 1
fi

echo
echo "Installing unit and sudoers rule (requires sudo):"
sudo install -m 644 -o root -g root "$WORK_DIR/unit.service" "/etc/systemd/system/${SERVICE_NAME}.service"
sudo install -m 440 -o root -g root "$WORK_DIR/sudoers" "/etc/sudoers.d/${SERVICE_NAME//./_}-restart"
sudo systemctl daemon-reload
sudo systemctl enable --now "${SERVICE_NAME}.service"

echo
echo "Done. Checking status:"
sudo systemctl status "${SERVICE_NAME}.service" --no-pager || true

echo
echo "Next: message your bot on Telegram to start using it."
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
