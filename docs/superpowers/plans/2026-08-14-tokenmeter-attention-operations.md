# TokenMeter Attention Operations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add concise per-session attention states, machine-readable output, session receipts, adapter scaffolding, and a privacy-preserving self-hosted team view.

**Architecture:** Agent hooks store only an event name, normalized attention signal, and timestamp in existing live-session files. Pure helpers in `meter.py` merge live files with stored session aggregates; the overlay, CLI, receipts, and team payload all consume those helpers. JSON output and adapter tooling remain local and use only the standard library plus the already-installed PyYAML.

**Tech Stack:** Python 3.10+, PyQt6, PyYAML, watchdog, stdlib `json`/`argparse`, assert-based `test_tokenmeter.py`.

**Spec:** `docs/superpowers/specs/2026-08-13-attention-exports-receipts-adapters-team-design.md`

## Global Constraints

- Do not implement Context Runway, compaction prediction, or new context warnings.
- Do not store or transmit prompts, responses, last-assistant text, tool input, commands, or filenames.
- Do not add a server, account system, web dashboard, runtime process, or dependency.
- Keep status copy to `확인`, `작업`, `대기`, and `종료`; sort in that order.
- Keep the existing single-writer rule for `state.json`; machine-readable watch mode is read-only.
- Team upload may contain only aggregate counts and must not contain project, path, session ID, or event content.
- Hook failures must remain silent, best-effort, and exit zero.
- Every non-trivial branch added below must have one runnable assertion in `test_tokenmeter.py`.

---

### Task 1: Canonical session attention state and lifecycle events

**Files:**
- Modify: `tokenmeter/meter.py:31-177,348-419,421-496`
- Modify: `tokenmeter/hook.py:31-154,250-300`
- Modify: `tokenmeter/installer.py:30-66`
- Modify: `tokenmeter/services.yaml:43-208`
- Test: `test_tokenmeter.py`

**Interfaces:**
- Produces: `ATTENTION_LABELS: dict[str, str]`, `ATTENTION_ORDER: dict[str, int]`
- Produces: `session_views(state: dict, now: float | None = None) -> list[dict]`
- Produces: `attention_counts(state: dict, now: float | None = None) -> dict[str, int]`
- Live record fields: `event`, `event_at`, `attention`, `attention_at`; no event payload fields
- Consumes later: Tasks 2, 3, and 6 use only these helpers, never reimplement state rules

- [ ] **Step 1: Write the failing attention-state test**

Add a test that covers live sessions without token records, check precedence, token-driven recovery, timeout to waiting, ended sessions, and the existing 90% context threshold:

```python
def test_attention_views(tmp: Path) -> None:
    from tokenmeter.meter import attention_counts, session_views

    now = 10_000.0
    state = {
        "sessions": {
            "claude-code/a": {
                "service": "claude-code", "project": "api", "last_seen": now - 5,
                "ctx": 180_000, "ctx_win": 200_000, "totals": {"output_tokens": 10},
            },
            "codex/b": {
                "service": "codex", "project": "web", "last_seen": now - 100,
                "totals": {"output_tokens": 2},
            },
            "codex/done": {
                "service": "codex", "project": "old", "last_seen": now - 200,
                "totals": {"output_tokens": 1},
            },
        },
        "live": [
            {"service": "claude-code", "session_id": "a", "attention": "check",
             "attention_at": now - 10, "updated_at": now - 10},
            {"service": "codex", "session_id": "b", "attention": "check",
             "attention_at": now - 1, "updated_at": now - 1},
            {"service": "opencode", "session_id": "c", "project": "docs",
             "attention": "working", "attention_at": now - 40, "updated_at": now - 40},
        ],
    }
    rows = {row["key"]: row for row in session_views(state, now)}
    assert rows["claude-code/a"]["attention"] == "working"
    assert rows["codex/b"]["attention"] == "check"
    assert rows["opencode/c"]["attention"] == "waiting"
    assert rows["codex/done"]["attention"] == "done"
    assert attention_counts(state, now) == {
        "check": 1, "working": 1, "waiting": 1, "risk": 1,
    }
```

- [ ] **Step 2: Run the suite and verify the new test fails**

Run: `python3 test_tokenmeter.py`

Expected: `test_attention_views` fails because `session_views` and `attention_counts` do not exist.

- [ ] **Step 3: Implement the shared state helpers**

Add constants and one merge pass in `meter.py`. A check event wins only while it is newer than the last token; working expires after 30 seconds; missing live data means done.

```python
ATTENTION_ACTIVE_SECONDS = 30.0
ATTENTION_LABELS = {"check": "확인", "working": "작업", "waiting": "대기", "done": "종료"}
ATTENTION_ORDER = {name: i for i, name in enumerate(("check", "working", "waiting", "done"))}


def session_views(state: Dict[str, Any], now: Optional[float] = None) -> List[Dict[str, Any]]:
    now = time.time() if now is None else now
    stored = state.get("sessions") if isinstance(state.get("sessions"), dict) else {}
    live_rows = state.get("live") if isinstance(state.get("live"), list) else []
    live = {
        f"{row.get('service') or '?'}/{row.get('session_id') or '?'}": row
        for row in live_rows if isinstance(row, dict)
    }
    rows: List[Dict[str, Any]] = []
    for key in set(stored) | set(live):
        rec = stored.get(key) if isinstance(stored.get(key), dict) else {}
        active = live.get(key)
        token_at = _float(rec.get("last_seen"))
        signal = str((active or {}).get("attention") or "")
        signal_at = _float((active or {}).get("attention_at") or (active or {}).get("updated_at"))
        if active is None:
            attention = "done"
        elif signal == "check" and signal_at > 0 and signal_at >= token_at:
            attention = "check"
        elif now - max(token_at, signal_at if signal == "working" else 0.0) <= ATTENTION_ACTIVE_SECONDS:
            attention = "working"
        else:
            attention = "waiting"
        service, session_id = key.split("/", 1)
        rows.append({
            "key": key,
            "service": rec.get("service") or (active or {}).get("service") or service,
            "session_id": (active or {}).get("session_id") or session_id,
            "project": rec.get("project") or (active or {}).get("project") or "(unknown)",
            "model": rec.get("model") or (active or {}).get("model") or "",
            "effort": rec.get("effort") or "", "vendor": rec.get("vendor") or "",
            "plan": rec.get("plan") or "unknown", "started_at": rec.get("started_at") or
            (active or {}).get("started_at") or 0.0, "last_seen": token_at,
            "totals": dict(rec.get("totals") or {}), "ctx": _int(rec.get("ctx")),
            "ctx_win": _int(rec.get("ctx_win")), "sub_cost": _float(rec.get("sub_cost")),
            "attention": attention, "attention_at": signal_at, "live": active is not None,
        })
    return sorted(rows, key=lambda row: (ATTENTION_ORDER[row["attention"]], -_float(row["last_seen"])))


def attention_counts(state: Dict[str, Any], now: Optional[float] = None) -> Dict[str, int]:
    rows = [row for row in session_views(state, now) if row["live"]]
    return {
        "check": sum(row["attention"] == "check" for row in rows),
        "working": sum(row["attention"] == "working" for row in rows),
        "waiting": sum(row["attention"] == "waiting" for row in rows),
        "risk": sum(row["ctx_win"] > 0 and row["ctx"] / row["ctx_win"] >= 0.9 for row in rows),
    }
```

Keep `Meter.status()` as the only place that attaches `live` and `live_count` to persisted state.

- [ ] **Step 4: Add lifecycle-event tests before changing hooks**

Test normalization without storing payload content and verify `Stop` no longer deletes a live record:

```python
def test_hook_attention_signal_does_not_store_content(tmp: Path) -> None:
    from tokenmeter import hook

    assert hook.attention_signal("claude-code", "Stop", {}) == "check"
    assert hook.attention_signal(
        "claude-code", "Notification", {"notification_type": "auth_success"}
    ) == ""
    assert hook.attention_signal(
        "codex", "PermissionRequest", {"approvals_reviewer": "auto_review"}
    ) == "working"
    assert hook.attention_signal("opencode", "question.asked", {}) == "check"
    path = tmp / "live.json"
    secret_payload = {"tool_input": {"command": "secret-command"}}
    hook._write_live(path, "codex", "s", "/work/api", "PermissionRequest", "gpt",
                     hook.attention_signal("codex", "PermissionRequest", secret_payload))
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert "secret-command" not in json.dumps(stored)
    assert set(stored) <= {
        "service", "session_id", "project", "cwd", "model", "started_at",
        "event", "event_at", "routing_env", "attention", "attention_at",
    }
    original_live = hook.LIVE_DIR
    original_no_daemon = os.environ.get("TOKENMETER_NO_DAEMON")
    hook.LIVE_DIR = tmp
    os.environ["TOKENMETER_NO_DAEMON"] = "1"
    try:
        live = hook.live_path("claude-code", "s")
        hook._write_live(live, "claude-code", "s", "/work/api", "SessionStart", "opus", "working")
        hook.main(["hook.py", "claude-code", "Stop", "s"])
        assert live.exists(), "Stop은 턴 완료이지 세션 종료가 아니다"
        hook.main(["hook.py", "claude-code", "SessionEnd", "s"])
        assert not live.exists()
    finally:
        hook.LIVE_DIR = original_live
        if original_no_daemon is None:
            os.environ.pop("TOKENMETER_NO_DAEMON", None)
        else:
            os.environ["TOKENMETER_NO_DAEMON"] = original_no_daemon
```

- [ ] **Step 5: Normalize and atomically record hook events**

In `hook.py`, make only `SessionEnd` and OpenCode `session.deleted` stop events. Export a pure `attention_signal()` and update the live JSON atomically on every other event. Read payload fields only for routing decisions; write only normalized metadata.

```python
STOP_EVENTS = {"SessionEnd", "session.deleted"}
CHECK_EVENTS = {
    "PermissionRequest", "Stop", "permission.asked", "permission.v2.asked",
    "question.asked", "question.v2.asked", "session.idle",
}
WORK_EVENTS = {
    "SessionStart", "UserPromptSubmit", "session.created", "permission.replied",
    "permission.v2.replied", "question.replied", "question.v2.replied",
}
ATTENTION_NOTIFICATIONS = {"permission_prompt", "idle_prompt", "elicitation_dialog"}


def attention_signal(service: str, event: str, payload: dict) -> str:
    if event == "Notification":
        return "check" if _pick(payload, "notification_type") in ATTENTION_NOTIFICATIONS else ""
    if service == "codex" and event == "PermissionRequest":
        reviewer = _pick(payload, "approvals_reviewer", "approval_reviewer").lower()
        if reviewer in {"auto_review", "guardian", "guardian_subagent"}:
            return "working"
    if event in CHECK_EVENTS:
        return "check"
    return "working" if event in WORK_EVENTS else ""
```

Extend `_resolve_session_id` with an optional argv override for the generated OpenCode plugin. Change `_write_live` to preserve the original `started_at` and routing data, set `event_at`, and set `attention_at` only when `attention_signal` is non-empty. Never copy `message`, `last_assistant_message`, `tool_input`, `command`, or arbitrary payload keys.

- [ ] **Step 6: Install the richer lifecycle signals**

Update service events:

```yaml
claude-code:
  install:
    events: [SessionStart, UserPromptSubmit, PermissionRequest, Notification, Stop, SessionEnd]
codex:
  install:
    events: [SessionStart, UserPromptSubmit, PermissionRequest, Stop, SessionEnd]
```

Update the generated OpenCode plugin so `event.properties.sessionID` is passed as argv 4 and only these mappings are fired:

```javascript
const EVENTS = new Set([
  "session.created", "session.deleted", "session.idle",
  "permission.asked", "permission.v2.asked", "permission.replied", "permission.v2.replied",
  "question.asked", "question.v2.asked", "question.replied", "question.v2.replied",
])
```

Map `session.created` to `SessionStart` and `session.deleted` to `SessionEnd`; pass other names unchanged. Remove the anonymous startup fire so same-directory concurrent sessions never share a fake live file.

- [ ] **Step 7: Run the full suite and commit**

Run: `python3 test_tokenmeter.py`

Expected: all assertions pass, including installer idempotence and the two new attention tests.

```bash
git add tokenmeter/meter.py tokenmeter/hook.py tokenmeter/installer.py tokenmeter/services.yaml test_tokenmeter.py
git commit -m "feat: track per-session attention state"
```

---

### Task 2: Concise overlay state and one-shot attention notifications

**Files:**
- Modify: `tokenmeter/overlay.py:65-101,217-250,555-589,891-940`
- Modify: `tokenmeter/cli.py:852-922`
- Modify: `tokenmeter/services.yaml:43-61`
- Test: `test_tokenmeter.py`

**Interfaces:**
- Consumes: `session_views`, `ATTENTION_LABELS`, `ATTENTION_ORDER` from Task 1
- Produces: `attention_notice_key(row: dict) -> tuple[str, float]`
- User-visible narrow columns: `상태`, `프로젝트`, `모델`, `tok/s`, `ctx`

- [ ] **Step 1: Add failing display and notification assertions**

Extend overlay self-checks and the main test file:

```python
def test_attention_notice_key(tmp: Path) -> None:
    from tokenmeter.cli import attention_notice_key

    row = {"key": "codex/s", "attention": "check", "attention_at": 123.0}
    assert attention_notice_key(row) == ("codex/s", 123.0)
    assert attention_notice_key({"key": "codex/s", "attention": "working"}) == ("", 0.0)
```

In `tokenmeter.overlay._demo()`, assert the produced session rows are ordered `확인`, `작업`, `대기`, `종료` and that the narrow header is exactly five columns.

- [ ] **Step 2: Verify the assertions fail**

Run: `python3 test_tokenmeter.py && python3 -m tokenmeter.overlay`

Expected: the new assertions fail before production changes.

- [ ] **Step 3: Render the canonical state in the session panel**

Import Task 1 helpers. Change `SessionRow` to include internal key and attention, derive rows from `session_views(self.status)`, then sort by canonical state before rate and activity:

```python
views.sort(key=lambda row: (
    ATTENTION_ORDER[row["attention"]],
    -self.rates.get(row["key"], 0.0),
    -_float(row["last_seen"]),
))
```

Use the exact narrow headers and labels:

```python
SESSION_HEAD = ("상태", "프로젝트", "모델", "tok/s", "ctx")
STATE_COLORS = {"check": "#FF5F6D", "working": GOLD, "waiting": DIM, "done": "#555B6C"}
```

Keep the existing expanded-only fields after those five columns. Do not add explanations, badges, tool names, or event names to the row.

- [ ] **Step 4: Replace global quiet detection with per-session check transitions**

In `_idle_loop`, read live sessions once, attach them to a shallow status snapshot, and use `session_views` for both notifications and leaderboard sync. Keep the last notified timestamp per session and notify only a newer check event:

```python
def attention_notice_key(row: Dict[str, Any]) -> Tuple[str, float]:
    if row.get("attention") != "check":
        return "", 0.0
    return str(row.get("key") or ""), _float(row.get("attention_at"))
```

Notification text is exactly `"<project> · 확인 필요"`. Support optional `settings.attention_notify`; when absent, treat legacy `idle_notify_seconds: 0` as disabled and any other legacy value as enabled. Do not put `attention_notify` in the package defaults, so an existing user override of `idle_notify_seconds: 0` remains effective. Remove the global quiet-timer notification path but retain `idle_exit_minutes` for daemon shutdown.

- [ ] **Step 5: Run checks and commit**

Run: `python3 test_tokenmeter.py && QT_QPA_PLATFORM=offscreen python3 -m tokenmeter.overlay`

Expected: all assertions pass and the overlay self-check exits zero.

```bash
git add tokenmeter/overlay.py tokenmeter/cli.py tokenmeter/services.yaml test_tokenmeter.py
git commit -m "feat: surface concise agent attention states"
```

---

### Task 3: Machine-readable status snapshot and read-only JSONL watch

**Files:**
- Modify: `tokenmeter/cli.py:1-55,585-682,788-824,999-1065`
- Test: `test_tokenmeter.py`

**Interfaces:**
- Consumes: `session_views(state)` from Task 1
- Produces: `public_snapshot(state: dict, record_type: str = "snapshot") -> dict`
- Produces CLI: `tokenmeter status --json`, `tokenmeter watch --jsonl`
- JSON records always contain `schema_version`, `type`, and `timestamp`

- [ ] **Step 1: Add failing public-schema and CLI tests**

```python
def test_public_snapshot_removes_private_live_fields(tmp: Path) -> None:
    from tokenmeter.cli import public_snapshot

    state = {
        "updated_at": 10.0,
        "today": {"date": "2026-08-14", "totals": {"output_tokens": 7}},
        "total": {"totals": {"output_tokens": 7}},
        "sessions": {},
        "live": [{"service": "codex", "session_id": "secret-id", "project": "api",
                  "cwd": "/secret/path", "routing_env": {"OPENAI_BASE_URL": "secret"},
                  "attention": "working", "attention_at": 9.0}],
    }
    out = public_snapshot(state)
    raw = json.dumps(out)
    assert out["schema_version"] == 1 and out["type"] == "snapshot"
    assert "secret-id" not in raw and "/secret/path" not in raw and "routing_env" not in raw
    assert out["sessions"][0]["attention"] == "working"
```

Add a subprocess test with a temporary `TOKENMETER_HOME` that runs `status --json`, parses stdout as one JSON object, and asserts stderr-free exit zero.

- [ ] **Step 2: Run the suite and verify failure**

Run: `python3 test_tokenmeter.py`

Expected: failures for the missing `public_snapshot` and unrecognized `--json` flag.

- [ ] **Step 3: Implement one privacy-filtered snapshot projection**

Return only explicitly allowed data; never dump `Meter.status()` directly:

```python
def public_snapshot(state: Dict[str, Any], record_type: str = "snapshot") -> Dict[str, Any]:
    sessions = [{
        "service": row["service"], "project": row["project"], "model": row["model"],
        "effort": row["effort"], "attention": row["attention"],
        "started_at": row["started_at"], "last_seen": row["last_seen"],
        "ctx": row["ctx"], "ctx_window": row["ctx_win"], "totals": row["totals"],
    } for row in session_views(state)]
    return {
        "schema_version": 1, "type": record_type, "timestamp": time.time(),
        "updated_at": state.get("updated_at", 0.0), "today": state.get("today", {}),
        "total": state.get("total", {}), "days": state.get("days", {}),
        "projects": state.get("projects", {}), "services": state.get("services", {}),
        "models": state.get("models", {}), "vendors": state.get("vendors", {}),
        "plans": state.get("plans", {}), "endpoints": state.get("endpoints", {}),
        "sessions": sessions,
    }
```

Branch at the start of `cmd_status`: when `args.json`, print this object with `ensure_ascii=False` and return before all text output.

- [ ] **Step 4: Implement read-only JSONL polling**

Branch at the start of `cmd_watch` when `args.jsonl`. Open `Meter(config, read_only=True)`, emit one snapshot, then every 500ms call `reload()` and `status()`. Emit:

- `delta`: positive changes in the four token fields, `cost_usd`, and `calls` when `updated_at` changes.
- `attention`: the full privacy-filtered sessions list when `(service, project, started_at, attention, attention_at)` changes.
- `snapshot`: instead of a negative delta when reset is detected.

Every line is produced by one `json.dumps(record, ensure_ascii=False, separators=(",", ":"))` call and flushed. This branch never creates `MultiWatcher`, never writes state, and may run while the daemon owns the writer.

Use a small numeric diff helper; a reset is represented by returning `None`:

```python
STREAM_TOTALS = ("input_tokens", "cache_read", "cache_write", "output_tokens", "cost_usd", "calls")


def totals_delta(before: Dict[str, Any], after: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    diff = {key: float(after.get(key) or 0) - float(before.get(key) or 0) for key in STREAM_TOTALS}
    if any(value < 0 for value in diff.values()):
        return None
    return {key: int(value) if key != "cost_usd" else round(value, 10)
            for key, value in diff.items() if value > 0}
```

- [ ] **Step 5: Wire argparse and verify streaming briefly**

Add `--json` to `status` and `--jsonl` to `watch`. Verify the first stream record without leaving a process behind:

Run: `TOKENMETER_HOME="$(mktemp -d)" python3 -m tokenmeter.cli watch --jsonl | head -1 | python3 -m json.tool`

Expected: one valid object with `"type": "snapshot"`; the upstream process exits through existing `BrokenPipeError` handling.

- [ ] **Step 6: Run tests and commit**

Run: `python3 test_tokenmeter.py`

```bash
git add tokenmeter/cli.py test_tokenmeter.py
git commit -m "feat: add JSON status and watch output"
```

---

### Task 4: Latest-session receipt

**Files:**
- Modify: `tokenmeter/cli.py:560-850,999-1065`
- Test: `test_tokenmeter.py`

**Interfaces:**
- Produces: `receipt_data(state: dict) -> dict | None`
- Produces: `format_receipt(data: dict, format_name: str) -> str`
- Produces CLI: `tokenmeter receipt --format text|markdown|json`

- [ ] **Step 1: Add failing receipt assertions**

```python
def test_receipt_uses_plan_specific_money_label(tmp: Path) -> None:
    from tokenmeter.cli import format_receipt, receipt_data

    state = {"sessions": {
        "claude-code/old": {"project": "old", "last_seen": 1, "totals": {"cost_usd": 9}},
        "codex/new": {
            "service": "codex", "project": "api", "model": "gpt-5.6-sol", "effort": "high",
            "plan": "subscription", "started_at": 10, "last_seen": 70,
            "ctx": 50_000, "ctx_win": 200_000, "sub_cost": 1.0,
            "totals": {"input_tokens": 10, "cache_read": 20, "cache_write": 3,
                       "output_tokens": 7, "cost_usd": 4.0, "cache_saved_usd": 2.0, "calls": 2},
        },
    }}
    data = receipt_data(state)
    assert data and data["project"] == "api" and data["money_label"] == "API 환산 가치"
    assert "API 환산 가치 $4.00" in format_receipt(data, "text")
    assert json.loads(format_receipt(data, "json"))["type"] == "receipt"
```

- [ ] **Step 2: Run the suite and verify failure**

Run: `python3 test_tokenmeter.py`

Expected: receipt helper imports fail.

- [ ] **Step 3: Implement read-only receipt data selection**

Select `max(sessions.values(), key=last_seen)` and return only approved aggregate fields. Compute `duration_seconds`, `ctx_percent`, and `subagent_percent` from stored numbers; do not open transcripts or history content.

```python
MONEY_LABELS = {"api": "예상 사용액", "subscription": "API 환산 가치"}


def receipt_data(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    rows = [row for row in (state.get("sessions") or {}).values() if isinstance(row, dict)]
    if not rows:
        return None
    rec = max(rows, key=lambda row: float(row.get("last_seen") or 0.0))
    totals = dict(rec.get("totals") or {})
    cost = float(totals.get("cost_usd") or 0.0)
    window = float(rec.get("ctx_win") or 0.0)
    return {
        "schema_version": 1, "type": "receipt", "project": rec.get("project") or "(unknown)",
        "service": rec.get("service") or "", "model": rec.get("model") or "",
        "effort": rec.get("effort") or "", "plan": rec.get("plan") or "unknown",
        "started_at": float(rec.get("started_at") or 0.0),
        "last_seen": float(rec.get("last_seen") or 0.0),
        "duration_seconds": max(0.0, float(rec.get("last_seen") or 0.0) - float(rec.get("started_at") or 0.0)),
        "totals": totals, "money_label": MONEY_LABELS.get(str(rec.get("plan")), "API 환산가"),
        "amount_usd": cost, "ctx_percent": round(100 * float(rec.get("ctx") or 0.0) / window, 1) if window else None,
        "subagent_percent": round(100 * float(rec.get("sub_cost") or 0.0) / cost, 1) if cost else 0.0,
    }
```

- [ ] **Step 4: Add compact formatters and command**

Text output is at most five lines; Markdown is one heading plus four bullets; JSON is the same dict. Use one formatter so labels cannot diverge:

```python
def format_receipt(data: Dict[str, Any], format_name: str) -> str:
    totals = data["totals"]
    tokens = tokens_of(totals)
    minutes = int(data["duration_seconds"] // 60)
    identity = " · ".join(str(data[key]) for key in ("project", "service", "model") if data.get(key))
    amount = f"{data['money_label']} ${data['amount_usd']:.2f}"
    context = "-" if data["ctx_percent"] is None else f"{data['ctx_percent']:g}%"
    lines = [
        "TokenMeter 영수증", identity,
        f"{minutes}분 · {tokens:,} 토큰 · {int(totals.get('calls') or 0)} 호출",
        f"{amount} · 캐시 절감 ${float(totals.get('cache_saved_usd') or 0):.2f}",
        f"ctx {context} · 서브에이전트 {data['subagent_percent']:g}%",
    ]
    if format_name == "json":
        return json.dumps(data, ensure_ascii=False)
    if format_name == "markdown":
        return "\n".join(["### TokenMeter 영수증", f"- {identity}", f"- {lines[2]}",
                            f"- {lines[3]}", f"- {lines[4]}"])
    return "\n".join(lines)
```

`cmd_receipt` loads `Meter(load_config(), read_only=True).state`, returns 1 with `영수증을 만들 세션이 없습니다.` when empty, and otherwise prints exactly one format. Do not write clipboard data or files.

- [ ] **Step 5: Run tests and manual formats, then commit**

Run: `python3 test_tokenmeter.py`

Run: `python3 -m tokenmeter.cli receipt --format json | python3 -m json.tool`

Expected: valid receipt when local data exists, or the documented no-session message with exit 1.

```bash
git add tokenmeter/cli.py test_tokenmeter.py
git commit -m "feat: add session receipts"
```

---

### Task 5: Privacy-safe adapter scaffold and structural checker

**Files:**
- Create: `tokenmeter/adapter.py`
- Modify: `tokenmeter/config.py:135-172,223-233`
- Modify: `tokenmeter/cli.py:35-50,990-1080`
- Test: `test_tokenmeter.py`

**Interfaces:**
- Renames internal parser to public `parse_service(name: str, raw: dict) -> ServiceSpec`
- Produces: `redact_fixture(value: Any, key: str = "") -> Any`
- Produces: `init_adapter(name: str, log_path: Path, output: Path) -> tuple[bool, str]`
- Produces: `check_adapter(path: Path) -> tuple[bool, list[str]]`
- Produces CLI: `tokenmeter adapter init NAME --log PATH`, `tokenmeter adapter check PATH`

- [ ] **Step 1: Add failing redaction and no-overwrite tests**

```python
def test_adapter_redacts_values_and_refuses_overwrite(tmp: Path) -> None:
    from tokenmeter.adapter import init_adapter, redact_fixture

    source = {"api_key": "sk-secret", "usage": {"input_tokens": 42},
              "model": "private-model", "ok": True, "items": ["secret"]}
    clean = redact_fixture(source)
    assert clean == {"api_key": "<redacted>", "usage": {"input_tokens": "<redacted>"},
                     "model": "", "ok": False, "items": [""]}

    log = tmp / "agent.json"
    log.write_text(json.dumps(source), encoding="utf-8")
    out = tmp / "sample-adapter"
    ok, _ = init_adapter("sample", log, out)
    assert ok and (out / "service.yaml").exists() and (out / "fixture.json").exists()
    before = (out / "fixture.json").read_text(encoding="utf-8")
    ok, _ = init_adapter("sample", log, out)
    assert not ok and (out / "fixture.json").read_text(encoding="utf-8") == before
    assert "sk-secret" not in before and "private-model" not in before and "42" not in before
```

- [ ] **Step 2: Run and verify failure**

Run: `python3 test_tokenmeter.py`

Expected: `tokenmeter.adapter` import fails.

- [ ] **Step 3: Expose the existing service parser**

Rename `_parse_service` to `parse_service`, update `load_config`, and add no second parser. Existing config tests must remain green.

- [ ] **Step 4: Implement one-shot record discovery and redaction**

`adapter.py` must:

1. Accept a file or recursively find the most recently modified `*.json`/`*.jsonl` file.
2. For JSON, load one object; for JSONL, scan with `collections.deque(fh, maxlen=100)` and take the last valid object.
3. Discover likely token and context dot-paths from key aliases in memory before redaction.
4. Recursively replace secret-looking keys matching `key|token|secret|password|credential|auth|cookie` with `"<redacted>"`; replace all other strings/numbers/booleans with `""`/`0`/`false`.
5. Parse everything before creating the destination; refuse any non-empty existing destination.

Mark the deliberate one-shot scan ceiling:

```python
SECRETISH = re.compile(r"key|token|secret|password|credential|auth|cookie", re.IGNORECASE)


def redact_fixture(value: Any, key: str = "") -> Any:
    if key and SECRETISH.search(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): redact_fixture(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_fixture(item) for item in value]
    if isinstance(value, bool):
        return False
    if isinstance(value, str):
        return ""
    if isinstance(value, (int, float)):
        return 0
    return None


# ponytail: adapter init scans a selected JSONL once; switch to reverse chunk reads if multi-GB logs make this measurable.
```

- [ ] **Step 5: Write the two-file scaffold**

Write `fixture.json` atomically. Write `service.yaml` with this exact shape, filling only discovered dot-paths:

```yaml
services:
  sample:
    enabled: true
    label: sample
    roots: ["~/agent/logs"]
    patterns: ["**/*.jsonl"]
    format: jsonl
    mode: choose-delta-or-cumulative
    key: null
    match: {}
    fields:
      input: usage.input_tokens
      cache_read: null
      cache_write: null
      output: usage.output_tokens
    context:
      cwd: null
      model: model
      session: session_id
    default_model: default
    install: {target: none}
```

Add comments stating that `mode`, `key`, and `match` require confirmation. Paths under the home directory use `~` so shared drafts do not expose usernames.

- [ ] **Step 6: Implement structural checking**

`check_adapter` loads exactly one service from `service.yaml`, calls `parse_service`, rejects any mode outside `delta|cumulative`, and confirms every non-null token/context dot-path exists in `fixture.json` using `dig`. It reports concrete messages such as:

```text
mode: delta 또는 cumulative 중 하나를 선택하세요
fields.output: usage.output_tokens 경로가 fixture.json에 없습니다
```

It does not require positive token values because the fixture is intentionally anonymized.

```python
def check_adapter(path: Path) -> Tuple[bool, List[str]]:
    raw = yaml.safe_load((path / "service.yaml").read_text(encoding="utf-8")) or {}
    services = raw.get("services") if isinstance(raw, dict) else None
    if not isinstance(services, dict) or len(services) != 1:
        return False, ["service.yaml에는 services 아래 서비스가 정확히 하나 있어야 합니다"]
    name, service_raw = next(iter(services.items()))
    spec = parse_service(str(name), service_raw if isinstance(service_raw, dict) else {})
    fixture = json.loads((path / "fixture.json").read_text(encoding="utf-8"))
    errors: List[str] = []
    if spec.mode not in {"delta", "cumulative"}:
        errors.append("mode: delta 또는 cumulative 중 하나를 선택하세요")
    for group, fields in (("fields", spec.fields), ("context", spec.context)):
        for field, dot_path in fields.items():
            if dot_path and dig(fixture, dot_path) is None:
                errors.append(f"{group}.{field}: {dot_path} 경로가 fixture.json에 없습니다")
    return not errors, errors or [f"{name}: 구조 검증 통과"]
```

- [ ] **Step 7: Wire nested argparse commands and verify**

`cmd_adapter` dispatches `init` and `check`; init passes `Path.cwd() / f"{args.name}-adapter"` as the output directory. Neither action reads user configuration or writes outside that adapter directory.

Run:

```bash
tmp_dir="$(mktemp -d)"
printf '%s\n' '{"usage":{"input_tokens":1,"output_tokens":2},"model":"x"}' > "$tmp_dir/log.jsonl"
(cd "$tmp_dir" && python3 -m tokenmeter.cli adapter init sample --log "$tmp_dir/log.jsonl")
python3 -m tokenmeter.cli adapter check "$tmp_dir/sample-adapter"
```

Expected: init succeeds without exposing values; check asks only for unresolved semantic choices.

- [ ] **Step 8: Run tests and commit**

Run: `python3 test_tokenmeter.py`

```bash
git add tokenmeter/adapter.py tokenmeter/config.py tokenmeter/cli.py test_tokenmeter.py
git commit -m "feat: add privacy-safe adapter kit"
```

---

### Task 6: Self-hosted team attention view

**Files:**
- Modify: `tokenmeter/leaderboard.py:1-289,289-373`
- Modify: `tokenmeter/cli.py:560-585,895-922,999-1080`
- Test: `test_tokenmeter.py`

**Interfaces:**
- Consumes: `attention_counts(state)` from Task 1
- Existing transport remains: `Leaderboard.sync(state, force=False)` and `settings.leaderboard.*`
- Produces: `TeamEntry`, `parse_team_entries(raw, me)`, `Leaderboard.team(state)`
- Produces CLI: `tokenmeter team [--sync] [--json]`

- [ ] **Step 1: Add failing payload privacy and compatibility tests**

Extend the existing leaderboard test fixture with live sessions and assert nesting under existing JSONB:

```python
state["sessions"] = {
    "codex/s": {"service": "codex", "project": "secret-project", "last_seen": time.time(),
                "ctx": 190_000, "ctx_win": 200_000, "totals": {"cost_usd": 1.0}}
}
state["live"] = [{"service": "codex", "session_id": "s", "attention": "check",
                  "attention_at": time.time(), "cwd": "/secret/path"}]
body = payload(state, "alice")
assert body["today"]["attention"] == {"check": 1, "working": 0, "waiting": 0, "risk": 1}
raw = json.dumps(body)
assert "secret-project" not in raw and "/secret/path" not in raw and "session_id" not in raw
```

Also parse an old server row with no `attention` and assert all four values become zero.

- [ ] **Step 2: Run the suite and verify failure**

Run: `python3 test_tokenmeter.py`

Expected: missing nested attention and missing team parser failures.

- [ ] **Step 3: Extend the existing payload without a database column**

In `payload`, add only:

```python
today["attention"] = attention_counts(state)
```

Keep the existing top-level key set unchanged. Do not add live records, projects, paths, session IDs, or raw events.

- [ ] **Step 4: Normalize team rows and local fallback**

Add:

```python
@dataclass
class TeamEntry:
    handle: str
    check: int = 0
    working: int = 0
    waiting: int = 0
    risk: int = 0
    cost_usd: float = 0.0
    me: bool = False
```

`parse_team_entries` reads `row.today.attention`, defaults missing/non-numeric fields to zero, and sorts by `(-check, -risk, -working, -cost_usd, handle)`. `Leaderboard.team(state)` uses cached rows, replaces/adds the local handle with current `attention_counts(state)` and today's cost, and returns the same status note behavior as `board()`.

```python
def parse_team_entries(raw: Any, me: str) -> List[TeamEntry]:
    rows = raw.get("entries") if isinstance(raw, dict) else raw
    out: List[TeamEntry] = []
    for row in (rows if isinstance(rows, list) else []):
        if not isinstance(row, dict) or not str(row.get("handle") or "").strip():
            continue
        today = row.get("today") if isinstance(row.get("today"), dict) else {}
        attention = today.get("attention") if isinstance(today.get("attention"), dict) else {}
        out.append(TeamEntry(
            handle=str(row["handle"]), check=int(_num(attention.get("check"))),
            working=int(_num(attention.get("working"))), waiting=int(_num(attention.get("waiting"))),
            risk=int(_num(attention.get("risk"))), cost_usd=cost_of(today),
            me=str(row["handle"]) == me,
        ))
    return sorted(out, key=lambda entry: (
        -entry.check, -entry.risk, -entry.working, -entry.cost_usd, entry.handle,
    ))
```

- [ ] **Step 5: Ensure daemon sync receives live state without duplicate scans**

In `_idle_loop`, call `meter.live_sessions()` once per tick, attach it to a shallow copy of `meter.state`, set `live_count`, and pass that snapshot to `board.sync`. Reuse the same snapshot for Task 2 notifications. Do not persist `live` inside `state.json`.

- [ ] **Step 6: Add team text and JSON command**

`tokenmeter team` prints only:

```text
핸들  확인  작업  대기  위험  오늘
```

`--sync` calls `sync(state, force=True)` only when endpoint is configured. `--json` emits the following normalized shape with no cached raw server object:

```json
{"schema_version":1,"type":"team","timestamp":0,"members":[{"handle":"alice","check":1,"working":3,"waiting":1,"risk":1,"cost_usd":12.4,"me":true}]}
```

- [ ] **Step 7: Run tests and commit**

Run: `python3 test_tokenmeter.py && python3 -m tokenmeter.leaderboard`

```bash
git add tokenmeter/leaderboard.py tokenmeter/cli.py test_tokenmeter.py
git commit -m "feat: add self-hosted team attention view"
```

---

### Task 7: User documentation and release-level verification

**Files:**
- Modify: `README.md:1-100`
- Modify: `README.ko.md:1-100`
- Modify: `docs/reference.ko.md:1-430,473-552`
- Modify: `docs/add-service.md:1-183`
- Modify: `skills/tokenmeter/SKILL.md:1-80`

**Interfaces:**
- Documents the commands and privacy contract produced by Tasks 1-6
- No production interface changes in this task

- [ ] **Step 1: Update the concise product promise and command lists**

Add `확인/작업/대기/종료` to the session description. Document exactly:

```bash
tokenmeter status --json
tokenmeter watch --jsonl
tokenmeter receipt --format markdown
tokenmeter adapter init gemini-cli --log ~/.gemini/tmp
tokenmeter adapter check ./gemini-cli-adapter
tokenmeter team --sync
```

Keep the README short; detailed JSON schemas and adapter cautions belong in `docs/reference.ko.md` and `docs/add-service.md`.

- [ ] **Step 2: Document privacy and compatibility**

State that attention files store event name/time only, public JSON omits paths and session IDs, adapter fixtures erase values, and team sync sends only aggregate state counts nested in `today`. State that team mode uses the existing self-hosted endpoint and does not provide a hosted TokenMeter service.

- [ ] **Step 3: Update the bundled agent skill**

Add receipt, adapter, and team commands to the existing quick-command table. Do not add new slash-command files; `/tm` can invoke the installed CLI directly when users ask for these functions.

- [ ] **Step 4: Run all verification commands**

Run:

```bash
python3 test_tokenmeter.py
QT_QPA_PLATFORM=offscreen python3 -m tokenmeter.overlay
python3 -m tokenmeter.leaderboard
python3 -m tokenmeter.history
python3 -m tokenmeter.cli --help
python3 -m tokenmeter.cli status --json | python3 -m json.tool
python3 -m build
```

Expected: every command exits zero. If `python3 -m build` is unavailable in the development interpreter, run the repository-supported isolated equivalent `uv build` and record that command in the final handoff.

- [ ] **Step 5: Check forbidden leakage and working tree scope**

Run:

```bash
rg -n 'last_assistant_message|tool_input|permission_suggestions' tokenmeter README.md README.ko.md docs
git diff --check
git status --short
```

Expected: forbidden payload names appear only in documentation stating they are excluded, `git diff --check` is clean, and only planned files are modified.

- [ ] **Step 6: Commit documentation**

```bash
git add README.md README.ko.md docs/reference.ko.md docs/add-service.md skills/tokenmeter/SKILL.md
git commit -m "docs: explain attention and operations features"
```
