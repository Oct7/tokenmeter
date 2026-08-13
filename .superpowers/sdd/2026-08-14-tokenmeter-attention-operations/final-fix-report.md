# TokenMeter final-review fix report

Date: 2026-08-14

## Finding map

1. **Public JSON session allowlist**
   - Fix: `public_snapshot()` now emits only `session_views()` rows with `live == true`. Each session is restricted to `service`, `project`, `model`, `attention`, `started_at`, `last_seen`, `attention_at`, `ctx`, and `ctx_window`; completed sessions, effort, totals, IDs, paths, and routing data are excluded.
   - Assertion: `test_public_snapshot_removes_private_live_fields` checks the exact session key set and absence of completed/private values, session IDs, paths, routing data, effort, and totals.

2. **Adapter content-like object keys**
   - Fix: `redact_fixture()` rejects every object key that does not fully match `[A-Za-z_][A-Za-z0-9_]*`, including keys nested below secretish objects/lists. `init_adapter()` converts the failure to a concise message before creating the output directory and does not echo the unsafe key.
   - Assertion: `test_adapter_rejects_content_like_object_keys_before_writing` covers nested filename-, path-, and command-like keys below a secretish parent and proves no output directory/files exist.

3. **Invalid UTF-8 adapter input**
   - Fix: JSON and JSONL reads normalize `UnicodeDecodeError` to the existing `로그를 읽을 수 없습니다` failure.
   - Assertion: `test_adapter_parse_failures_create_no_files` writes invalid UTF-8 in both formats, checks the normal error, and proves no output exists.

4. **Adapter root from requested log path**
   - Fix: generated roots now use the requested directory or requested file's parent, use generic `**/*.json` or `**/*.jsonl`, and shorten roots inside the home directory to `~`.
   - Assertion: `test_adapter_uses_requested_log_root_and_home_shorthand` checks file, directory, generic pattern, source-filename absence, and fake-home normalization without exposing a username.

5. **Receipt token four-way breakdown**
   - Fix: text and Markdown keep their five-line / heading-plus-four-bullet limits while showing `입력`, `캐시 읽기`, `캐시 쓰기`, and `출력` separately. JSON remains the structured receipt.
   - Assertion: `test_receipt_uses_plan_specific_money_label` compares exact text and Markdown output for all four labels/counts and parses JSON.

6. **Broken local-state degradation**
   - Fix: shared numeric conversion rejects booleans, invalid values, NaN, and infinity; session containers/rows/totals, keys without `/`, timestamps, context fields, receipt fields, and `sessions_today()` now fall back safely.
   - Assertions: `test_status_json_is_one_clean_public_object` exercises an actual `status --json` subprocess with malformed local state. `test_read_only_views_degrade_on_malformed_session_state` covers invalid rows/numbers/totals/context, slashless keys, non-dict sessions, finite standard JSON, receipt fallback, and `sessions_today()`.

7. **Remote team numeric normalization**
   - Fix: remote attention counts are finite non-negative integers; costs are finite non-negative floats. Booleans, negative values, NaN, both infinities, and invalid objects fall back to zero.
   - Assertion: `test_leaderboard_offline_and_ranking` covers every invalid class plus valid numeric/string values and serializes normalized entries with `allow_nan=False`.

8. **README accuracy/privacy**
   - Fix: English/Korean READMEs describe the local live-file allowlist and stricter public/team filters, say notifications fire only on explicit `확인`, use the real Korean labels in English copy, and clarify that attention aggregates are the only added live-session team data.
   - Assertion: the focused documentation command below checks all required phrases.

9. **Korean reference sorting/colors and upload example**
   - Fix: `docs/reference.ko.md` now documents state-first sorting, state-specific colors, and `today.attention` in the upload JSON example.
   - Assertion: the focused documentation command below checks state order, colors, and the attention key sequence.

10. **Adapter check success details**
    - Fix: successful checks list connected non-null token field names and dot-paths, never fixture values.
    - Assertion: `test_adapter_check_requires_confirmed_mode_and_cli_runs` compares the exact concise success line.

## Files changed

- `tokenmeter/adapter.py`
- `tokenmeter/cli.py`
- `tokenmeter/leaderboard.py`
- `tokenmeter/meter.py`
- `test_tokenmeter.py`
- `README.md`
- `README.ko.md`
- `docs/reference.ko.md`
- `.superpowers/sdd/2026-08-14-tokenmeter-attention-operations/final-fix-report.md`

No dependency, plan, binding spec, or SDD ledger changes were made.

## Focused verification

Exact command:

```bash
.venv/bin/python -c '
import tempfile
from pathlib import Path
import test_tokenmeter as t
names = (
 "test_adapter_redacts_values_and_refuses_overwrite",
 "test_adapter_check_requires_confirmed_mode_and_cli_runs",
 "test_adapter_redacts_nested_arrays_and_scalar_values",
 "test_adapter_directory_uses_most_recent_log",
 "test_adapter_uses_requested_log_root_and_home_shorthand",
 "test_adapter_rejects_content_like_object_keys_before_writing",
 "test_adapter_parse_failures_create_no_files",
 "test_public_snapshot_removes_private_live_fields",
 "test_status_json_is_one_clean_public_object",
 "test_read_only_views_degrade_on_malformed_session_state",
 "test_leaderboard_offline_and_ranking",
 "test_team_cli_outputs_allowlisted_local_entry",
 "test_receipt_uses_plan_specific_money_label",
)
for name in names:
    with tempfile.TemporaryDirectory(prefix="tokenmeter-focused-") as directory:
        getattr(t, name)(Path(directory))
    print(f"PASS {name}")
'
```

Output:

```text
PASS test_adapter_redacts_values_and_refuses_overwrite
PASS test_adapter_check_requires_confirmed_mode_and_cli_runs
PASS test_adapter_redacts_nested_arrays_and_scalar_values
PASS test_adapter_directory_uses_most_recent_log
PASS test_adapter_uses_requested_log_root_and_home_shorthand
PASS test_adapter_rejects_content_like_object_keys_before_writing
PASS test_adapter_parse_failures_create_no_files
PASS test_public_snapshot_removes_private_live_fields
PASS test_status_json_is_one_clean_public_object
PASS test_read_only_views_degrade_on_malformed_session_state
PASS test_leaderboard_offline_and_ranking
PASS test_team_cli_outputs_allowlisted_local_entry
PASS test_receipt_uses_plan_specific_money_label
```

Exact documentation assertion command:

```bash
.venv/bin/python -c '
import re
from pathlib import Path
english = Path("README.md").read_text(encoding="utf-8")
korean = Path("README.ko.md").read_text(encoding="utf-8")
reference = Path("docs/reference.ko.md").read_text(encoding="utf-8")
assert "`확인` (needs attention), `작업` (working), `대기` (waiting), or `종료` (done)" in english
assert "only on an explicit transition to `확인`" in english
assert "Local live files contain allowlisted session/routing metadata" in english
assert "only added live-session data is aggregate attention counts" in english
assert "명시적으로 `확인`으로 전환될 때만" in korean
assert "허용된 세션·라우팅 메타데이터" in korean
assert "새 라이브 세션 정보로는 `today` 안의 관심 상태 집계만 추가" in korean
assert "확인 → 작업 → 대기 → 종료" in reference
assert "확인은 빨강, 작업은 금색, 대기는 회색, 종료는 짙은 회색" in reference
assert re.search(r"attention.{0,20}check.{0,20}working.{0,20}waiting.{0,20}risk", reference)
print("PASS README privacy/labels/notifications and reference sorting/colors/today.attention")
'
```

Output:

```text
PASS README privacy/labels/notifications and reference sorting/colors/today.attention
```

## Release verification

### Full self-test

Command:

```bash
.venv/bin/python test_tokenmeter.py
```

Relevant output:

```text
TokenMeter 자가 검증 — 63개
  ✓ test_adapter_uses_requested_log_root_and_home_shorthand
  ✓ test_adapter_rejects_content_like_object_keys_before_writing
  ✓ test_adapter_parse_failures_create_no_files
  ✓ test_leaderboard_offline_and_ranking
  ✓ test_public_snapshot_removes_private_live_fields
  ✓ test_status_json_is_one_clean_public_object
  ✓ test_read_only_views_degrade_on_malformed_session_state
  ✓ test_watch_jsonl_reads_daemon_state_and_emits_changes
  ✓ test_receipt_uses_plan_specific_money_label
전부 통과 (63개)
```

### Module self-checks

Commands and outputs:

```text
$ QT_QPA_PLATFORM=offscreen .venv/bin/python -m tokenmeter.overlay
qt.qpa.fonts: Populating font family aliases took 326 ms. Replace uses of missing font family "Sans Serif" with one that exists to avoid this cost.
overlay.py 미터 자가 검증 통과

$ .venv/bin/python -m tokenmeter.leaderboard
leaderboard.py 자가 검증 통과

$ .venv/bin/python -m tokenmeter.history
history 자가 검증 통과

$ .venv/bin/python -m tokenmeter.endpoints
endpoints.py 자가 검증 통과
```

### Actual CLI smoke harness

This exact command launches the real module CLI for malformed status JSON, one watch JSONL record, adapter init/check, normalized team JSON, and all three receipt formats:

```bash
.venv/bin/python -c '
import json, os, signal, subprocess, sys, tempfile
from pathlib import Path
import yaml
root = Path.cwd()

def strict_json(text):
    def reject(value):
        raise AssertionError(f"non-standard JSON number: {value}")
    return json.loads(text, parse_constant=reject)

def run(args, env, cwd=root):
    result = subprocess.run([sys.executable, "-m", "tokenmeter.cli", *args], cwd=cwd, env=env, capture_output=True, text=True)
    assert result.returncode == 0, (args, result.stdout, result.stderr)
    return result.stdout.strip()

with tempfile.TemporaryDirectory(prefix="tokenmeter-cli-smoke-") as directory:
    base = Path(directory)
    state_dir = base / "state"
    config_dir = base / "config"
    state_dir.mkdir()
    (config_dir / "tokenmeter").mkdir(parents=True)
    (config_dir / "tokenmeter" / "services.yaml").write_text(
        yaml.safe_dump({"settings": {"leaderboard": {"handle": "smoke"}}}), encoding="utf-8")
    env = {**os.environ, "TOKENMETER_HOME": str(state_dir), "XDG_CONFIG_HOME": str(config_dir), "PYTHONPATH": str(root)}
    state_file = state_dir / "state.json"
    state_file.write_text(json.dumps({"sessions": ["broken"], "today": {"totals": {"cost_usd": float("nan")}}, "total": []}), encoding="utf-8")

    status = strict_json(run(["status", "--json"], env))
    assert status["type"] == "snapshot" and status["sessions"] == []
    status_type, status_sessions = status["type"], len(status["sessions"])
    print(f"STATUS JSON: type={status_type} sessions={status_sessions}")

    watch = subprocess.Popen([sys.executable, "-m", "tokenmeter.cli", "watch", "--jsonl"], cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert watch.stdout is not None
    first = watch.stdout.readline()
    watch.send_signal(signal.SIGINT)
    _rest, watch_error = watch.communicate(timeout=5)
    assert watch.returncode == 0 and not watch_error, watch_error
    watch_record = strict_json(first)
    assert watch_record["type"] == "snapshot"
    watch_type, watch_schema = watch_record["type"], watch_record["schema_version"]
    print(f"WATCH JSONL: type={watch_type} schema_version={watch_schema}")

    log = base / "logs" / "private-source.json"
    log.parent.mkdir()
    log.write_text(json.dumps({"usage": {"input_tokens": 1, "output_tokens": 2}}), encoding="utf-8")
    init_output = run(["adapter", "init", "sample", "--log", str(log)], env, base)
    service_file = base / "sample-adapter" / "service.yaml"
    service_file.write_text(service_file.read_text(encoding="utf-8").replace(
        "mode: choose-delta-or-cumulative", "mode: cumulative"), encoding="utf-8")
    check_output = run(["adapter", "check", str(base / "sample-adapter")], env, base)
    assert "input=usage.input_tokens" in check_output and "output=usage.output_tokens" in check_output
    print(f"ADAPTER INIT: {init_output.split(chr(58), 1)[0]}")
    print(f"ADAPTER CHECK: {check_output}")

    team = run(["team", "--json"], env)
    team_obj = strict_json(team)
    assert team_obj["members"] == [{"handle": "smoke", "check": 0, "working": 0, "waiting": 0, "risk": 0, "cost_usd": 0.0, "me": True}]
    print(f"TEAM JSON: {team}")

    state_file.write_text(json.dumps({"sessions": {"codex/s": {"service": "codex", "project": "smoke", "model": "gpt", "plan": "api", "started_at": 10, "last_seen": 70, "ctx": 25, "ctx_win": 100, "totals": {"input_tokens": 1, "cache_read": 2, "cache_write": 3, "output_tokens": 4, "calls": 1, "cost_usd": 0.5}}}}), encoding="utf-8")
    receipt_text = run(["receipt", "--format", "text"], env)
    receipt_markdown = run(["receipt", "--format", "markdown"], env)
    receipt_json = strict_json(run(["receipt", "--format", "json"], env))
    assert "입력 1 · 캐시 읽기 2 · 캐시 쓰기 3 · 출력 4" in receipt_text
    assert "입력 1 · 캐시 읽기 2 · 캐시 쓰기 3 · 출력 4" in receipt_markdown
    assert receipt_json["type"] == "receipt" and receipt_json["totals"]["output_tokens"] == 4
    print("RECEIPT TEXT:")
    print(receipt_text)
    print("RECEIPT MARKDOWN:")
    print(receipt_markdown)
    print("RECEIPT JSON: type=receipt output_tokens=4")

print("CLI smoke checks passed")
'
```

Output:

```text
STATUS JSON: type=snapshot sessions=0
WATCH JSONL: type=snapshot schema_version=1
ADAPTER INIT: ✓ 어댑터 초안을 만들었습니다
ADAPTER CHECK: sample: 구조 검증 통과 (연결된 토큰 필드: input=usage.input_tokens, output=usage.output_tokens)
TEAM JSON: {"schema_version": 1, "type": "team", "timestamp": 1786642149.885243, "members": [{"handle": "smoke", "check": 0, "working": 0, "waiting": 0, "risk": 0, "cost_usd": 0.0, "me": true}]}
RECEIPT TEXT:
TokenMeter 영수증
smoke · codex · gpt
1분 · 입력 1 · 캐시 읽기 2 · 캐시 쓰기 3 · 출력 4 · 1 호출
예상 사용액 $0.50 · 캐시 절감 $0.00
ctx 25% · 서브에이전트 0%
RECEIPT MARKDOWN:
### TokenMeter 영수증
- smoke · codex · gpt
- 1분 · 입력 1 · 캐시 읽기 2 · 캐시 쓰기 3 · 출력 4 · 1 호출
- 예상 사용액 $0.50 · 캐시 절감 $0.00
- ctx 25% · 서브에이전트 0%
RECEIPT JSON: type=receipt output_tokens=4
CLI smoke checks passed
```

### Compilation, diff, and build

Commands and outputs:

```text
$ .venv/bin/python -m py_compile tokenmeter/adapter.py tokenmeter/meter.py tokenmeter/cli.py tokenmeter/leaderboard.py test_tokenmeter.py
(no output; exit 0)

$ git diff --check
(no output; exit 0)

$ uv build
Building source distribution...
Building wheel from source distribution...
Successfully built dist/oct7_tokenmeter-0.1.1.tar.gz
Successfully built dist/oct7_tokenmeter-0.1.1-py3-none-any.whl
```

## Self-review

- **Privacy boundaries:** Public sessions are live-only and exact-key allowlisted. Completed rows, session IDs, paths, routing values, effort, and session totals do not cross the public boundary. Adapter unsafe keys are rejected before `mkdir`, including below redacted secretish parents; the error does not echo content. Generated patterns never store the source filename.
- **Input trust boundaries:** Invalid UTF-8, malformed persisted containers, invalid numeric/context values, NaN/infinity, negative remote counts/costs, and booleans degrade to explicit errors or finite defaults. Programmer errors outside persisted/user input are not broadly swallowed.
- **Public schemas/output compatibility:** `schema_version` and stable attention enums are unchanged. The intentional session allowlist, four-way receipt display, adapter success suffix, and finite team numbers are the only output changes. Receipt text is five lines; Markdown is one heading plus four bullets; JSON remains structured and standard-compliant.
- **CLI/docs:** Status, watch, adapter, team, and receipt actual-module smoke checks pass. English/Korean labels, notification semantics, local/public/team privacy wording, reference sorting/colors, and upload example match the implementation.
- **Test cleanliness:** Assertions use temporary directories and fake runtime/config homes; `HOME` and monkeypatched globals are restored. No real user logs, state, adapter directories, or network endpoint were touched.
- **Final diff:** Minimal shared normalization points were used; no dependency or speculative abstraction was added. No plan, binding spec, or SDD ledger was edited.

## Concerns

- No blocking concerns. The offscreen Qt self-check emitted only the existing missing-font alias performance warning and passed.
