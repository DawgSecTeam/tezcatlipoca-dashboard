# Console

Drop into a full `sliver-client` PTY inside the app — the same CLI you
would run locally, but proxied through the C3rberus backend.

## When to use

- Anything that needs raw Sliver commands not exposed by the higher
  level UI: `socks5 start`, `portfwd add`, `armory install`,
  `loot import`, etc.
- Debug a Sliver server quirk without leaving the operator console.
- Operator-side configuration changes (`profiles new`, `jobs`, …).

For interactive shells on a specific target, use the
[Targets](c2-targets#shell) page's per-box SHELL button — it goes
through the lighter-weight `SliverHub` shell fan-out, not a full
console.

## Prerequisites

- `sliver-client` binary present on the C3rberus host
  (`SLIVER_CLIENT_BIN` config key, default `"sliver-client"`).
- Operator config pre-imported under `~/.sliver-client/configs/`
  (run `sliver-client import <cfg>` once during initial setup —
  see [Sliver setup § operator-cert](sliver-setup#operator-cert)).
- Logged in as admin or red team (blue → `ws.close(4003)`).
- `DEV_MODE=False` (in `DEV_MODE` the console responds with a stub
  message and closes — see [#dev-mode](#dev-mode)).

## Open-console

Open `/c2/console`. Click **OPEN CONSOLE**
(`data-testid="open-console-btn"`).

A new WebSocket connects to `/c2/sliver-console?token=`. The backend
opens a PTY pair (`pty.openpty()`), spawns `sliver-client console` as
a child with the slave fd plumbed into stdin/stdout/stderr, and pumps
bytes both ways:

- `master_fd → ws` via `loop.run_in_executor(None, os.read, fd, 4096)`.
- `ws → master_fd` via `os.write(fd, data.encode())`.

The terminal lands in the bottom-of-screen tab strip with the
`terminal_settings` icon and a `[C] Console` label.

Button label tracks state:

- **OPEN CONSOLE** — no console open yet.
- **CONSOLE ACTIVE** — console tab is the focused terminal
  (`data-testid="console-active-badge"` visible).
- **RESTORE CONSOLE** — console is minimized; clicking restores it.

## Config-import

The console will only start if the backend can find an operator config
in `~/.sliver-client/configs/`. Initial setup:

```bash
# On the C3rberus host:
sliver-client import sliver_operator.cfg
ls ~/.sliver-client/configs/   # should list the .cfg
```

The `SLIVER_CFG_PATH` env var (`core/config.py`, default
`"./sliver_operator.cfg"`) only affects backend-side config — the
client itself loads from `~/.sliver-client/configs/`.

## Resize

The terminal sends a JSON control frame on every xterm resize:

```json
{"type": "resize", "cols": 220, "rows": 50}
```

The backend handles this with
`fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))`.

Initial size at spawn: 50 × 220.

## Dev-mode

When `core.config.DEV_MODE=True`, the console route sends a stub
message:

```
[DEV MODE] Sliver console not available
```

…then closes the WS cleanly. No subprocess spawned. To use the real
console, set `DEV_MODE=False` in `.env` and restart the backend.

## What you'll see

- The standard Sliver prompt (`sliver >`).
- xterm renders the PTY output exactly as the local CLI would.
- Drag handles on the terminal pane resize it; the resize JSON frame
  fires on every drag stop.
- The `[L]` / `[R]` tab strip buttons — or a drag, or the
  `Ctrl+Shift+L`/`R` keyboard chord — assign the console to the left
  or right split pane. Right-click for Copy/Paste, even mid-disconnect.
  Full mechanics on
  [Keyboard Shortcuts § terminal-split-panes](keyboard-shortcuts#terminal-split-panes).

## Troubleshooting

- **Console opens then immediately closes** — `sliver-client` binary
  missing or operator config not imported. Tail
  `logs/fastapi_backend_<ts>.log` for `FileNotFoundError` or
  `import failed`.
- **`[DEV MODE] ...` message** — `DEV_MODE=True` in `.env`. Flip to
  `False`, restart backend.
- **Blue team can't open** — by design; blue is blocked at the WS
  handshake (`close(4003)`). The button is hidden in nav for blue.
- **Console disconnects on resize** — should not happen; if it does,
  check the control frame is parseable JSON (frontend bug, not
  backend).
- **Backslashes break commands** — Sliver's input quoting uses
  `shlex.split(line, posix=False)` for parsing in
  `sliver_shell_dispatch.py`, but the console PTY passes raw bytes,
  so this is a non-issue at the console level.

<!-- audited 2026-06-05 — PTY contract, resize control frame, DEV_MODE stub all verified against c2/sliver_console.py per wiki/subsystems.md. -->
