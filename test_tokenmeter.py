#!/usr/bin/env python3
"""TokenMeter 자가 검증 — 프레임워크 없이 assert 만 쓴다.

    python3 test_tokenmeter.py

실제 사용자 설정(~/.claude, ~/.codex, data/state.json)은 **하나도 건드리지 않는다.**
모든 테스트는 임시 디렉토리에서 돌고, 모듈 상수는 끝나면 원복한다.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List

_PROCESS_HOME = tempfile.TemporaryDirectory(prefix="tokenmeter-test-home-")
os.environ.setdefault("TOKENMETER_HOME", str(Path(_PROCESS_HOME.name) / "state"))
os.environ.setdefault("XDG_CONFIG_HOME", str(Path(_PROCESS_HOME.name) / "config"))

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tokenmeter import installer
from tokenmeter.config import Config, InstallSpec, ServiceSpec, dig, load_config
from tokenmeter.leaderboard import Leaderboard, parse_entries, payload
from tokenmeter.meter import DAYS_KEPT, Meter, TokenDelta
from tokenmeter.overlay import visible_pos
from tokenmeter.pricing import cost_usd, normalize_model
from tokenmeter.watcher import ServiceReader

# ── 스펙 §1 의 실측 로그 샘플 ────────────────────────────────────────────────

CLAUDE_RECORD: Dict[str, Any] = {
    "type": "assistant",
    "uuid": "u-1",
    "cwd": "/Users/dev/projects/tokenmeter",
    "sessionId": "sess-claude-1",
    "message": {
        "model": "claude-opus-5",
        "usage": {
            "input_tokens": 2,
            "cache_creation_input_tokens": 2161,
            "cache_read_input_tokens": 60955,
            "output_tokens": 813,
        },
    },
}

OPENCODE_RECORD: Dict[str, Any] = {
    "id": "msg_1",
    "role": "assistant",
    "sessionID": "ses_1",
    "modelID": "nemotron-3-ultra-free",
    "providerID": "opencode",
    "path": {"cwd": "/Users/dev"},
    "cost": 0,
    "tokens": {"total": 0, "input": 0, "output": 0, "reasoning": 0,
               "cache": {"read": 0, "write": 0}},
}


def _codex_record(inp: int, cached: int, cache_write: int, out: int, reasoning: int,
                  last: int = 0, window: int = 0) -> Dict[str, Any]:
    return {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": inp,
                    "cached_input_tokens": cached,
                    "cache_write_input_tokens": cache_write,
                    "output_tokens": out,
                    "reasoning_output_tokens": reasoning,
                },
                "last_token_usage": {"total_tokens": last},
                "model_context_window": window,
            },
        },
    }


# ── 헬퍼 ───────────────────────────────────────────────────────────────────

_CLOCK = [time.time()]


def _bump(path: Path) -> None:
    """mtime 을 확실히 증가시킨다 (같은 순간에 두 번 쓰면 폴링이 변경을 놓친다)."""
    _CLOCK[0] += 1.0
    os.utime(path, (_CLOCK[0], _CLOCK[0]))


def _write(path: Path, text: str, append: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a" if append else "w", encoding="utf-8") as fh:
        fh.write(text)
    _bump(path)


def _write_lines(path: Path, records: List[Any], append: bool = False) -> None:
    _write(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), append)


def _spec(name: str, root: Path) -> ServiceSpec:
    """패키지 services.yaml의 진짜 스펙에 roots만 임시 디렉터리로 갈아끼운다."""
    spec = load_config().get(name)
    assert spec is not None, f"패키지 services.yaml에 {name} 서비스가 없습니다"
    spec.roots = [root]
    return spec


def _vec(delta: TokenDelta) -> tuple:
    return (delta.input_tokens, delta.cache_read, delta.cache_write, delta.output_tokens)


@contextlib.contextmanager
def _state_file(tmp: Path):
    """Meter 가 실제 data/state.json · hours.jsonl 대신 임시 파일을 쓰게 한다."""
    from tokenmeter import meter as meter_mod

    original = (meter_mod.STATE_FILE, meter_mod.HOURS_FILE)
    meter_mod.STATE_FILE = tmp / "state.json"
    meter_mod.HOURS_FILE = tmp / "hours.jsonl"
    try:
        yield
    finally:
        meter_mod.STATE_FILE, meter_mod.HOURS_FILE = original


# ── 테스트 ─────────────────────────────────────────────────────────────────


def test_dig(tmp: Path) -> None:
    obj = {"a": {"b": [{"c": 1}, {"c": 2}]}, "n": None, "zero": 0}
    assert dig(obj, "a.b.1.c") == 2
    assert dig(obj, "a.b.0.c") == 1
    assert dig(obj, "zero") == 0
    assert dig(obj, "a.b.9.c") is None
    assert dig(obj, "a.b.x.c") is None
    assert dig(obj, "a.nope") is None
    assert dig(obj, "n.deeper") is None
    assert dig(obj, "") is None and dig(obj, None) is None
    assert dig(CLAUDE_RECORD, "message.usage.cache_read_input_tokens") == 60955


def test_claude_jsonl(tmp: Path) -> None:
    """Claude Code: delta 모드, uuid 중복 제거, match 필터."""
    root = tmp / "projects"
    path = root / "slug" / "sess-1.jsonl"
    _write_lines(path, [CLAUDE_RECORD])

    got: List[TokenDelta] = []
    reader = ServiceReader(_spec("claude-code", root), got.append)
    assert reader.poll() == 1
    delta = got[0]
    assert _vec(delta) == (2, 60955, 2161, 813), _vec(delta)
    assert delta.model == "claude-opus-5"
    assert delta.project == "tokenmeter"
    assert delta.service == "claude-code"
    assert delta.session == "sess-claude-1", "세션 수 집계가 통째로 빠진다"
    assert delta.vendor == "anthropic"
    assert delta.plan in ("subscription", "api"), delta.plan

    # 같은 uuid 가 다시 나와도 두 번 먹지 않는다
    _write_lines(path, [CLAUDE_RECORD], append=True)
    assert reader.poll() == 0

    # match(type=assistant) 에 안 걸리면 무시
    other = dict(CLAUDE_RECORD, type="user", uuid="u-2")
    _write_lines(path, [other], append=True)
    assert reader.poll() == 0

    # 깨진 줄은 건너뛰고 다음 줄은 정상 처리
    _write(path, '{"type":"assistant","uuid"\n', append=True)
    _write_lines(path, [dict(CLAUDE_RECORD, uuid="u-3")], append=True)
    got.clear()
    assert reader.poll() == 1
    assert _vec(got[0]) == (2, 60955, 2161, 813)


def test_codex_cumulative(tmp: Path) -> None:
    """Codex: 누적치 차분 + 캐시 차감 + reasoning 미가산."""
    root = tmp / "sessions"
    path = root / "2026" / "08" / "10" / "rollout-1.jsonl"
    cwd = "/Users/dev/projects/tokenmeter"
    _write_lines(path, [
        {"type": "session_meta",
         "payload": {"cwd": cwd, "session_id": "sess-codex-1", "model_provider": "openai"}},
        {"type": "turn_context", "payload": {"cwd": cwd, "model": "gpt-5.6-sol"}},
        _codex_record(50327, 34304, 0, 384, 129),
    ])

    got: List[TokenDelta] = []
    reader = ServiceReader(_spec("codex", root), got.append)
    assert reader.poll() == 1
    delta = got[0]
    assert delta.input_tokens == 50327 - 34304, delta.input_tokens  # input 은 캐시를 포함
    assert delta.output_tokens == 384, "reasoning_output_tokens 를 더하면 안 된다"
    assert _vec(delta) == (16023, 34304, 0, 384)
    # cwd/model/session/vendor 는 다른 줄(session_meta / turn_context)에서 학습한다
    assert delta.model == "gpt-5.6-sol" and delta.project == "tokenmeter"
    assert delta.session == "sess-codex-1" and delta.vendor == "openai"

    # 두 번째 누적치 → 증가분만
    got.clear()
    _write_lines(path, [_codex_record(60327, 40304, 100, 584, 200)], append=True)
    assert reader.poll() == 1
    assert _vec(got[0]) == (4000, 6000, 100, 200), _vec(got[0])

    # 같은 누적치를 또 기록해도 증가분 0 → emit 없음
    got.clear()
    _write_lines(path, [_codex_record(60327, 40304, 100, 584, 200)], append=True)
    assert reader.poll() == 0

    # 누적치가 줄면(세션 리셋) baseline 만 갱신하고 먹지 않는다
    _write_lines(path, [_codex_record(10, 0, 0, 5, 0)], append=True)
    assert reader.poll() == 0
    _write_lines(path, [_codex_record(110, 0, 0, 15, 0)], append=True)
    assert reader.poll() == 1
    assert _vec(got[0]) == (100, 0, 0, 10), _vec(got[0])


def test_opencode_json(tmp: Path) -> None:
    """OpenCode: 파일 하나 = 레코드 하나, 같은 id 로 두 번 쓰여도 한 번만."""
    root = tmp / "message"
    path = root / "ses_1" / "msg_1.json"
    _write(path, json.dumps(OPENCODE_RECORD))

    got: List[TokenDelta] = []
    reader = ServiceReader(_spec("opencode", root), got.append)
    assert reader.poll() == 0, "생성 시점(토큰 0)에는 먹지 않는다"

    done = dict(OPENCODE_RECORD)
    done["tokens"] = {"total": 28697, "input": 3265, "output": 88, "reasoning": 68,
                      "cache": {"read": 25344, "write": 0}}
    _write(path, json.dumps(done))
    assert reader.poll() == 1
    delta = got[0]
    assert _vec(delta) == (3265, 25344, 0, 88), _vec(delta)
    assert delta.model == "nemotron-3-ultra-free" and delta.project == "dev"
    # 벤더는 게이트웨이(providerID)를 쓴다 — 모델명(nvidia)이 아니라 실제 결제처다
    assert delta.session == "ses_1" and delta.vendor == "opencode"
    assert delta.plan == "api"

    # 같은 내용이 다시 쓰여도 증가분 0
    _write(path, json.dumps(done))
    assert reader.poll() == 0


def test_prime_skips_history(tmp: Path) -> None:
    """prime() 뒤에는 새로 추가된 줄만 먹는다 (jsonl 오프셋)."""
    root = tmp / "projects"
    path = root / "slug" / "sess-1.jsonl"
    _write_lines(path, [dict(CLAUDE_RECORD, uuid=f"old-{i}") for i in range(5)])

    got: List[TokenDelta] = []
    reader = ServiceReader(_spec("claude-code", root), got.append)
    reader.prime()
    assert reader.poll() == 0 and got == [], "기동 시 과거 로그를 먹으면 안 된다"

    fresh = dict(CLAUDE_RECORD, uuid="new-1")
    fresh["message"] = {"model": "claude-opus-5",
                        "usage": {"input_tokens": 7, "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": 0, "output_tokens": 11}}
    _write_lines(path, [fresh], append=True)
    assert reader.poll() == 1
    assert _vec(got[0]) == (7, 0, 0, 11)


def test_prime_cumulative_baseline(tmp: Path) -> None:
    """cumulative 서비스도 prime() 이 현재 누적치를 baseline 으로 잡는다."""
    root = tmp / "sessions"
    path = root / "2026" / "08" / "10" / "rollout-1.jsonl"
    _write_lines(path, [_codex_record(50000, 0, 0, 1000, 0)])

    got: List[TokenDelta] = []
    reader = ServiceReader(_spec("codex", root), got.append)
    reader.prime()
    assert reader.poll() == 0 and got == []

    _write_lines(path, [_codex_record(50500, 0, 0, 1200, 0)], append=True)
    assert reader.poll() == 1
    assert _vec(got[0]) == (500, 0, 0, 200)


def test_partial_line_not_consumed(tmp: Path) -> None:
    """개행으로 끝나지 않은 마지막 줄은 완성될 때까지 소비하지 않는다."""
    root = tmp / "projects"
    path = root / "slug" / "sess-1.jsonl"
    _write_lines(path, [dict(CLAUDE_RECORD, uuid="u-0")])

    got: List[TokenDelta] = []
    reader = ServiceReader(_spec("claude-code", root), got.append)
    reader.prime()

    line = json.dumps(dict(CLAUDE_RECORD, uuid="u-partial"))
    _write(path, line[:40], append=True)  # 쓰다 만 줄
    assert reader.poll() == 0, "미완결 줄을 먹으면 안 된다"
    _write(path, line[40:] + "\n", append=True)  # 완성
    assert reader.poll() == 1
    assert _vec(got[0]) == (2, 60955, 2161, 813)


def test_meter_buckets(tmp: Path) -> None:
    """델타 하나가 누적/오늘/세션/프로젝트/서비스/모델에 모두 반영된다."""
    with _state_file(tmp):
        config = Config(services={}, settings={})
        meter = Meter(config)
        totals = meter.ingest(
            TokenDelta(1000, 1000, 1000, 1000, "claude-opus-5", "claude-code", "tokenmeter")
        )
        assert totals["input_tokens"] == 1000 and totals["cost_usd"] > 0.0

        meter.ingest(TokenDelta(input_tokens=100_000, model="claude-opus-5"))
        state = meter.status()
        for bucket in ("total", "session", "today"):
            assert state[bucket]["totals"]["input_tokens"] == 101_000, bucket
        assert state["projects"]["tokenmeter"]["totals"]["input_tokens"] == 1000
        assert state["projects"]["(unknown)"]["totals"]["input_tokens"] == 100_000
        assert state["services"]["claude-code"]["totals"]["output_tokens"] == 1000
        assert state["models"]["claude-opus-5"]["totals"]["cache_read"] == 1000
        assert "live_count" in state

        # 저장은 원자적이고, 다시 열면 그대로 읽힌다
        assert Meter(config, read_only=True).state["total"]["totals"]["input_tokens"] == 101_000

        # 초기화하면 모든 버킷이 0 으로 돌아간다
        meter.reset_stats()
        assert meter.state["total"]["totals"]["input_tokens"] == 0
        assert meter.state["today"]["totals"]["cost_usd"] == 0.0
        assert meter.state["projects"] == {} and meter.state["models"] == {}


def test_day_history_rollover(tmp: Path) -> None:
    """날이 바뀌면 today 가 사라지지 않고 days 에 남는다 (오버레이 일별 히스토리의 근거)."""
    with _state_file(tmp):
        meter = Meter(Config(services={}, settings={}))
        meter.ingest(TokenDelta(output_tokens=100, model="claude-opus-5"))
        meter.state["today"]["date"] = "2026-08-10"  # 어제로 되돌려 하루가 넘어가게 한다
        meter.ingest(TokenDelta(output_tokens=7, model="claude-opus-5"))

        assert list(meter.state["days"]) == ["2026-08-10"]
        assert meter.state["days"]["2026-08-10"]["output_tokens"] == 100
        assert meter.state["today"]["totals"]["output_tokens"] == 7  # 새 날은 새 버킷

        # 토큰 없는 날은 남기지 않고, 보관 상한을 넘으면 오래된 날부터 버린다
        meter.state["today"]["date"] = "2026-08-09"
        meter.ingest(TokenDelta(output_tokens=0, input_tokens=1))
        for i in range(1, 71):
            meter.state["days"][f"2026-01-{i:02d}"] = {"output_tokens": 1}
        meter.state["today"]["date"] = "2020-01-01"
        meter.ingest(TokenDelta(output_tokens=1))
        assert len(meter.state["days"]) == DAYS_KEPT
        assert "2026-01-01" not in meter.state["days"]  # 오래된 쪽이 먼저 나간다

        meter.reset_stats()
        assert meter.state["days"] == {}


def test_hour_history_rollover(tmp: Path) -> None:
    """시간이 바뀌면 진행 중 버킷이 hours.jsonl 로 나간다 (시간축 그래프의 근거).

    진행 중인 한 시간은 state.json 안에만 있다 — 델타마다 state 를 통째로 다시
    쓰는 구조라 60일치를 여기 넣으면 쓰기 비용이 그만큼 불어난다.
    """
    from tokenmeter import meter as meter_mod

    with _state_file(tmp):
        meter = Meter(Config(services={}, settings={}))
        meter.ingest(TokenDelta(output_tokens=100, project="a", model="claude-opus-5"))
        assert meter.state["hour"]["p"]["a"][0] == 100, meter.state["hour"]
        assert not meter_mod.HOURS_FILE.exists(), "진행 중인 시간은 아직 파일로 안 나간다"

        # 시간 키를 확실한 과거로 돌려 넘어가게 한다 (실행 시각에 좌우되면 안 된다)
        meter.state["hour"]["h"] = "2020-01-01T00"
        meter.ingest(TokenDelta(output_tokens=7, project="b", model="claude-opus-5"))

        lines = meter_mod.HOURS_FILE.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1, lines
        done = json.loads(lines[0])
        assert done["h"] == "2020-01-01T00" and done["p"]["a"][0] == 100
        assert done["p"]["a"][2] == 1, "호출 수도 같이 나간다"
        assert list(meter.state["hour"]["p"]) == ["b"], "새 시간은 새 버킷"

        # 토큰 없는 시간은 남기지 않는다
        meter.state["hour"] = {"h": "2020-01-01T01", "p": {}}
        meter.ingest(TokenDelta(output_tokens=1, project="c"))
        assert len(meter_mod.HOURS_FILE.read_text(encoding="utf-8").splitlines()) == 1

        # 보관 상한을 넘으면 오래된 줄부터 잘린다
        old = [json.dumps({"h": f"2026-01-01T{i:02d}", "p": {"x": [1, 0.0, 1]}})
               for i in range(24)]
        meter_mod.HOURS_FILE.write_text("\n".join(old) + "\n", encoding="utf-8")
        meter_mod.HOURS_KEPT = 5
        try:
            meter.state["hour"]["h"] = "2020-01-01T02"
            meter.ingest(TokenDelta(output_tokens=1, project="d"))
            kept = meter_mod.HOURS_FILE.read_text(encoding="utf-8").splitlines()
            assert len(kept) == 5, kept
            assert json.loads(kept[-1])["h"] == "2020-01-01T02", "새 줄이 맨 뒤"
            assert json.loads(kept[0])["h"] == "2026-01-01T20", "오래된 쪽이 먼저 나간다"
        finally:
            meter_mod.HOURS_KEPT = 24 * DAYS_KEPT

        meter.reset_stats()
        assert meter.state["hour"]["p"] == {}
        assert not meter_mod.HOURS_FILE.exists(), "리셋은 시간 기록도 지운다"


def test_meter_calls_and_sessions(tmp: Path) -> None:
    """호출 수 · 세션 수 · 벤더/요금제 축 — 서비스 비교 집계의 근거."""
    with _state_file(tmp):
        meter = Meter(Config(services={}, settings={}))

        def feed(model: str, vendor: str, plan: str, session: str) -> None:
            meter.ingest(TokenDelta(
                input_tokens=100, model=model, service="claude-code", project="p",
                session=session, vendor=vendor, plan=plan,
            ))

        feed("claude-opus-5", "anthropic", "subscription", "s1")
        feed("claude-opus-5", "anthropic", "subscription", "s1")  # 같은 세션, 같은 모델
        feed("claude-sonnet-5", "anthropic", "subscription", "s1")  # 같은 세션, 다른 모델
        feed("gpt-5.6-sol", "openai", "api", "s2")  # 다른 세션
        st = meter.state

        # 호출 = 델타 건수
        assert st["total"]["totals"]["calls"] == 4
        assert st["models"]["claude-opus-5"]["totals"]["calls"] == 2
        assert st["vendors"]["anthropic"]["totals"]["calls"] == 3
        assert st["plans"]["api"]["totals"]["calls"] == 1

        # 세션 = 서로 다른 세션 수. 한 세션이 모델을 바꾸면 두 모델 모두 1세션으로 센다
        assert st["total"]["sessions"] == 2
        assert st["models"]["claude-opus-5"]["sessions"] == 1
        assert st["models"]["claude-sonnet-5"]["sessions"] == 1
        assert st["vendors"]["anthropic"]["sessions"] == 1, "같은 세션을 두 번 세면 안 된다"
        assert st["vendors"]["openai"]["sessions"] == 1
        assert st["services"]["claude-code"]["sessions"] == 2

        # 모델 노드는 벤더를 기억한다 (서버가 모델→벤더를 되짚는다)
        assert st["models"]["gpt-5.6-sol"]["vendor"] == "openai"

        # 세션 기록은 상한을 넘으면 오래된 것부터 버린다
        meter.config = Config(services={}, settings={"session_history": 20})
        for i in range(40):
            feed("claude-opus-5", "anthropic", "subscription", f"bulk-{i}")
        assert len(meter.state["sessions"]) == 20, len(meter.state["sessions"])
        assert "claude-code/bulk-39" in meter.state["sessions"]
        assert "claude-code/bulk-0" not in meter.state["sessions"]

        # 세션 id 가 없는 델타는 세션 집계에서만 빠지고 토큰은 그대로 먹는다
        before = meter.state["total"]["sessions"]
        meter.ingest(TokenDelta(input_tokens=50, model="x", service="s"))
        assert meter.state["total"]["sessions"] == before
        assert meter.state["total"]["totals"]["input_tokens"] == 100 * 44 + 50

        from tokenmeter.meter import sessions_today

        assert sessions_today(meter.state) == 20  # 방금 먹었으니 전부 오늘


def test_plan_and_vendor_resolution(tmp: Path) -> None:
    """요금제 판정(환경변수/파일 프로브)과 모델명 → 벤더 추론."""
    from tokenmeter.config import resolve_plan
    from tokenmeter.pricing import vendor_of

    assert vendor_of("claude-opus-5") == "anthropic"
    assert vendor_of("gpt-5.6-sol") == "openai"
    assert vendor_of("nemotron-3-ultra-free") == "nvidia"
    assert vendor_of("무슨-모델") == "unknown" and vendor_of("") == "unknown"

    # 명시값이 항상 이긴다
    assert resolve_plan(ServiceSpec(name="x", label="x", plan="subscription",
                                    plan_probe={"env": ["TP_FAKE_KEY"], "if_set": "api",
                                                "else": "subscription"})) == "subscription"

    env_spec = ServiceSpec(name="x", label="x", plan_probe={
        "env": ["TP_FAKE_KEY"], "if_set": "api", "else": "subscription"})
    os.environ.pop("TP_FAKE_KEY", None)
    assert resolve_plan(env_spec) == "subscription"
    os.environ["TP_FAKE_KEY"] = "sk-1"
    try:
        assert resolve_plan(env_spec) == "api"
    finally:
        os.environ.pop("TP_FAKE_KEY", None)

    auth = tmp / "auth.json"
    auth.write_text(json.dumps({"auth_mode": "chatgpt"}), encoding="utf-8")
    file_spec = ServiceSpec(name="y", label="y", plan_probe={
        "path": str(auth), "key": "auth_mode",
        "map": {"chatgpt": "subscription", "apikey": "api"}, "default": "unknown"})
    assert resolve_plan(file_spec) == "subscription"
    auth.write_text(json.dumps({"auth_mode": "apikey"}), encoding="utf-8")
    assert resolve_plan(file_spec) == "api"
    auth.write_text("깨진 파일", encoding="utf-8")
    assert resolve_plan(file_spec) == "unknown", "프로브가 실패해도 죽으면 안 된다"
    auth.unlink()
    assert resolve_plan(file_spec) == "unknown"
    assert resolve_plan(ServiceSpec(name="z", label="z")) == "unknown"


def test_endpoint_resolution_and_privacy(tmp: Path) -> None:
    """훅이 찍은 라우팅 환경 → 엔드포인트 판별, 그리고 업로드 시 익명화."""
    from tokenmeter import meter as meter_mod
    from tokenmeter.endpoints import SELF_HOSTED, classify, resolve
    from tokenmeter.hook import routing_env
    from tokenmeter.watcher import ServiceReader

    # 훅은 라우팅 환경만 찍고 비밀값은 절대 담지 않는다
    saved = dict(os.environ)
    os.environ.update({
        "ANTHROPIC_BASE_URL": "https://llm.mycorp.com/v1",
        "ANTHROPIC_API_KEY": "present",
        "ANTHROPIC_AUTH_TOKEN": "present",
        "HTTPS_PROXY": "http://proxy.corp:3128",
        "EDITOR": "vim",
    })
    try:
        env = routing_env()
        assert env["ANTHROPIC_BASE_URL"] == "https://llm.mycorp.com/v1"
        assert env["HTTPS_PROXY"] == "http://proxy.corp:3128"
        assert "EDITOR" not in env
        assert not any("secret" in v for v in env.values()), env
        assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env
    finally:
        os.environ.clear()
        os.environ.update(saved)

    spec = _spec("claude-code", tmp)
    assert resolve(spec, {}) == "https://api.anthropic.com"
    assert resolve(spec, env) == "https://llm.mycorp.com/v1"
    assert resolve(spec, {"CLAUDE_CODE_USE_BEDROCK": "1"}) == "bedrock"

    # 워처는 훅이 남긴 라이브 파일에서 세션별 환경을 읽는다
    live = tmp / "live"
    live.mkdir(parents=True, exist_ok=True)
    saved_live = meter_mod.LIVE_DIR
    meter_mod.LIVE_DIR = live
    try:
        (live / "claude-code__s-bedrock.json").write_text(
            json.dumps({"routing_env": {"CLAUDE_CODE_USE_BEDROCK": "1"}}), encoding="utf-8"
        )
        reader = ServiceReader(spec, lambda _d: None)
        assert reader.endpoint_for("s-bedrock", "anthropic") == "bedrock"
        # 라이브 파일이 없으면 서비스 기본값으로 떨어진다 (죽지 않는다)
        assert reader.endpoint_for("s-없음", "anthropic") == "https://api.anthropic.com"
    finally:
        meter_mod.LIVE_DIR = saved_live

    # 업로드는 분류된 라벨만 — 사내 주소는 나가지 않는다
    assert classify("https://llm.mycorp.com/v1") == SELF_HOSTED
    assert classify("https://api.anthropic.com") == "api.anthropic.com"
    assert classify("https://llm.mycorp.com/v1", ["llm.mycorp.com"]) == "llm.mycorp.com"

    with _state_file(tmp):
        m = Meter(Config(services={}, settings={}))
        m.ingest(TokenDelta(input_tokens=10, model="claude-opus-5", service="claude-code",
                            session="s1", vendor="anthropic", plan="api",
                            endpoint="https://llm.mycorp.com/v1"))
        m.ingest(TokenDelta(input_tokens=10, model="claude-opus-5", service="claude-code",
                            session="s2", vendor="anthropic", plan="api",
                            endpoint="https://gw.other-corp.io/v1"))
        assert len(m.state["endpoints"]) == 2, "로컬에는 실제 URL 이 그대로 남는다"
        body = payload(m.state, "alice")
        assert set(body["endpoints"]) == {"self-hosted"}, body["endpoints"]
        assert body["endpoints"]["self-hosted"]["calls"] == 2, "합쳐지면서 숫자는 더해진다"
        assert "mycorp" not in json.dumps(body) and "other-corp" not in json.dumps(body)


def test_state_migration_from_pet(tmp: Path) -> None:
    """v1(펫) 상태 파일을 열면 레벨/경험치는 버리고 누적 토큰만 살린다."""
    with _state_file(tmp):
        from tokenmeter import meter as meter_mod

        meter_mod.STATE_FILE.write_text(
            json.dumps({
                "version": 1,
                "pet": {
                    "level": 12, "exp": 3.5, "name": "토큰햄",
                    "born_at": 111.0, "last_fed": 222.0,
                    "totals": {"input_tokens": 7, "cache_read": 1, "cache_write": 2,
                               "output_tokens": 3, "cost_usd": 0.25},
                },
            }),
            encoding="utf-8",
        )
        state = Meter(Config(services={}, settings={}), read_only=True).state
        assert "pet" not in state, "펫 잔재가 남으면 안 된다"
        assert state["version"] == 2
        assert state["total"]["totals"]["input_tokens"] == 7
        assert state["total"]["totals"]["cost_usd"] == 0.25
        assert state["total"]["started_at"] == 111.0, "누적 시작 시각은 born_at 을 잇는다"


def test_leaderboard_offline_and_ranking(tmp: Path) -> None:
    """랭킹 정렬 · 업로드 범위 · endpoint 없을 때의 로컬 폴백."""
    from tokenmeter import leaderboard as lb_mod

    saved = lb_mod.CACHE_FILE
    lb_mod.CACHE_FILE = tmp / "leaderboard.json"
    try:
        state = {
            "today": {"totals": {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.5}},
            "total": {"totals": {"input_tokens": 100, "output_tokens": 50, "cost_usd": 9.0},
                      "sessions": 4},
            "models": {"claude-opus-5": {"totals": {"input_tokens": 100, "cost_usd": 9.0,
                                                    "calls": 7},
                                         "sessions": 2, "vendor": "anthropic"}},
            "vendors": {"anthropic": {"totals": {"cost_usd": 9.0, "calls": 7}, "sessions": 2}},
            "plans": {"subscription": {"totals": {"cost_usd": 9.0, "calls": 7}, "sessions": 2}},
            "services": {"claude-code": {"totals": {"cost_usd": 9.0, "calls": 7}, "sessions": 2}},
            "projects": {"secret-client": {"totals": {"cost_usd": 1.0}}},
            "sessions": {"claude-code/s1": {"last_seen": time.time()}},
        }
        # 업로드 본문에 프로젝트명은 절대 들어가지 않는다
        body = payload(state, "alice")
        assert set(body) == {"handle", "updated_at", "today", "total", "models",
                             "vendors", "plans", "clients", "endpoints"}, sorted(body)
        assert "secret-client" not in json.dumps(body, ensure_ascii=False)
        assert "projects" not in body
        # 비교 집계에 필요한 축이 전부 실린다
        assert body["models"]["claude-opus-5"]["cost_usd"] == 9.0
        assert body["models"]["claude-opus-5"]["calls"] == 7
        assert body["models"]["claude-opus-5"]["sessions"] == 2
        assert body["models"]["claude-opus-5"]["vendor"] == "anthropic"
        assert body["vendors"]["anthropic"]["calls"] == 7
        assert body["plans"]["subscription"]["sessions"] == 2
        assert body["clients"]["claude-code"]["cost_usd"] == 9.0
        assert body["total"]["sessions"] == 4 and body["today"]["sessions"] == 1

        # 비용 내림차순, 내 줄 표시
        got = parse_entries([{"handle": "friend", "total": {"cost_usd": 99.0}}, body], "total", "alice")
        assert [e.handle for e in got] == ["friend", "alice"]
        assert got[1].me and got[1].tokens == 150

        # endpoint 가 없으면 네트워크를 안 타고 나 혼자 나온다
        board = Leaderboard(Config(services={}, settings={}))
        assert board.online is False
        entries, note = board.board(state, "total")
        assert len(entries) == 1 and entries[0].me and entries[0].cost_usd == 9.0
        assert "endpoint" in note

        # 설정을 읽고, 서버가 죽어 있어도 예외 없이 상태만 남긴다
        online = Leaderboard(Config(services={}, settings={"leaderboard": {
            "handle": "alice", "endpoint": "http://127.0.0.1:1/board", "sync_seconds": 60,
        }}))
        assert online.handle == "alice" and online.online
        online.sync(state, force=True)
        cached = json.loads(lb_mod.CACHE_FILE.read_text(encoding="utf-8"))
        assert cached["status"].startswith("동기화 실패"), cached
    finally:
        lb_mod.CACHE_FILE = saved


def test_cost_usd(tmp: Path) -> None:
    """모델별 단가 + TokenDelta.cost()."""
    assert normalize_model("claude-opus-5") == "claude-opus-5"
    assert normalize_model("") == "default"
    assert normalize_model("모르는-모델-9") == "default"
    assert abs(cost_usd("claude-opus-5", 1_000_000, 0, 0, 0) - 5.00) < 1e-9
    assert abs(cost_usd("claude-opus-5", 0, 1_000_000, 0, 0) - 0.50) < 1e-9
    assert abs(cost_usd("claude-opus-5", 0, 0, 1_000_000, 0) - 6.25) < 1e-9
    assert abs(cost_usd("claude-opus-5", 0, 0, 0, 1_000_000) - 25.00) < 1e-9
    assert abs(cost_usd("모르는-모델-9", 1_000_000, 0, 0, 0) - 3.00) < 1e-9

    delta = TokenDelta(2, 60955, 2161, 813, "claude-opus-5")
    expected = (2 * 5.0 + 60955 * 0.5 + 2161 * 6.25 + 813 * 25.0) / 1_000_000
    assert abs(delta.cost() - expected) < 1e-12, delta.cost()
    assert delta.total == 2 + 60955 + 2161 + 813


def test_visible_pos(tmp: Path) -> None:
    """뽑아버린 외장 모니터 좌표에 창이 갇히지 않는다."""
    laptop = [(0, 0, 1512, 945)]
    two = [(0, 0, 1512, 945), (-5904, 0, 3008, 1692)]
    home = (40, 80)

    assert visible_pos([100, 200], laptop, home) == (100, 200)
    assert visible_pos([-5856, 255], two, home) == (-5856, 255)   # 모니터 붙어 있을 때
    assert visible_pos([-5856, 255], laptop, home) == home        # 뽑은 뒤
    assert visible_pos([1512, 0], laptop, home) == home           # 경계 바로 밖
    assert visible_pos(None, laptop, home) == home
    assert visible_pos(["x", 1], laptop, home) == home


def test_installer_idempotent(tmp: Path) -> None:
    """두 번 설치해도 엔트리 1개, 남의 훅 무손상, uninstall 하면 원본 그대로."""
    path = tmp / "settings.json"
    original: Dict[str, Any] = {
        "model": "opus",
        "hooks": {
            "SessionStart": [
                {"matcher": "", "hooks": [
                    {"type": "command", "command": "/usr/bin/other-hook.sh", "timeout": 3}
                ]}
            ],
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}
            ],
        },
        "permissions": {"allow": ["Bash(ls:*)"]},
    }
    path.write_text(json.dumps(original, indent=2, ensure_ascii=False), encoding="utf-8")
    before = path.read_text(encoding="utf-8")

    spec = ServiceSpec(
        name="claude-code",
        label="Claude Code",
        install=InstallSpec(target="claude_json", path=path, events=["SessionStart", "SessionEnd"]),
    )
    config = Config(services={"claude-code": spec}, settings={})

    # dry-run 은 파일을 건드리지 않는다
    installer.install(config, dry_run=True)
    assert path.read_text(encoding="utf-8") == before
    assert not installer.is_installed(spec)

    installer.install(config)
    installer.install(config)  # 멱등
    data = json.loads(path.read_text(encoding="utf-8"))

    ours = [
        entry
        for groups in data["hooks"].values()
        for group in groups
        for entry in group.get("hooks", [])
        if installer.MARKER in str(entry.get("command", ""))
    ]
    assert len(ours) == 2, f"엔트리가 {len(ours)}개 (SessionStart/SessionEnd 각 1개여야 함)"
    assert all(spec.name in e["command"] for e in ours)
    assert installer.is_installed(spec)

    # 남의 설정은 한 글자도 바뀌지 않는다
    assert data["model"] == original["model"]
    assert data["permissions"] == original["permissions"]
    assert data["hooks"]["PreToolUse"] == original["hooks"]["PreToolUse"]
    assert original["hooks"]["SessionStart"][0] in data["hooks"]["SessionStart"]
    assert (tmp / ("settings.json" + installer.BACKUP_SUFFIX)).exists(), "백업이 없다"

    installer.uninstall(config)
    assert json.loads(path.read_text(encoding="utf-8")) == original, "해제 후 원본과 달라졌다"
    assert not installer.is_installed(spec)


def test_installer_opencode_plugin(tmp: Path) -> None:
    """플러그인 파일 생성/삭제 — 남이 만든 동명 파일은 지우지 않는다."""
    path = tmp / "plugin" / "tokenmeter.js"
    spec = ServiceSpec(
        name="opencode",
        label="OpenCode",
        install=InstallSpec(target="opencode_plugin", path=path, events=["SessionStart"]),
    )
    config = Config(services={"opencode": spec}, settings={})

    installer.install(config)
    assert path.exists() and installer.PLUGIN_MARKER in path.read_text(encoding="utf-8")
    assert installer.is_installed(spec)
    installer.install(config)  # 멱등
    assert installer.is_installed(spec)

    installer.uninstall(config)
    assert not path.exists()

    path.write_text("// 남이 만든 플러그인\n", encoding="utf-8")
    installer.uninstall(config)
    assert path.exists(), "우리 마커가 없는 파일을 지우면 안 된다"


def test_installer_migrates_legacy_opencode_plugin(tmp: Path) -> None:
    """옛 tokenpet.js를 남기면 OpenCode가 두 플러그인을 함께 실행한다."""
    plugin_dir = tmp / "plugin"
    plugin_dir.mkdir()
    legacy = plugin_dir / "tokenpet.js"
    legacy.write_text("// tokenpet:generated\n", encoding="utf-8")
    current = plugin_dir / "tokenmeter.js"
    spec = ServiceSpec(
        name="opencode",
        label="OpenCode",
        install=InstallSpec(target="opencode_plugin", path=current, events=["SessionStart"]),
    )

    installer.install(Config(services={"opencode": spec}, settings={}))

    assert current.exists() and installer.PLUGIN_MARKER in current.read_text(encoding="utf-8")
    assert not legacy.exists(), "옛 생성 플러그인을 남기면 이벤트가 중복된다"


# ── 회귀 (리뷰 지적) ────────────────────────────────────────────────────────


def test_fork_does_not_double_count(tmp: Path) -> None:
    """세션 fork 로 같은 uuid 가 새 파일에 복사돼도 다시 먹지 않는다."""
    root = tmp / "projects"
    old = [dict(CLAUDE_RECORD, uuid=f"u-{i}") for i in range(5)]
    _write_lines(root / "slug" / "a.jsonl", old)

    got: List[TokenDelta] = []
    reader = ServiceReader(_spec("claude-code", root), got.append)
    reader.prime()

    fresh = [dict(CLAUDE_RECORD, uuid="u-new")]
    _write_lines(root / "slug" / "b.jsonl", old + fresh)  # fork = 기존 레코드 복사 + 신규
    assert reader.poll() == 1, "복사된 레코드를 다시 먹으면 토큰이 이중계상된다"
    assert got[0].input_tokens == 2


def test_gc_keeps_baseline_of_live_file(tmp: Path) -> None:
    """유휴 GC 는 살아 있는 파일의 누적 baseline/컨텍스트를 버리면 안 된다."""
    from tokenmeter import watcher as watcher_mod

    root = tmp / "sessions"
    path = root / "rollout-1.jsonl"
    meta = {"payload": {"type": "session_meta", "cwd": "/x/myproj", "model": "gpt-5.6-sol"}}
    _write_lines(path, [meta, _codex_record(1000, 0, 0, 100, 0)])

    got: List[TokenDelta] = []
    reader = ServiceReader(_spec("codex", root), got.append)
    reader.prime()

    reader._touch[str(path)] = time.time() - watcher_mod.GC_IDLE_SEC - 1
    reader._last_gc = 0.0
    reader._gc()

    _write_lines(path, [_codex_record(2000, 0, 0, 200, 0)], append=True)
    assert reader.poll() == 1, "baseline 을 버리면 한 턴이 통째로 유실된다"
    assert _vec(got[0]) == (1000, 0, 0, 100)
    assert got[0].project == "myproj", "컨텍스트를 버리면 귀속이 깨진다"


def test_installer_leaves_foreign_hook(tmp: Path) -> None:
    """경로 조각이 겹치는 남의 훅을 우리 것으로 오인하지 않는다 + 권한 유지."""
    path = tmp / "settings.json"
    foreign = {"type": "command", "command": "python3 /other/tool/tokenmeter/hook.py start", "timeout": 10}
    original = {"hooks": {"SessionStart": [{"matcher": "", "hooks": [foreign]}], "PreToolUse": []}}
    path.write_text(json.dumps(original, indent=2), encoding="utf-8")
    os.chmod(path, 0o600)

    spec = ServiceSpec(
        name="claude-code",
        label="Claude Code",
        install=InstallSpec(target="claude_json", path=path, events=["SessionStart", "SessionEnd"]),
    )
    config = Config(services={"claude-code": spec}, settings={})

    installer.install(config)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert {"matcher": "", "hooks": [foreign]} in data["hooks"]["SessionStart"], "남의 훅을 덮어썼다"
    assert installer.is_installed(spec), "남의 훅을 우리 것으로 오인해 설치를 건너뛰었다"
    assert path.stat().st_mode & 0o777 == 0o600, "원본 권한이 유실됐다"

    installer.uninstall(config)
    assert json.loads(path.read_text(encoding="utf-8")) == original, "해제 후 원본과 달라졌다"


def test_writer_meter_never_reloads(tmp: Path) -> None:
    """writer 는 자기 파일을 다시 읽지 않는다 (진행 중 ingest 의 상태가 날아간다)."""
    with _state_file(tmp):
        from tokenmeter import meter as meter_mod

        meter = Meter(Config(services={}, settings={}))
        meter.ingest(TokenDelta(input_tokens=100))
        meter_mod.STATE_FILE.write_text(
            json.dumps({"total": {"totals": {"input_tokens": 9999}}}), encoding="utf-8"
        )
        _bump(meter_mod.STATE_FILE)
        meter.reload()
        assert meter.state["total"]["totals"]["input_tokens"] == 100

        reader = Meter(Config(services={}, settings={}), read_only=True)
        assert reader.state["total"]["totals"]["input_tokens"] == 9999, "read_only 는 파일을 읽어야 한다"


def test_hook_live_session(tmp: Path) -> None:
    """훅: SessionStart 가 라이브 파일을 만들고 SessionEnd 가 지운다. stdout 은 무조건 비어 있어야 한다."""
    from tokenmeter import hook as hook_mod

    live = tmp / "live"
    saved_dir = hook_mod.LIVE_DIR
    saved_env = dict(os.environ)
    hook_mod.LIVE_DIR = live
    os.environ["TOKENMETER_NO_DAEMON"] = "1"  # 테스트가 데몬을 띄우면 안 된다
    os.environ["TOKENMETER_CWD"] = "/Users/dev/projects/tokenmeter"
    os.environ.pop("TOKENMETER_DISABLE", None)
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert hook_mod.main(["hook.py", "claude-code", "SessionStart"]) == 0
        assert buf.getvalue() == "", "SessionStart 훅의 stdout 은 컨텍스트로 주입된다 — 비어야 한다"

        files = list(live.glob("*.json"))
        assert len(files) == 1, files
        record = json.loads(files[0].read_text(encoding="utf-8"))
        assert record["service"] == "claude-code"
        assert record["project"] == "tokenmeter"
        assert record["event"] == "SessionStart"

        # 중간 이벤트는 파일을 늘리지 않는다 (생존 신호)
        assert hook_mod.main(["hook.py", "claude-code", "Ping"]) == 0
        assert len(list(live.glob("*.json"))) == 1

        assert hook_mod.main(["hook.py", "claude-code", "SessionEnd"]) == 0
        assert list(live.glob("*.json")) == []

        os.environ["TOKENMETER_DISABLE"] = "1"
        assert hook_mod.main(["hook.py", "claude-code", "SessionStart"]) == 0
        assert list(live.glob("*.json")) == [], "TOKENMETER_DISABLE=1 이면 아무것도 하지 않는다"
    finally:
        hook_mod.LIVE_DIR = saved_dir
        os.environ.clear()
        os.environ.update(saved_env)


def test_attention_views(tmp: Path) -> None:
    """라이브 신호와 토큰 시각을 합쳐 세션의 주의 상태를 낸다."""
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


def test_hook_attention_signal_does_not_store_content(tmp: Path) -> None:
    """훅은 정규화된 상태만 저장하고 Stop은 세션을 종료하지 않는다."""
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


def test_live_session_lifecycle(tmp: Path) -> None:
    """Meter 의 라이브 세션 등록/조회/정리."""
    from tokenmeter import meter as meter_mod

    live = tmp / "live"
    live.mkdir(parents=True)
    saved = meter_mod.LIVE_DIR
    meter_mod.LIVE_DIR = live
    try:
        with _state_file(tmp):
            meter = Meter(Config(services={}, settings={}), read_only=True)
            meter.add_live(service="claude-code", session_id="a/b c", cwd="/tmp/proj")
            files = list(live.glob("*.json"))
            assert len(files) == 1
            assert "/" not in files[0].name and " " not in files[0].name  # 파일명 안전화
            sessions = meter.live_sessions()
            assert sessions[0]["project"] == "proj"
            assert meter.status()["live_count"] == 1

            assert meter.prune_live(24.0) == 0
            os.utime(files[0], (time.time() - 7200, time.time() - 7200))
            assert meter.prune_live(1.0) == 1
            assert meter.live_sessions() == []

            meter.add_live(service="codex", session_id="s1")
            assert meter.remove_live("codex", "s1") is True
            assert meter.remove_live("codex", "s1") is False
    finally:
        meter_mod.LIVE_DIR = saved


def test_ingest_keeps_live_file_alive(tmp: Path) -> None:
    """토큰이 들어오는 동안은 라이브 파일이 prune 되면 안 된다.

    훅이 SessionStart/SessionEnd 만 쏘는 서비스(Codex)는 mtime 갱신 기회가 없어,
    live_ttl_hours 를 넘긴 장시간 세션이 죽은 것으로 오인되고 → 라이브 0개 →
    데몬이 세션 도중 자살했다. 토큰 유입 자체가 생존 신호여야 한다.
    """
    from tokenmeter import meter as meter_mod

    live = tmp / "live"
    live.mkdir(parents=True)
    saved = meter_mod.LIVE_DIR
    meter_mod.LIVE_DIR = live
    try:
        with _state_file(tmp):
            meter = Meter(Config(services={}, settings={}))
            meter.add_live(service="codex", session_id="long-run")
            path = next(iter(live.glob("*.json")))

            old = time.time() - 7 * 3600  # ttl(6h) 을 이미 넘긴 상태
            os.utime(path, (old, old))
            assert meter.prune_live(6.0) == 1, "전제 확인: 갱신이 없으면 잘려나간다"

            meter.add_live(service="codex", session_id="long-run")
            os.utime(path, (old, old))
            meter.ingest(TokenDelta(output_tokens=10, service="codex", session="long-run"))
            assert meter.prune_live(6.0) == 0, "토큰이 들어왔는데도 죽은 세션으로 잘렸다"
            assert len(meter.live_sessions()) == 1

            # 라이브 파일이 없는 세션(훅 없이 watch 만)에서도 죽지 않는다
            meter.ingest(TokenDelta(output_tokens=10, service="codex", session="없음"))
    finally:
        meter_mod.LIVE_DIR = saved


def test_toggle_switches(tmp: Path) -> None:
    """on/off 스위치 — 훅과 설정 로딩이 **같은 파일**을 보고 같은 결론을 내야 한다.

    훅은 yaml 을 import 할 수 없어 services.yaml 을 못 본다. 그래서 토글만 JSON 이고,
    둘이 어긋나면 '껐는데 계속 재는' 또는 '켰는데 안 재는' 상태가 된다.
    """
    from tokenmeter import config as config_mod
    from tokenmeter import hook as hook_mod

    saved = (config_mod.TOGGLE_FILE, hook_mod.TOGGLE_FILE)
    path = tmp / "toggle.json"
    config_mod.TOGGLE_FILE = path
    hook_mod.TOGGLE_FILE = path
    try:
        # 파일이 없으면 켜진 것으로 본다 — 측정이 조용히 멈추는 쪽이 나쁘다
        assert not hook_mod.is_off("codex")
        assert load_config().enabled

        config_mod.save_toggle({"enabled": False})
        assert hook_mod.is_off("codex")
        cfg = load_config()
        assert not cfg.enabled
        assert cfg.enabled_services() == [], "전체를 껐는데 워처가 서비스를 잡으면 안 된다"

        config_mod.save_toggle({"services": {"codex": False}})
        assert hook_mod.is_off("codex")
        assert not hook_mod.is_off("claude-code"), "하나만 껐는데 전부 꺼지면 안 된다"
        names = [s.name for s in load_config().enabled_services()]
        assert "codex" not in names and "claude-code" in names

        # 미터 창 토글은 측정과 독립이어야 한다
        config_mod.save_toggle({"overlay": False})
        assert not load_config().overlay_auto
        assert load_config().enabled
        assert not hook_mod.is_off("codex"), "창을 끈 것이 측정을 끄면 안 된다"

        # 깨진 파일도 켜진 것으로 (파일 하나 때문에 측정이 멈추면 안 된다)
        path.write_text("{망가짐", encoding="utf-8")
        assert not hook_mod.is_off("codex")
        assert load_config().enabled
    finally:
        config_mod.TOGGLE_FILE, hook_mod.TOGGLE_FILE = saved


def test_install_state_three_ways(tmp: Path) -> None:
    """'훅 설치' 칸이 거짓말하지 않는지 — 없음 / 반쯤 붙음 / 최신 을 구분한다.

    이벤트가 늘어난 뒤 옛 엔트리만 남아 있으면 '미설치'(엔트리는 있는데)도
    '설치됨'(새 이벤트가 없는데)도 둘 다 틀린 안내가 된다.
    """
    path = tmp / "settings.json"
    events = ["SessionStart", "UserPromptSubmit", "SessionEnd"]
    spec = ServiceSpec(
        name="claude-code",
        label="Claude Code",
        install=InstallSpec(target="claude_json", path=path, events=events),
    )
    config = Config(services={"claude-code": spec}, settings={})

    path.write_text(json.dumps({"hooks": {}}), encoding="utf-8")
    assert installer.install_state(spec) == "missing"

    # 옛 버전이 SessionStart/SessionEnd 만 붙여 둔 상태
    partial = {
        ev: [{"matcher": "", "hooks": [
            {"type": "command", "command": installer.hook_command("claude-code", ev)}
        ]}]
        for ev in ("SessionStart", "SessionEnd")
    }
    path.write_text(json.dumps({"hooks": partial}), encoding="utf-8")
    assert installer.install_state(spec) == "stale", "새 이벤트가 빠졌는데 설치됨으로 보이면 안 된다"
    assert not installer.is_installed(spec)

    installer.install(config)
    assert installer.install_state(spec) == "ok"
    assert sorted(installer.installed_events(spec)) == sorted(events)

    # target: none 은 애초에 설치 대상이 아니다
    bare = ServiceSpec(name="x", label="X", install=InstallSpec(target="none"))
    assert installer.install_state(bare) == "skip"


def test_installer_upgrades_legacy_entry(tmp: Path) -> None:
    """패키지가 src/ 였던 시절의 엔트리는 덧붙이지 말고 제자리에서 교체한다.

    인식하지 못하면 죽은 경로를 가리키는 옛 엔트리가 남아 매 세션 실패한다.
    """
    path = tmp / "settings.json"
    legacy_cmd = f'"/old/py" {installer.LEGACY_MARKERS[0]} claude-code SessionStart'
    path.write_text(
        json.dumps({"hooks": {"SessionStart": [
            {"matcher": "", "hooks": [{"type": "command", "command": legacy_cmd, "timeout": 5}]}
        ]}}),
        encoding="utf-8",
    )
    spec = ServiceSpec(
        name="claude-code",
        label="Claude Code",
        install=InstallSpec(target="claude_json", path=path, events=["SessionStart"]),
    )
    installer.install(Config(services={"claude-code": spec}, settings={}))

    entries = json.loads(path.read_text(encoding="utf-8"))["hooks"]["SessionStart"][0]["hooks"]
    assert len(entries) == 1, f"옛 엔트리를 남기고 덧붙였다: {entries}"
    assert entries[0]["command"] == installer.hook_command("claude-code", "SessionStart")
    assert installer.is_installed(spec)


def test_installer_upgrades_tokenpet_entry_from_moved_checkout(tmp: Path) -> None:
    """다른 위치에서 설치한 TokenPet 훅도 죽은 엔트리를 남기지 않고 교체한다."""
    path = tmp / "settings.json"
    legacy_cmd = (
        '"/old/checkout/.venv/bin/python" '
        '"/old/checkout/tokenpet/hook.py" claude-code UserPromptSubmit'
    )
    path.write_text(
        json.dumps({"hooks": {"UserPromptSubmit": [
            {"matcher": "", "hooks": [{"type": "command", "command": legacy_cmd, "timeout": 5}]}
        ]}}),
        encoding="utf-8",
    )
    spec = ServiceSpec(
        name="claude-code",
        label="Claude Code",
        install=InstallSpec(target="claude_json", path=path, events=["UserPromptSubmit"]),
    )

    installer.install(Config(services={"claude-code": spec}, settings={}))

    entries = json.loads(path.read_text(encoding="utf-8"))["hooks"]["UserPromptSubmit"][0]["hooks"]
    assert len(entries) == 1, f"옮기기 전 죽은 훅이 남았다: {entries}"
    assert entries[0]["command"] == installer.hook_command("claude-code", "UserPromptSubmit")


# ── 컨텍스트 · 캐시 절감 · 단가 오버라이드 · 유휴 알림 ──────────────────────


def test_context_tracking(tmp: Path) -> None:
    """ctx% 의 원료 — '지금 컨텍스트에 얼마나 차 있나' 는 증분이 아니라 현재값이다."""
    root = tmp / "projects"
    got: List[TokenDelta] = []
    reader = ServiceReader(_spec("claude-code", root), got.append)
    _write_lines(root / "slug" / "s.jsonl", [CLAUDE_RECORD])
    assert reader.poll() == 1
    # 그 턴의 input + cache_read + cache_write 가 곧 컨텍스트 점유다 (출력은 뺀다)
    assert got[0].ctx_tokens == 2 + 60955 + 2161, got[0].ctx_tokens
    assert got[0].ctx_window == 200_000, "가격표의 window 가 분모가 된다"

    # 롱컨텍스트 세션: 로그의 model 에는 '[1m]' 이 없다. 200k 창에 400k 가 들어갔다는
    # 관측이 유일한 단서다 — 이걸 안 보면 1M 세션은 계속 100% 로 보인다
    got.clear()
    big = {**CLAUDE_RECORD, "uuid": "u-big",
           "message": {"model": "claude-opus-5",
                       "usage": {"input_tokens": 2, "cache_read_input_tokens": 430_000,
                                 "cache_creation_input_tokens": 5_000, "output_tokens": 100}}}
    _write_lines(root / "slug" / "big.jsonl", [big])
    assert reader.poll() == 1
    assert got[0].ctx_tokens > 200_000 and got[0].ctx_window == 1_000_000, got[0].ctx_window

    # 서브에이전트는 부모와 같은 sessionId 로 찍힌다 — 토큰은 내 것이지만 컨텍스트는 아니다
    got.clear()
    _write_lines(root / "slug" / "sub.jsonl", [{**big, "uuid": "u-sub", "isSidechain": True}])
    assert reader.poll() == 1
    assert got[0].total > 0 and got[0].subagent, "서브에이전트 토큰은 그대로 합산된다"
    assert got[0].ctx_tokens == 0, "남의 컨텍스트가 내 세션 ctx% 를 덮어쓰면 안 된다"

    # 비용은 세션에 합산되고, 그중 하위 에이전트 몫이 얼마인지 따로 남는다
    with _state_file(tmp / "sub"):
        meter = Meter(Config(services={}, settings={}))
        meter.ingest(TokenDelta(output_tokens=100, model="claude-opus-5", service="claude-code",
                                session="s", ctx_tokens=1_000, ctx_window=200_000))
        meter.ingest(TokenDelta(output_tokens=300, model="claude-opus-5", service="claude-code",
                                session="s", subagent=True))
        rec = meter.state["sessions"]["claude-code/s"]
    assert rec["totals"]["output_tokens"] == 400, rec["totals"]
    assert abs(rec["sub_cost"] / rec["totals"]["cost_usd"] - 0.75) < 1e-9, rec
    assert rec["ctx"] == 1_000, "서브에이전트가 세션 컨텍스트를 건드리면 안 된다"

    # 누적 서비스(codex)는 fields 합계가 세션 총량이라 last_token_usage 를 봐야 한다
    croot = tmp / "sessions"
    cgot: List[TokenDelta] = []
    creader = ServiceReader(_spec("codex", croot), cgot.append)
    _write_lines(croot / "r.jsonl", [
        {"type": "turn_context", "payload": {"cwd": "/a/tokenmeter", "model": "gpt-5.6-sol"}},
        _codex_record(4_657_501, 4_343_296, 0, 20_577, 9_593, last=123_165, window=353_400),
    ])
    assert creader.poll() == 1
    assert cgot[0].ctx_tokens == 123_165, "세션 누적(4.6M)이 컨텍스트로 잡히면 늘 100% 다"
    assert cgot[0].ctx_window == 353_400, "로그가 알려주는 창이 가격표보다 우선한다"

    # 압축되면 다음 레코드에서 그대로 내려간다 (누적이 아니므로)
    cgot.clear()
    _write_lines(croot / "r.jsonl",
                 [_codex_record(4_700_000, 4_343_296, 0, 20_800, 9_593,
                                last=12_000, window=353_400)], append=True)
    assert creader.poll() == 1 and cgot[0].ctx_tokens == 12_000


def test_cache_savings(tmp: Path) -> None:
    """캐시 읽기로 아낀 금액이 비용과 같은 축에 쌓인다."""
    from tokenmeter.pricing import cache_savings

    # opus-5: 입력 $5 / 캐시읽기 $0.5 → 1M 토큰을 캐시로 읽으면 $4.5 를 안 낸다
    assert abs(cache_savings("claude-opus-5", 1_000_000) - 4.5) < 1e-9
    assert cache_savings("claude-opus-5", 0) == 0.0

    with _state_file(tmp):
        meter = Meter()
        meter.ingest(TokenDelta(0, 1_000_000, 0, 0, "claude-opus-5", session="s1"))
        for node in (meter.state["total"], meter.state["today"], meter.state["session"]):
            assert abs(node["totals"]["cache_saved_usd"] - 4.5) < 1e-9, node
        # 축별 집계와 세션 기록에도 같이 쌓인다
        assert abs(meter.state["models"]["claude-opus-5"]["totals"]["cache_saved_usd"] - 4.5) < 1e-9
        assert abs(meter.state["sessions"]["?/s1"]["totals"]["cache_saved_usd"] - 4.5) < 1e-9
        # 절감액은 비용이 아니다 — 실제로 낸 돈은 캐시 단가뿐이다
        assert abs(meter.state["total"]["totals"]["cost_usd"] - 0.5) < 1e-9


def test_price_override(tmp: Path) -> None:
    """가격표에 없는 모델의 단가를 사용자가 직접 못 박는다 (~/.config/tokenmeter/prices.json)."""
    from tokenmeter import pricing

    original, original_mtime = pricing.USER_PRICES, pricing._OVER_MTIME
    pricing.USER_PRICES = tmp / "prices.json"  # 사용자 실제 파일은 건드리지 않는다
    pricing._OVER_MTIME = -1.0
    try:
        assert not pricing.known("nemotron-3-ultra"), "가격표에 없어야 하는 전제가 깨졌다"
        assert abs(cost_usd("nemotron-3-ultra", 1_000_000) - 3.0) < 1e-9  # default 추정
        assert pricing.context_window("nemotron-3-ultra") == 0, "모르면 0 — ctx% 를 지어내지 않는다"

        pricing.set_price("nemotron-3-ultra", {"input": 1.0, "output": 2.0, "window": 128_000})
        assert pricing.known("nemotron-3-ultra"), "지정했으면 더 이상 '모르는 모델' 이 아니다"
        assert abs(cost_usd("nemotron-3-ultra", 1_000_000) - 1.0) < 1e-9
        assert abs(cost_usd("nemotron-3-ultra", 0, 0, 0, 1_000_000) - 2.0) < 1e-9
        assert pricing.context_window("nemotron-3-ultra") == 128_000
        # 안 적은 항목은 기본 표에서 채운다
        assert pricing.prices_for("nemotron-3-ultra")["cache_read"] == 0.3

        # 대소문자/구분자가 달라도 같은 모델로 본다
        assert pricing.known("Nemotron_3 Ultra".replace(" ", "-"))

        # 기본 표의 모델도 덮어쓸 수 있다 (롱컨텍스트 프리미엄 같은 것)
        pricing.set_price("claude-opus-5[1m]", {"input": 10.0})
        assert abs(cost_usd("claude-opus-5[1m]", 1_000_000) - 10.0) < 1e-9
        assert pricing.context_window("claude-opus-5[1m]") == 1_000_000, "[1m] 은 창도 1M"
        assert abs(cost_usd("claude-opus-5", 1_000_000) - 5.0) < 1e-9, "본체 단가는 그대로"

        # 파일이 깨져 있어도 계산은 계속된다 (기본 표로)
        (tmp / "prices.json").write_text("{ 깨진", encoding="utf-8")
        assert abs(cost_usd("claude-opus-5", 1_000_000) - 5.0) < 1e-9

        pricing.set_price("nemotron-3-ultra", {"input": 1.0})
        assert pricing.unset_price("nemotron-3-ultra")
        assert not pricing.unset_price("nemotron-3-ultra"), "없는 걸 지우면 False"
        assert abs(cost_usd("nemotron-3-ultra", 1_000_000) - 3.0) < 1e-9
    finally:
        pricing.USER_PRICES, pricing._OVER_MTIME = original, original_mtime
        pricing._OVER = {}


def test_idle_notice_due(tmp: Path) -> None:
    """유휴 알림은 조용한 구간마다 한 번만 울린다."""
    from tokenmeter.cli import idle_notice_due

    last, after = 1000.0, 90.0
    assert not idle_notice_due(last, 1050.0, after, True, 0.0), "아직 90초가 안 됐다"
    assert idle_notice_due(last, 1100.0, after, True, 0.0)
    assert not idle_notice_due(last, 1100.0, after, True, last), "같은 구간에 두 번 울리면 스팸"
    assert not idle_notice_due(last, 1100.0, after, False, 0.0), "세션이 없으면 기다리는 사람도 없다"
    assert not idle_notice_due(last, 1100.0, 0.0, True, 0.0), "0 이면 꺼진 것"
    assert not idle_notice_due(0.0, 1100.0, after, True, 0.0), "한 번도 안 먹었으면 알릴 게 없다"
    # 토큰이 다시 들어오면(last_seen 갱신) 다음 조용한 구간에 다시 울린다
    assert idle_notice_due(2000.0, 2100.0, after, True, last)


def test_runtime_paths_and_legacy_copy(tmp: Path) -> None:
    """설치 패키지 밖에 상태를 쓰고, 옛 데이터는 원본을 남긴 채 한 번만 복사한다."""
    old_env = dict(os.environ)
    try:
        os.environ["TOKENMETER_HOME"] = str(tmp / "state")
        os.environ["XDG_CONFIG_HOME"] = str(tmp / "config")

        from tokenmeter.paths import config_dir, data_dir, migrate_legacy

        legacy_root = tmp / "checkout"
        legacy_data = legacy_root / "data"
        legacy_data.mkdir(parents=True)
        (legacy_data / "state.json").write_text('{"version": 2}', encoding="utf-8")
        legacy_config = tmp / "legacy-config"
        legacy_config.mkdir()
        (legacy_config / "services.yaml").write_text("services: {}\n", encoding="utf-8")

        migrate_legacy(legacy_root, legacy_config)

        assert data_dir() == tmp / "state"
        assert config_dir() == tmp / "config" / "tokenmeter"
        assert (data_dir() / "state.json").read_text(encoding="utf-8") == '{"version": 2}'
        assert (config_dir() / "services.yaml").read_text(encoding="utf-8") == "services: {}\n"
        assert (legacy_data / "state.json").exists(), "이전 뒤에도 원본 데이터는 남겨야 한다"
        assert (legacy_config / "services.yaml").exists(), "이전 뒤에도 원본 설정은 남겨야 한다"

        (data_dir() / "state.json").write_text('{"version": 3}', encoding="utf-8")
        migrate_legacy(legacy_root, legacy_config)
        assert (data_dir() / "state.json").read_text(encoding="utf-8") == '{"version": 3}', \
            "이미 시작한 새 상태를 옛 데이터로 덮어쓰면 안 된다"
    finally:
        os.environ.clear()
        os.environ.update(old_env)


def test_config_and_hook_share_runtime_home(tmp: Path) -> None:
    """CLI와 stdlib-only 훅이 서로 다른 상태 디렉터리를 보면 측정이 끊긴다."""
    root = Path(__file__).resolve().parent
    state = tmp / "state"
    config_home = tmp / "config"
    env = {
        **os.environ,
        "TOKENMETER_HOME": str(state),
        "XDG_CONFIG_HOME": str(config_home),
        "PYTHONPATH": str(root),
    }
    code = (
        "from tokenmeter import config, hook; "
        "print(config.DATA_DIR); print(hook.DATA_DIR); print(config.USER_CONFIG)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=root, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        str(state), str(state), str(config_home / "tokenmeter" / "services.yaml")
    ]


def test_tokenmeter_public_import_and_module_cli(tmp: Path) -> None:
    """공개 패키지명으로 import와 모듈 CLI가 실제 실행돼야 한다."""
    root = Path(__file__).resolve().parent
    env = {**os.environ, "PYTHONPATH": str(root), "TOKENMETER_HOME": str(tmp / "state")}
    imported = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from tokenmeter.config import DEFAULT_CONFIG, load_config; "
                "assert DEFAULT_CONFIG.exists(); "
                "assert {'claude-code', 'codex', 'opencode'} <= set(load_config().services)"
            ),
        ],
        cwd=tmp,
        env=env,
        capture_output=True,
        text=True,
    )
    assert imported.returncode == 0, imported.stderr

    cli = subprocess.run(
        [sys.executable, "-m", "tokenmeter.cli", "--help"],
        cwd=tmp,
        env=env,
        capture_output=True,
        text=True,
    )
    assert cli.returncode == 0, cli.stderr
    assert "TokenMeter" in cli.stdout
    assert "usage: tokenmeter" in cli.stdout


def test_install_guides_first_measurement(tmp: Path) -> None:
    """빈 홈에서 설치한 사용자가 출력만 보고 첫 측정까지 갈 수 있어야 한다."""
    root = Path(__file__).resolve().parent
    home = tmp / "home"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "PYTHONPATH": str(root),
        "TOKENMETER_HOME": str(tmp / "state"),
        "XDG_CONFIG_HOME": str(tmp / "config"),
    }
    installed = subprocess.run(
        [sys.executable, "-m", "tokenmeter.cli", "install"],
        cwd=tmp,
        env=env,
        capture_output=True,
        text=True,
    )
    assert installed.returncode == 0, installed.stderr
    assert "1." in installed.stdout and "완전히 다시" in installed.stdout
    assert "2." in installed.stdout and "프롬프트" in installed.stdout
    assert "3." in installed.stdout and "tokenmeter status" in installed.stdout
    assert "4." in installed.stdout and "tokenmeter doctor" in installed.stdout

    status = subprocess.run(
        [sys.executable, "-m", "tokenmeter.cli", "status"],
        cwd=tmp,
        env=env,
        capture_output=True,
        text=True,
    )
    assert status.returncode == 0, status.stderr
    assert "첫 세션 대기 중" in status.stdout
    assert "tokenmeter doctor" in status.stdout


def test_skill_wrapper_invokes_installed_cli(tmp: Path) -> None:
    """스킬은 소스 체크아웃을 찾지 않고 설치된 tokenmeter 명령을 그대로 호출한다."""
    root = Path(__file__).resolve().parent
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "tokenmeter"
    fake.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*"\n', encoding="utf-8")
    fake.chmod(0o755)
    result = subprocess.run(
        [str(root / "skills" / "tokenmeter" / "tm"), "status", "--sync"],
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "status --sync"


# ── 러너 ───────────────────────────────────────────────────────────────────


def main() -> int:
    tests = [(k, v) for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed: List[str] = []
    print(f"TokenMeter 자가 검증 — {len(tests)}개\n")
    for name, func in tests:
        with tempfile.TemporaryDirectory(prefix="tokenmeter-test-") as tmp:
            try:
                func(Path(tmp))
            except Exception:
                failed.append(name)
                print(f"  ✗ {name}")
                traceback.print_exc()
                continue
        print(f"  ✓ {name}")
    print()
    if failed:
        print(f"실패 {len(failed)}개: {', '.join(failed)}")
        return 1
    print(f"전부 통과 ({len(tests)}개)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
