# TokenMeter

**See when your AI coding agents are working, waiting, or running out of context.**

TokenMeter is a local-first desktop meter for Claude Code, Codex, and OpenCode. It discovers active sessions, shows live output speed and context pressure, keeps cost history, and lets you know when token flow stops.

[한국어](README.ko.md) · [Advanced reference](docs/reference.ko.md) · [Add an agent](docs/add-service.md)

```text
┌────────────────────────────────────────────────────┐
│ TOKENMETER                                    TODAY │
│ 478 output tok/s                         ≈ $15.8599 │
│ ██████████████░░░░░░░░░░░░░░░░░░░░░░░░           ▏ │
│ in 1.2M         out 84.0k      cache 23.5M         │
├────────────────────────────────────────────────────┤
│ LIVE SESSIONS                                        │
│ api-server     opus-5      412/s      ctx 31%       │
│ web-client     gpt-5.6      63/s      ctx 75%       │
│ mobile         sonnet-5      3/s      ctx 95%  ⚠    │
└────────────────────────────────────────────────────┘
```

## Why TokenMeter

- **Know what is actually running.** Live sessions are sorted by output speed across supported agents.
- **See compaction coming.** Context pressure changes color at 70% and 90%.
- **Understand usage locally.** Inspect tokens, estimated API-equivalent cost, cache savings, projects, models, and daily history.
- **Stop watching terminals.** A desktop notification fires after an active session becomes quiet.

TokenMeter reads local agent logs. It does not require an API key, store prompt contents in its state, or make network requests by default.

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

Adding another log-based agent is configuration-only. See [the service guide](docs/add-service.md).

## Commands

```bash
tokenmeter status                 # usage, history, sessions
tokenmeter services               # detected logs and hook state
tokenmeter doctor                 # validate parsers and installation
tokenmeter meter off              # hide overlay; keep measuring
tokenmeter off                    # stop measuring; keep hooks
tokenmeter on                     # resume measurement
tokenmeter uninstall              # remove only TokenMeter hooks
```

The overlay supports drag to move, wheel to resize, `S/M/L` display sizes, double-click to switch views, and right-click for all controls.

## Agent skill

The optional skill lets compatible coding agents operate TokenMeter in natural language.

```bash
npx skills add Oct7/tokenmeter -g -a claude-code
```

It provides `/tm`, `/tm-meter`, `/tm-measure`, and `/tm-doctor`.

## Privacy and data

- Prompt text, project paths, and session contents are never uploaded.
- Runtime state lives in `~/Library/Application Support/tokenmeter` on macOS or `${XDG_STATE_HOME:-~/.local/state}/tokenmeter` on Linux.
- User overrides live in `${XDG_CONFIG_HOME:-~/.config}/tokenmeter`.
- The optional self-hosted leaderboard is disabled by default. Enabling it uploads only the aggregates documented in [the advanced reference](docs/reference.ko.md#글로벌-랭킹-붙이기).

## Update or remove

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
