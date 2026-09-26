# Kitty Remote

[中文](README.md) | English

View, control and open **Kitty terminal windows** on your Ubuntu desktop from a phone browser. Tasks keep running in their original processes on the computer; the phone reaches a local agent through a relay you host yourself.

The first version provides text snapshots, full history reading, multi-line CJK input, a key bar, new desktop windows, login, device pairing and revocation. New windows start a plain shell in the agent user's `~`. It is text-only by design; Kitty 0.49's native screenshots are planned for later.

## How it works

```
Phone browser ⇄ HTTPS/WSS ⇄ your relay ⇄ WSS (outbound from the PC) ⇄ local agent ⇄ Kitty remote-control socket ⇄ windows already running
```

- **No preparation needed**: attach to Kitty windows that are already running. No tmux, no wrapper command to start tasks with. Claude Code, Codex and plain shells all work the same way.
- **Only changed rows travel**: the agent reads a window's full buffer about once a second and sends only the rows that changed. The phone patches those rows in place, so your reading position never jumps.
- **Input hits exactly the intended window**: window identity checks (stale targets are rejected when a window ID is reused), request de-duplication, receipts that distinguish "submitted" from "uncertain", and a single controller per window.
- **Self-hosted relay**: the computer connects out to your server. No inbound port on the computer and no third-party tunnel.

**Status**: the author runs it daily on a self-hosted server over public HTTPS. New desktop windows and the native-scrolling terminal view (only changed rows are replaced; no separate reading/following modes) pass real end-to-end acceptance against an isolated Kitty instance, locally and over the public network. Real Android devices and full Codex/Claude task and approval flows still need dedicated acceptance.

The detailed documentation under `docs/` is currently written in Chinese.

## Run locally

Requirements: Ubuntu desktop, Python 3.12, uv, Node.js 24 LTS and Kitty. Accepted with Kitty / kitten 0.49.0.

From the project root, install dependencies, build the web app and create the single administrator:

```bash
uv sync --locked --python 3.12
npm ci --prefix web
npm run build --prefix web
uv run kitty-remote create-admin --username admin
```

The password is entered interactively and must be at least 12 characters. SQLite lives at `.state/relay.sqlite3` by default.

Start the relay and keep it running:

```bash
KR_DEV=1 KR_ORIGIN=http://127.0.0.1:8765 uv run kitty-remote relay
```

Open **http://127.0.0.1:8765** and log in. In another terminal, start pairing:

```bash
uv run kitty-remote pair --relay http://127.0.0.1:8765 --allow-http --name 'My Ubuntu'
```

In the web app, click "Pair computer", enter the 8-character code printed by the command, check the name and confirm. The command then claims its credentials and writes them to `~/.config/kitty-remote/agent.json` with mode `0600`.

Then start the agent:

```bash
uv run kitty-remote doctor
uv run kitty-remote agent
```

Pick a window in the web app to read and write it. Plain HTTP is only allowed for localhost during development; use the HTTPS/WSS deployment for phones on other networks.

## Connecting Kitty

Run `doctor` first to check the existing configuration. Any instance with a remote-control socket can be attached. Kitty Remote tags windows with its own user variable and does not interfere with other tools.

Example configuration for `~/.config/kitty/kitty.conf`:

```conf
allow_remote_control socket-only
listen_on unix:${XDG_RUNTIME_DIR}/kitty-remote-{kitty_pid}
```

Enabling a listener usually requires starting a new Kitty instance. Plan around the tasks already running; this project never closes or restarts terminals.

The default discovery pattern is `$XDG_RUNTIME_DIR/kitty-remote-*`. If your sockets use a different name, point at them explicitly:

```bash
uv run kitty-remote doctor --socket unix:/absolute/path/kitty.sock
uv run kitty-remote agent --socket unix:/absolute/path/kitty.sock
```

`--socket` can be repeated; when given, only the listed addresses are used. You can also add `--socket-glob '/absolute/path/kitty-*'`, which replaces the default `kitty-remote-*` pattern. Run the agent as the desktop user; sockets never leave the machine.

## Using it on a phone

- Pick a window under a device. Each window has exactly one controlling browser; other connections are read-only and can request control once the controller leaves.
- Enter inserts a newline in the input box. "Send text" only pastes; "Send + Enter" sends Enter separately. The box is cleared once the message is handed to the connection; unsent drafts are kept per window.
- Sending is disabled while an input method is composing. Voice input works through the phone keyboard's own speech-to-text.
- Two rows of keys are always available: Esc, Tab, Shift+Tab, Enter, Ctrl+C, the arrow keys and Backspace. "More keys" adds Home/End, PageUp/PageDown, editing keys, Ctrl/Alt shortcuts and F1–F12, always targeting the selected window explicitly.
- Selecting a window loads all history Kitty currently keeps and shows it as one block of text with native scrolling. Scrolling only reads local content and never requests pages. After that only changed rows are replaced. At the bottom the view follows new output; scrolled up, it stays put and shows "回到最新 ↓" (back to latest), which becomes "有新输出 ↓" (new output) when something arrives. Font-size changes keep the row you are reading; wide rows scroll horizontally; CJK characters occupy two cells, as in the terminal.
- "Pinned layout" is opt-in: the terminal, input box and send buttons stay on screen, while devices, extra keys and receipts open as drawers; the layout follows the visible height when the soft keyboard opens. It has to be re-enabled after a page reload.
- PageUp/PageDown are keys sent to the program on the computer, unlike local scrolling on the phone. History that a full-screen program keeps to itself is not in Kitty's buffer: for example, Claude Code with `"tui": "fullscreen"` runs on the alternate screen, so Kitty only holds the current screen. Use `/tui default` in Claude Code, or `codex --no-alt-screen`, to keep history in Kitty's scrollback.
- "Submitted to Kitty" only means the control call returned; watch the screen for progress. For "uncertain" results, check the screen first — nothing is ever re-sent automatically.
- "Revoke device" invalidates the device credentials immediately. To reuse the computer, move the old credential file away and pair again.

Restarting the agent, a changed Kitty instance, a closed window or a lost identity marker all invalidate the old target. The computer keyboard still works, so avoid editing the same line from both ends at once.

## Checks and acceptance

```bash
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run pytest -q
npm run typecheck --prefix web
npm test --prefix web
npm run build --prefix web
```

Real desktop acceptance:

```bash
uv run playwright install chromium
uv run python scripts/e2e.py
```

The script creates, and finally closes, an isolated Kitty instance with two test windows, a temporary relay, an agent and a headless browser. It only types into its own test windows. Results and phone/desktop screenshots go to `artifacts/` (git-ignored). Set `KR_CHROMIUM_PATH` to use an existing Chromium.

After upgrading Kitty, regenerate the character width table with `kitty +launch scripts/gen_widths.py`.

## Docs and layout

- [Implementation plan and status](docs/implementation-plan.md)
- [Verification record and open items](docs/verification.md)
- [Deployment, service install and removal](docs/deployment.md)
- [Protocol v1](docs/protocol-v1.md)
- [Initial design draft (historical)](docs/design-background.md)

| Part | Location |
| --- | --- |
| Shared models, ANSI filtering, bounded queues | `src/kitty_remote/protocol.py` |
| Kitty identity checks, command wrapper | `src/kitty_remote/kitty.py` |
| Agent, input de-duplication | `src/kitty_remote/agent.py` |
| Relay, sessions, leases and routing | `src/kitty_remote/relay.py` |
| SQLite, passwords and credential digests | `src/kitty_remote/storage.py` |
| Admin commands | `src/kitty_remote/cli.py` |
| React / TypeScript; terminal rows and diffs in `web/src/screen.ts` | `web/` |
| Docker, Caddy and systemd templates | `deploy/` |

Limitations: text mode does not reproduce images, animations or every font feature; complex emoji, ligatures and wide characters should still be checked against the programs you use. History has no fixed line limit, but total UTF-8 size is capped at 16 MiB and each WebSocket message at 1 MiB; content is chunked automatically, and exceeding the cap is reported explicitly rather than truncated. History Kitty has already discarded cannot be recovered. The relay is trusted and can see the content, but by default it does not persist terminal text or input. The relay runs as a single process and does not support multi-replica routing.

## How it differs from similar projects

As of 2026-09:
- **Also built on Kitty remote control**: [kitty-remote-deck](https://github.com/identxxy/kitty-remote-deck) and [term-deck](https://github.com/adrian-wulf/term-deck). Both resend the full text on every refresh and pause updates while you read history.
- **Phone clients for AI coding agents**: [Happy](https://github.com/slopus/happy), [VibeTunnel](https://github.com/amantus-ai/vibetunnel), [handmux](https://github.com/handmux/handmux), [itwillsync](https://github.com/shrijayan/itwillsync) and others. They require tasks to be started through their own wrapper command or inside tmux.
- **Official options**: Claude Code [Remote Control](https://code.claude.com/docs/en/remote-control) covers a single Claude session through a chat interface; Codex [remote connections](https://developers.openai.com/codex/remote-connections) currently require a Mac running the Codex App as the host.

## License

[MIT](LICENSE)
