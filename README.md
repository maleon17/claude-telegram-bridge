# Claude Telegram Bridge

> Part of **[Telegram AI](https://github.com/maleon17/telegram-ai)** — persistent Claude Code and Codex workflows controlled from Telegram.

**Claude Code on your server, with Telegram as the remote control.**

This bridge keeps a real Claude Code session running behind a Telegram bot. It does not turn each Telegram message into an isolated API request: conversations are resumable, tool activity is visible while Claude works, files travel both ways, and model/workspace/permission controls are available from the phone.

It maintains one `claude -p --input-format=stream-json --output-format=stream-json` process per active chat. A process is respawned with `--resume` after a model, effort, mode or workspace change, an explicit session switch, or a long idle period.

It uses your existing Claude subscription through the installed Claude Code CLI rather than a metered API key.

## Why a persistent bridge?

```mermaid
flowchart LR
    T["Telegram"] <--> B["Claude Telegram Bridge"]
    B <--> C["Persistent Claude Code process"]
    C <--> H["Your host<br/>repos · shell · files · tools"]
```

The important part is where Claude lives: **on the machine that has the project**. Telegram is only the human control surface.

A session can survive bot restarts, be resumed later by ID, expose live progress while tools run, accept files from Telegram, and return generated artifacts without requiring you to sit at an SSH terminal.

## Highlights

- **Live progress** — one Telegram message is edited while Claude works, showing current thought/tool activity and results; the final answer is delivered separately.
- **Native persistent sessions** — `/new`, `/sessions`, and `/resume` use Claude Code's session mechanism.
- **Remote runtime controls** — change model, effort, workspace and permission mode from Telegram.
- **Human approval loop** — permission modes range from read-only planning to explicit approve/deny gates and full bypass.
- **Files in both directions** — incoming photos/documents are passed to Claude; generated files can be returned automatically or explicitly through `send_telegram_file`.
- **Multi-account isolation** — every chat, including the owner, has its own `CLAUDE_CONFIG_DIR`, sessions and usage; additional users authenticate their own Claude subscription.
- **Safe operations** — updates are tested before fast-forwarding and restarts are deferred until active work finishes.
- **Message resilience** — forwarded batches are combined, Telegram rich messages are converted to Markdown, long answers are split, and failed Telegram deliveries can be retried.
- **Voice messages** — optional `faster-whisper` transcribes incoming audio; without it, the bot explains that transcription is unavailable.

## Requirements

- Linux host with `python3` (the bridge itself uses only the standard library)
- [Claude Code CLI](https://claude.com/claude-code) installed, and a Claude subscription (each chat authorises its own from Telegram)
- Telegram bot token from [@BotFather](https://t.me/BotFather)
- `systemd` for the supplied service/install workflow
- Optional [`faster-whisper`](https://github.com/SYSTRAN/faster-whisper) for voice transcription

## Quick start

```bash
git clone https://github.com/maleon17/claude-telegram-bridge.git
cd claude-telegram-bridge
./setup.sh
```

The installer asks for the bot token, Telegram owner ID and service name, and checks the token with Telegram before installing anything. It is safe to re-run: it keeps the settings it does not manage, and an empty answer keeps the current value.

Then send any message to your bot. The first time, it answers with a login button: authorise Claude in the browser and paste the code back into the chat. The bridge keeps its own isolated Claude login per chat and never reads or changes the one in `~/.claude`.

## Commands

| Command | What it does |
|---|---|
| `/language en\|ru\|uk\|kk\|de` | Set the chat's interface and persona language; update the Telegram command menu |
| `/persona` / `/persona reset` | Owner-only: show or reset the chat persona |
| `/new` | Start a new session |
| `/sessions` | List recent sessions |
| `/resume <id>` | Resume a session by ID/prefix |
| `/status` | Show session, model, mode, workspace and busy state |
| `/stop` | Interrupt current work |
| `/compact` | Compact the current session context |
| `/usage` | Show tokens, cost and account rate limits |
| `/model [model]` | Show/select Claude model by full ID or short form such as `opus`, `sonnet` or `default` |
| `/effort [level]` | Show/select reasoning effort |
| `/mode <mode>` | Select permission mode |
| `/workspace <path>` | Change working directory |
| `/approve` / `/approve session` | Approve one blocked tool call or bypass for the session |
| `/deny` | Reject a blocked tool call |
| `/login` | Authenticate/re-authenticate the Claude account |
| `/restart` | Owner-only deferred restart |
| `/update` | Owner-only tested update followed by deferred restart |

## Permission modes

- `bypass` — no confirmation prompts; equivalent to `--dangerously-skip-permissions`.
- `default` — dangerous tool calls require `/approve`.
- `acceptEdits` — file edits are approved automatically; other protected actions still require approval.
- `plan` — read-only planning.

`bypass` is convenient precisely because humans apparently enjoy giving autonomous software shell access at 2 AM. Use it only for trusted chats and hosts.

## Multi-account isolation

`whitelist.txt` controls who may use the bridge and is re-read for every message. Every chat receives isolated account state under `accounts/<chat_id>/`. On the owner's first use, their existing persona (`~/.claude/CLAUDE.md`), MCP configuration and past sessions are copied in; credentials are not, so the owner logs in from Telegram like everyone else.

Additional users get their own subscription, sessions and usage rather than sharing the owner's billing or context. The owner can also re-authenticate through `/login` in Telegram.

To add someone, put their numeric Telegram ID in `whitelist.txt` (comma or newline separated). They then message the bot and follow its login button, authorize their Claude account in a browser, and paste the resulting code back into that chat. The whitelist is checked again on their next message, so no service restart is required.

Removing a user from the whitelist blocks new requests; account data remains on disk until deliberately removed.

## Files and Telegram delivery

Incoming Telegram photos and documents are downloaded and made available to Claude.

For outgoing artifacts, the bridge can automatically send files Claude writes or names in its final response. Claude can also explicitly call the tenant-scoped `send_telegram_file` tool; that tool accepts files only from the chat's `CLAUDE_TELEGRAM_OUTBOX`. Because automatic delivery can use paths outside that outbox, grant bridge access only to trusted chats and hosts.

## Updating safely

`/update` and `./update.sh` fetch the tracked branch, check the candidate revision in a temporary worktree and run:

```bash
scripts/run_tests.sh
```

The active checkout is fast-forwarded only when checks pass. Bot-triggered updates then schedule a deferred restart after active turns and queued messages finish. `./update.sh` alone does not restart the service; follow it with `/restart`.

## Manual install

Create `bridge.env` with mode `600`:

```dotenv
TELEGRAM_BOT_TOKEN=<token>
OWNER_ID=<numeric Telegram ID>
SERVICE_NAME=claude-telegram-bridge.service
CLAUDE_BIN=<path to claude>
```

Copy `claude-telegram-bridge.service.example` to `/etc/systemd/system/claude-telegram-bridge.service` and replace `__USER__`, `__HOME__`, `__INSTALL_DIR__`, `__ENV_FILE__` (the absolute path to `bridge.env`) and `__PYTHON__` (`/usr/bin/python3`, or a virtualenv's interpreter if you installed `faster-whisper` into one). For `/restart` and `/update`, allow only this sudoers command:

```text
<user> ALL=(root) NOPASSWD: /usr/bin/systemctl restart claude-telegram-bridge.service
```

Validate the sudoers file with `visudo -cf` before installing it.

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now claude-telegram-bridge.service
```

## Working together with the Codex bridge

This bridge is complete on its own. If **[codex-telegram-bridge](https://github.com/maleon17/codex-telegram-bridge)** is installed on the same host, the two can hand tasks to each other:

- **From a chat.** Every chat gets a `delegate_to_codex` tool. When someone asks Claude to "ask Codex" (or similar), the task is passed to the Codex bridge for that same Telegram user. Claude waits for Codex's final answer (ten minutes by default) and continues with it; the same answer also arrives in the Codex bot's chat. The user must be whitelisted and logged in on the Codex bridge too; nobody can delegate into someone else's account.
- **From a shell or script.** `bridge_exec.py "<task>"` feeds a task to this running bridge without going through Telegram and prints the final answer. It is owner-only and supports `--workspace`, `--resume`, `--model`, `--effort`, `--timeout` and `--env KEY=VALUE`.

The peer is found automatically: both bridges record themselves in the host's component registry at install time, and a checkout next to this repository (as `codex-telegram-bridge`) is recognised too. If it lives elsewhere, set `CODEX_TELEGRAM_BRIDGE_DIR=/path/to/it` in `bridge.env` and restart. Without a Codex bridge on the host the `delegate_to_codex` tool is simply not offered.

## Working together with ClaudeAsk / CodexAsk

If **[ClaudeAsk](https://github.com/maleon17/claude-ask)** or **[CodexAsk](https://github.com/maleon17/codex-ask)** is installed on the same host, the two sides connect on their own:

- **Telegram actions from the bridge.** A person who is the registered owner of a userbot instance gets that instance's Telegram tools in their bridge chat — send a message, read a chat's history, manage groups and triggers — executed in their own Telegram account through their userbot. Which instance belongs to whom is decided by the relay's owners map, not by the model: someone who owns no instance gets no tools, and nobody gets another person's. Because these calls do not come from a Telegram chat, every tool needs its target named explicitly.
- **Tasks from Jarvis to the bridge.** The userbot assistant can hand a task to this bridge (`delegate_to_bridge`) and get the answer back. Only the instance's owner can trigger that, in their own request; trigger-fired runs and other people's requests are refused by the relay.

A change in who owns which instance is picked up when the bridge restarts.

## Tests

```bash
scripts/run_tests.sh
```

The suite byte-compiles modules and runs `breaker_tests/test_*.py`. Optional cross-repository checks look for `~/telegram-ai-core`, `~/claude-jarvis` and `~/codex-jarvis` (override with `TELEGRAM_AI_CORE_DIR`, `CLAUDE_JARVIS_DIR` and `CODEX_JARVIS_DIR`) and report `SKIP` when those checkouts are absent.

## Troubleshooting

- **Bot is silent:** inspect `journalctl -u <service-name> -f`; a mistyped token shows up there as repeated `getUpdates not ok`.
- **Claude says it is not logged in:** send `/login` to the bot — each chat has its own Claude login.
- **Missing `OWNER_ID` / `TELEGRAM_BOT_TOKEN`:** check `bridge.env` and the unit's `EnvironmentFile=`.
- **`/restart` fails:** verify the sudoers rule matches `SERVICE_NAME`, including `.service`.
- **A user's login is stuck:** check for an old `claude auth login` process; `/login` can start the flow again.
- **Process-log formatting looks wrong:** test with Telegram's default theme; custom themes may render preformatted blocks differently.

## Ecosystem

Want Codex instead, or an agent callable inline from your own Telegram account? See the **[Telegram AI hub](https://github.com/maleon17/telegram-ai)**.

## License

MIT, see [LICENSE](LICENSE).

`telegram_format.py` is adapted from [hermes-agent](https://github.com/NousResearch/hermes-agent) (MIT License, Copyright (c) 2025 Nous Research); see its file header for attribution.
