# TokenMeter

**See when your AI coding agents are working, waiting, or running out of context.**

TokenMeter is a local-first desktop meter for Claude Code, Codex, and OpenCode. It discovers active sessions, shows `확인` (needs attention), `작업` (working), `대기` (waiting), or `종료` (done), and keeps usage history locally.

[한국어](README.ko.md) · [Advanced reference](docs/reference.ko.md) · [Add an agent](docs/add-service.md)

```text
┌────────────────────────────────────────────────────┐
│ TOKENMETER                                    TODAY │
│ 478 total output tok/s       API-equivalent $15.8599 │
│ ██████████████░░░░░░░░░░░░░░░░░░░░░░░░           ▏ │
│ in 1.2M         out 84.0k      cache 23.5M         │
├────────────────────────────────────────────────────┤
│ STATUS  PROJECT        MAIN/s      TOTAL    CONTEXT  │
│ 작업    api-server       412/s      84.0k      31%  │
│ 대기    web-client          —      21.7k      75%  │
│ 확인    mobile              3/s       8.1k  95% · high │
└────────────────────────────────────────────────────┘
```

## Why TokenMeter

- **Know what needs attention.** Sessions use the actual UI labels `확인`, `작업`, `대기`, and `종료`.
- **See context pressure.** Context pressure changes color at 70% and 90%; Context Runway or compaction prediction is not implemented.
- **Understand usage locally.** Inspect tokens, estimated API-equivalent cost, cache savings, projects, models, and daily history.
- **Stop watching terminals.** A desktop notification fires only on an explicit transition to `확인`.

TokenMeter reads local agent logs. It does not require an API key or store prompt contents. Metering stays local. The optional quota view uses already-logged-in Claude, Codex, or Grok credentials to read remaining plan windows.

## Install

Requirements: macOS or Linux and Python 3.10+. [uv](https://docs.astral.sh/uv/) is recommended because it installs TokenMeter in an isolated environment.

```bash
uv tool install git+https://github.com/Oct7/tokenmeter.git
tokenmeter install
```

Using pipx instead:

```bash
pipx install git+https://github.com/Oct7/tokenmeter.git
tokenmeter install
```

Then activate your first measurement:

1. Fully restart Claude Code, Codex, or OpenCode.
2. Run one new prompt.
3. Confirm the overlay appears or run `tokenmeter status`.
4. If nothing appears, run `tokenmeter doctor`.

Existing TokenPet hooks and local data are detected and copied safely. The old files are not deleted.

## Supported agents

| Agent | Local usage | Automatic lifecycle hook |
|---|---:|---:|
| Claude Code | Yes | Yes |
| Codex | Yes | Yes |
| OpenCode | Yes | Yes, generated plugin |
| Grok CLI | Yes | Yes, dedicated `~/.grok/hooks` plus Claude-compat remap when the session id matches |

Adding another log-based agent is configuration-only. See [the service guide](docs/add-service.md).

## Commands

```bash
tokenmeter status --json
tokenmeter watch --jsonl
tokenmeter receipt --format markdown
tokenmeter adapter init gemini-cli --log ~/.gemini/tmp
tokenmeter adapter check ./gemini-cli-adapter
tokenmeter team --sync
tokenmeter quota                  # remaining Claude/Codex/Grok plan windows
tokenmeter services               # detected logs and hook state
tokenmeter doctor                 # validate parsers and installation
tokenmeter meter off              # hide overlay; keep measuring
tokenmeter update on              # opt in to daily stable-release updates
tokenmeter off                    # stop measuring; keep hooks
tokenmeter on                     # resume measurement
tokenmeter uninstall              # remove only TokenMeter hooks
```

Drag the overlay to move it. The wheel scrolls when it is over a list row and resizes the window elsewhere. Use the visible `S/M/L` controls to switch between simple (meter only), normal (Sessions, Projects, Quota), and detail (adds Speed and Daily). `⌘K`/`Ctrl+K` is an optional quick search. Theme, reduced transparency, and reduced motion live under `⋯` or the right-click menu. `×` hides only the overlay, so measurement continues. To stop measurement, choose `TokenMeter 종료 · 측정 중지` from that menu or the tray.

The global `tokens/s` meter is aggregate output throughput, including sub-agents. Session and model `tok/s` exclude sub-agent output and show the main model's throughput; these rates are log-delta arrival rates, not a benchmark of the provider's streaming generation speed. Session rows keep status, cumulative output, and context usage in separate columns. The optional Team tab is hidden while its endpoint is offline.

## Agent skill

The optional skill lets compatible coding agents operate TokenMeter in natural language.

```bash
npx skills add Oct7/tokenmeter -g -a claude-code
```

It provides `/tm`, `/tm-meter`, `/tm-measure`, and `/tm-doctor`.

## Privacy and data

- Local live files contain allowlisted session/routing metadata plus normalized event and attention timestamps. They never contain prompt or response text, tool commands, or filenames.
- Public JSON and team output apply stricter filters, omitting internal paths, session IDs, routing URLs, and session content. Adapter fixtures erase values before writing them.
- Runtime state lives in `~/Library/Application Support/tokenmeter` on macOS or `${XDG_STATE_HOME:-~/.local/state}/tokenmeter` on Linux.
- User overrides live in `${XDG_CONFIG_HOME:-~/.config}/tokenmeter`.
- The optional quota view and `tokenmeter quota` read remaining plan windows from Claude, Codex, or Grok using credentials already stored by those CLIs. Session logs and prompt contents are not sent.
- The optional self-hosted leaderboard is disabled by default. Team sync reuses that endpoint; its only added live-session data is aggregate attention counts under `today`. TokenMeter does not host the service.

## Update or remove

Automatic updates are off by default. Opt in to check once per day when the daemon starts, or update immediately:

```bash
tokenmeter update on
tokenmeter update now
```

Only stable GitHub Releases are installed. Turn it back off with `tokenmeter update off`.

Manual update:

```bash
uv tool install --force git+https://github.com/Oct7/tokenmeter.git
tokenmeter install                # refresh absolute hook paths
```

Remove hooks before removing the isolated tool:

```bash
tokenmeter uninstall
uv tool uninstall oct7-tokenmeter
```

## Development

```bash
git clone https://github.com/Oct7/tokenmeter.git
cd tokenmeter
uv sync
uv run python test_tokenmeter.py
uv run tokenmeter install --dry-run
```

TokenMeter is licensed under the [MIT License](LICENSE).
