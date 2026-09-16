# claude-telegram-bridge

> Part of **[telegram-ai](https://github.com/maleon17/telegram-ai)** — Claude/Codex ↔ Telegram, four ways.

A Telegram bridge for [Claude Code](https://claude.com/claude-code) — message Claude from Telegram using your own Claude subscription (Pro/Max/Team), not a metered API key.

Keeps one persistent `claude -p --input-format=stream-json --output-format=stream-json` process per chat (respawned with `--resume` when the model, effort, mode or workspace changes, on `/new`/`/resume`/`/stop`, or after a long idle period) and streams progress back live, with real session management, multi-tenancy (each whitelisted person gets their own isolated Claude account), and support for photos/files in both directions.

## Features

- **Live progress streaming** — one progress message is edited in place while Claude works, showing the current thought, tool call and its result. When the turn ends it becomes a collapsible process log, and the final answer arrives as a new message (so Telegram notifies you). Long answers are split across several messages instead of being truncated, and an answer Telegram rejects (rate limit, network) is kept and re-sent later.
- **Sessions** — `/new`, `/sessions`, `/resume <id>` map onto Claude Code's own `--resume`/session mechanism, so conversations persist across bot restarts.
- **Multi-tenant** — a simple `whitelist.txt` gates access. Anyone besides the owner who's whitelisted goes through their own `claude auth login` (a button + pasted code, right in the chat) and gets a fully isolated Claude account (own subscription, own sessions, own usage) — no shared billing.
- **Model / permission control** — `/model` shows a tap-to-copy picker and accepts full ids (`claude-opus-5`) or short forms (`opus`, `opus 4.7`); `/effort` sets the reasoning effort the chosen model supports; `/mode` switches between auto-approve and a real approve/deny gate for tool calls.
- **Files & photos** — incoming photos/documents are downloaded and handed to Claude to read natively; outgoing files Claude creates (or mentions by path) get sent back as Telegram attachments automatically.
- **Explicit file delivery** — `send_telegram_file` lets an agent return a file deliberately from `CLAUDE_TELEGRAM_OUTBOX`, bound to its current chat without exposing the bot token.
- **Rich messages** — incoming Telegram "rich messages" (tables, collapsible sections, etc.) are converted to Markdown so they don't get silently dropped; outgoing collapsible process logs use the same rich-message format.
- **Forwarded-message batching** — forwarding a batch of messages at once combines them into a single prompt instead of processing only the first and bouncing the rest.

## Requirements

- Linux host with `python3` (the bridge itself is stdlib only)
- Optional: [`faster-whisper`](https://github.com/SYSTRAN/faster-whisper) for transcribing incoming voice messages (`setup.sh` offers to install it; without it voice messages get an explanatory reply)
- [Claude Code CLI](https://claude.com/claude-code) installed and logged in with a Claude subscription (Pro, Max, or Team — the bridge shells out to your own logged-in `claude` binary, it does not use an API key)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- `systemd` (for running it as a persistent service) — not strictly required, but the setup script assumes it

## Quick start

```bash
# 1. Log in to Claude Code first (interactive browser OAuth, do this yourself)
claude auth login --claudeai

# 2. Clone this repo
git clone https://github.com/maleon17/claude-telegram-bridge.git
cd claude-telegram-bridge

# 3. Run the installer -- it asks for your bot token, Telegram ID and service name
./setup.sh
```

That's it — message your bot on Telegram to start.

## Manual install (alternative to `setup.sh`)

If you'd rather not run a script with `sudo`, do it by hand:

1. Create `bridge.env` in this directory with mode `600` (`install -m 600 /dev/null bridge.env`) containing:
   ```
   TELEGRAM_BOT_TOKEN=<token from @BotFather>
   OWNER_ID=<your numeric Telegram ID, from @userinfobot>
   SERVICE_NAME=claude-telegram-bridge.service
   CLAUDE_BIN=<output of: command -v claude>
   ```
2. Copy `claude-telegram-bridge.service.example` to `/etc/systemd/system/claude-telegram-bridge.service` and replace `__USER__`, `__HOME__`, `__INSTALL_DIR__` and `__ENV_FILE__` (absolute path to `bridge.env`). The unit itself holds no secrets.
3. For `/restart` and `/update`, allow exactly one command via sudoers (check with `visudo -cf` before installing it to `/etc/sudoers.d/`):
   ```
   <user> ALL=(root) NOPASSWD: /usr/bin/systemctl restart claude-telegram-bridge.service
   ```
4. `sudo systemctl daemon-reload && sudo systemctl enable --now claude-telegram-bridge.service`

## Commands

| Command | What it does |
|---|---|
| `/new` | Start a new session |
| `/sessions` | List recent sessions |
| `/resume <id>` | Resume a session by ID (or prefix) |
| `/status` | Current session/model/mode/workspace/busy state |
| `/stop` | Interrupt the request currently running |
| `/usage` | Tokens, cost, and account rate limits |
| `/model [model]` | Show the model picker, or switch: `/model claude-opus-5`, `/model opus 4.7`, `/model sonnet`, `/model default` |
| `/effort [level]` | Show or set reasoning effort for the current model: `/effort high`, `/effort default` |
| `/mode <mode>` | Permission mode: `bypass` / `default` / `acceptEdits` / `plan` |
| `/workspace <path>` | Change the working directory for this chat |
| `/approve` / `/approve session` | Allow a blocked tool call once, or bypass for the rest of the session |
| `/deny` | Reject a blocked tool call |
| `/login` | Start/re-authenticate the Claude account for this chat (including the owner account) |
| `/restart` | Owner-only: safe deferred restart (waits for in-flight turns to finish) |
| `/update` | Owner-only: test the latest push in a temporary worktree, fast-forward only if it passes, then restart the same way `/restart` does |

## Updating

Send `/update` from the owner chat, or run `./update.sh` on the host. Both fetch the tracking branch, check the new revision out into a temporary worktree and run `scripts/run_tests.sh` there; the working checkout is fast-forwarded only if every check passes, otherwise it is left untouched. `/update` then schedules the deferred restart (after in-flight turns and queued messages finish; messages arriving during the restart get a "restarting" reply). `./update.sh` alone does not restart — follow it with `/restart`. The bot token and owner ID live in `bridge.env`, which updates never touch.

## Tests

`scripts/run_tests.sh` byte-compiles every module and runs each `breaker_tests/test_*.py` against the checkout it lives in. Tests that exercise the sibling `claude-jarvis` / `codex-jarvis` repositories look for them at `~/claude-jarvis` and `~/codex-jarvis` (override with `CLAUDE_JARVIS_DIR` / `CODEX_JARVIS_DIR`) and report `SKIP` when they are absent.

## Multi-tenancy: giving someone else access

1. Add their numeric Telegram ID to `whitelist.txt` (comma or newline separated — no restart needed, it's re-read on every message).
2. Tell them to message the bot. They'll get a "not whitelisted" prompt with their ID and a button.
3. Once you've added their ID, they press the button (or just message the bot again) — the bot walks them through `claude auth login` for their **own** account: it sends a login link, they authorize it and paste back the code.
4. From then on, everything they do runs against their own Claude subscription, in `accounts/<their_chat_id>/` — fully separate sessions, usage, and billing from the owner's.

The owner (whoever's Telegram ID is in `OWNER_ID`) uses the default, un-isolated Claude account.  If its OAuth session expires, `/login` starts the same browser flow from Telegram: open the button, authorize Claude, then send the displayed code back to the chat.  No SSH access to the host is needed.

## Permission modes

- `bypass` (default) — no confirmation prompts, equivalent to `--dangerously-skip-permissions`. Convenient, but only use this if you trust everything the bot might be asked to do.
- `default` — dangerous tool calls require `/approve` before running.
- `acceptEdits` — file edits are auto-approved, everything else needs `/approve`.
- `plan` — read-only, Claude can't make any changes.

## Troubleshooting

- **"OWNER_ID" / "TELEGRAM_BOT_TOKEN" KeyError on startup** — both are required (no defaults); check `bridge.env` and the unit's `EnvironmentFile=` path.
- **Bot doesn't respond at all** — check `journalctl -u <service-name> -f`; a common cause is `claude auth status` showing not logged in for the account the process is running as.
- **Progress message doesn't show code formatting** — this depends on your Telegram client's active theme; some custom themes don't render `pre` blocks distinctly. Try the default Telegram theme to confirm.
- **`/restart` says the restart was not performed** — the sudoers rule is missing or doesn't match `SERVICE_NAME` exactly (including the `.service` suffix).
- **A whitelisted user's login never completes** — check for a stuck `claude auth login` subprocess (`ps aux | grep "auth login"`); they can just send `/login` again to retry.

## License

MIT, see [LICENSE](LICENSE).

`telegram_format.py` is adapted from [hermes-agent](https://github.com/NousResearch/hermes-agent) (MIT License, Copyright (c) 2025 Nous Research) — see the file header for details.
