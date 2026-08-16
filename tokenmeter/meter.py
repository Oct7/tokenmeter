"""토큰 계량 (Application 레이어).

`data/state.json` 의 단일 writer 는 데몬(워처)이다. 오버레이/CLI 는 `read_only=True`
로 열어서 읽기만 한다. 누적(total)은 글로벌·영구 — 세션이 끝나도 남는다.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import (
    HISTORY_DIR,
    HOURS_FILE,
    LIVE_DIR,
    RATES_FILE,
    STATE_FILE,
    Config,
    deep_merge,
    ensure_dirs,
    load_config,
)
from .pricing import cache_savings, cost_usd
from .rates import RATES_KEPT, active_seconds, add_rate, rate_key, rate_slot
from .views import project_key

STATE_VERSION = 2
DAYS_KEPT = 60  # 일별 히스토리 보관 일수 (state.json 이 무한정 커지지 않게)
HOURS_KEPT = 24 * DAYS_KEPT  # 시간별 버킷 보관 줄 수 (hours.jsonl)
ATTENTION_ACTIVE_SECONDS = 30.0
ATTENTION_LABELS = {"check": "확인", "working": "작업", "waiting": "대기", "done": "종료"}
ATTENTION_ORDER = {name: i for i, name in enumerate(("check", "working", "waiting", "done"))}


def _int(v: Any) -> int:
    """state.json 은 사람이 고칠 수 있는 파일이다 — 숫자 자리에 뭐가 있어도 죽지 않는다."""
    return int(_float(v))


def _float(v: Any) -> float:
    if isinstance(v, bool):
        return 0.0
    try:
        value = float(v)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def session_views(state: Dict[str, Any], now: Optional[float] = None) -> List[Dict[str, Any]]:
    """저장된 토큰 기록과 라이브 신호를 합쳐 세션별 주의 상태를 만든다."""
    now = time.time() if now is None else now
    raw_stored = state.get("sessions") if isinstance(state.get("sessions"), dict) else {}
    stored = {str(key): value for key, value in raw_stored.items()}
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
        started_at = _float(rec.get("started_at") or (active or {}).get("started_at"))
        activity_at = max(
            token_at,
            signal_at,
            _float((active or {}).get("updated_at")),
            _float((active or {}).get("event_at")),
            started_at,
        )
        if active is None:
            attention = "done"
        elif signal == "check" and signal_at > 0 and signal_at >= token_at:
            attention = "check"
        elif now - max(token_at, signal_at if signal == "working" else 0.0) <= ATTENTION_ACTIVE_SECONDS:
            attention = "working"
        else:
            attention = "waiting"
        service, separator, session_id = key.partition("/")
        if not separator:
            service, session_id = "?", key
        rec_service = rec.get("service") or (active or {}).get("service") or service
        project = rec.get("project") or (active or {}).get("project") or "(unknown)"
        model = rec.get("model") or (active or {}).get("model") or ""
        totals = dict(rec.get("totals")) if isinstance(rec.get("totals"), dict) else {}
        sub_output = min(_int(totals.get("output_tokens")), _int(rec.get("sub_output_tokens")))
        event = (active or {}).get("event") or rec.get("event") or ""
        cwd = (active or {}).get("cwd") or rec.get("cwd") or ""
        rows.append({
            "key": key,
            "service": rec_service if isinstance(rec_service, str) else service,
            "session_id": (active or {}).get("session_id") or session_id,
            "project": project if isinstance(project, str) else "(unknown)",
            "model": model if isinstance(model, str) else "",
            "effort": rec.get("effort") or "", "vendor": rec.get("vendor") or "",
            "plan": rec.get("plan") or "unknown", "started_at": started_at,
            "last_seen": token_at, "activity_at": activity_at,
            "totals": totals,
            "cost_usd": _float(totals.get("cost_usd")),
            "main_output_tokens": max(0, _int(totals.get("output_tokens")) - sub_output),
            "sub_output_tokens": sub_output,
            "ctx": _int(rec.get("ctx")),
            "ctx_win": _int(rec.get("ctx_win")), "sub_cost": _float(rec.get("sub_cost")),
            "attention": attention, "attention_at": signal_at, "live": active is not None,
            "event": event if isinstance(event, str) else "",
            "cwd": cwd if isinstance(cwd, str) else "",
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


@dataclass
class TokenDelta:
    """한 번의 토큰 증가분. 워처가 만들고 Meter 가 먹는다."""

    input_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output_tokens: int = 0
    model: str = "default"
    service: str = ""
    project: str = ""
    session: str = ""  # 서비스 안에서 고유한 세션 id (없으면 세션 집계에서 빠진다)
    vendor: str = ""  # anthropic | openai | ... (watcher 가 판정)
    plan: str = ""  # subscription | api | unknown
    endpoint: str = ""  # 통신 대상 URL 또는 라벨 (bedrock/vertex)
    effort: str = ""  # 추론 강도 (low | medium | high | xhigh …). 없는 서비스도 있다
    ctx_tokens: int = 0  # 이 턴이 끝난 시점의 컨텍스트 점유 (누적이 아니라 '지금')
    ctx_window: int = 0  # 그 모델의 컨텍스트 창. 0 이면 모름
    subagent: bool = False  # 하위 에이전트가 태운 몫 (같은 세션에 합산되지만 따로 센다)

    @property
    def total(self) -> int:
        return self.input_tokens + self.cache_read + self.cache_write + self.output_tokens

    def cost(self) -> float:
        return cost_usd(
            self.model,
            self.input_tokens,
            self.cache_read,
            self.cache_write,
            self.output_tokens,
        )

    def saved(self) -> float:
        """캐시 읽기로 아낀 금액. 비용과 같은 축에 쌓아둬야 '얼마나 이득인지' 가 보인다."""
        return cache_savings(self.model, self.cache_read)


def _totals() -> Dict[str, Any]:
    return {
        "input_tokens": 0,
        "cache_read": 0,
        "cache_write": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "cache_saved_usd": 0.0,  # 캐시 읽기로 아낀 금액 (안 냈으므로 cost 와 별도)
        "calls": 0,  # 델타 1건 = 모델 호출 1회
    }


# 델타 하나가 동시에 들어가는 축들. 여기 추가하면 CLI·업로드가 함께 따라간다.
GROUPS = ("projects", "services", "models", "vendors", "plans", "endpoints")


def _today_str() -> str:
    return date.today().isoformat()


def _hour_str(ts: Optional[float] = None) -> str:
    """'2026-08-11T15' — 시간 버킷의 키."""
    return time.strftime("%Y-%m-%dT%H", time.localtime(ts))


def _default_state() -> Dict[str, Any]:
    now = time.time()
    state = {
        "version": STATE_VERSION,
        "total": {"started_at": now, "last_seen": 0.0, "sessions": 0, "totals": _totals()},
        "session": {"started_at": now, "totals": _totals()},
        "today": {"date": _today_str(), "totals": _totals()},
        "days": {},  # 날짜 → 그날의 합계 (DAYS_KEPT 일까지). 하루가 넘어갈 때 today 가 여기로 간다
        # 진행 중인 한 시간만 여기 둔다. 끝나면 hours.jsonl 로 나간다 — 델타마다 이
        # 파일을 통째로 다시 쓰므로 60일치를 담으면 쓰기 비용이 그만큼 불어난다.
        "hour": {"h": "", "p": {}},  # {"h": 시간키, "p": {프로젝트: [토큰, 비용, 호출]}}
        "rate": {"h": "", "m": {}},  # {"h": 15분키, "m": {벤더/모델: [출력토큰, 활성초, 횟수]}}
        "sessions": {},  # 최근 세션 기록 (session_history 개까지)
        "updated_at": now,
    }
    for group in GROUPS:
        state[group] = {}
    return state


def _accumulate(totals: Dict[str, Any], delta: TokenDelta, cost: float, saved: float = 0.0) -> None:
    totals["input_tokens"] = int(totals.get("input_tokens", 0)) + delta.input_tokens
    totals["cache_read"] = int(totals.get("cache_read", 0)) + delta.cache_read
    totals["cache_write"] = int(totals.get("cache_write", 0)) + delta.cache_write
    totals["output_tokens"] = int(totals.get("output_tokens", 0)) + delta.output_tokens
    totals["cost_usd"] = round(float(totals.get("cost_usd", 0.0)) + cost, 10)
    totals["cache_saved_usd"] = round(_float(totals.get("cache_saved_usd")) + saved, 10)
    totals["calls"] = int(totals.get("calls", 0)) + 1


def tokens_of(totals: Any) -> int:
    """합계 dict → 토큰 4종의 합."""
    if not isinstance(totals, dict):
        return 0
    return sum(
        _int(totals.get(k))
        for k in ("input_tokens", "cache_read", "cache_write", "output_tokens")
    )


def sessions_today(state: Dict[str, Any]) -> int:
    """오늘 활동한 세션 수. 보관 중인 최근 세션 기록에서 센다."""
    today = _today_str()
    sessions = state.get("sessions") if isinstance(state.get("sessions"), dict) else {}
    return sum(
        1
        for rec in sessions.values()
        if isinstance(rec, dict) and _day(rec.get("last_seen")) == today
    )


def _day(ts: Any) -> str:
    try:
        return date.fromtimestamp(float(ts)).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _safe_name(value: str) -> str:
    """파일명에 쓸 수 있게 세션 id 를 안전화한다."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(value or "unknown"))[:120]


def live_path(service: str, session_id: str) -> Path:
    """훅이 쓰는 라이브 세션 파일 경로 (훅·워처·미터가 같은 규칙을 써야 한다)."""
    return LIVE_DIR / f"{_safe_name(service)}__{_safe_name(session_id)}.json"


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _migrate(state: Dict[str, Any]) -> Dict[str, Any]:
    """v1(펫) → v2(미터). 레벨/경험치는 버리고 누적 토큰만 살린다."""
    legacy = state.pop("pet", None)
    if isinstance(legacy, dict):
        total = state.setdefault("total", {})
        if not any(_int(v) for v in (total.get("totals") or {}).values()):
            total["totals"] = deep_merge(_totals(), legacy.get("totals") or {})
        total["started_at"] = legacy.get("born_at") or total.get("started_at") or time.time()
        total["last_seen"] = legacy.get("last_fed") or total.get("last_seen") or 0.0
    state["version"] = STATE_VERSION
    return state


class Meter:
    """state.json 을 읽고 쓰는 계량기.

    쓰기 권한(read_only=False)으로 열면 '세션' 버킷을 새로 시작한다.
    데몬 1회 실행 = 세션 1개라는 뜻이다. 펫/오늘/누적은 그대로 이어진다.
    """

    def __init__(self, config: Optional[Config] = None, read_only: bool = False) -> None:
        ensure_dirs()
        self.config = config or load_config()
        self.read_only = read_only
        self._save_lock = threading.Lock()  # 워처 스레드 vs GUI 스레드가 tmp 를 겹쳐 쓰는 것 방지
        self._mtime = 0.0
        self._project_cache: Dict[str, str] = {}  # 세션 키 → 훅이 알려준 프로젝트
        self.state: Dict[str, Any] = _default_state()
        self._load()
        if not read_only:
            self.state["session"] = {"started_at": time.time(), "totals": _totals()}

    # ── 상태 파일 ────────────────────────────────────────────────────────
    def _load(self) -> None:
        raw: Any = None
        try:
            if STATE_FILE.exists():
                raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                self._mtime = STATE_FILE.stat().st_mtime
        except Exception:
            raw = None  # 깨진 상태 파일은 조용히 버리고 기본값으로
        self.state = _migrate(deep_merge(_default_state(), raw if isinstance(raw, dict) else {}))

    def _save(self) -> None:
        if self.read_only:
            return
        with self._save_lock:  # 같은 프로세스의 두 스레드가 tmp 를 반쪽씩 쓰면 안 된다
            self.state["updated_at"] = time.time()
            tmp = STATE_FILE.with_name(f"{STATE_FILE.name}.{os.getpid()}.tmp")
            try:
                tmp.write_text(
                    json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                os.replace(tmp, STATE_FILE)  # 원자적 교체 — 오버레이가 반쪽 파일을 보지 않게
                self._mtime = STATE_FILE.stat().st_mtime
            except Exception:
                try:
                    tmp.unlink()
                except Exception:
                    pass

    def reload(self) -> None:
        """파일이 바뀌었으면 다시 읽는다 (오버레이/CLI 용)."""
        if not self.read_only:
            # writer 가 자기 파일을 다시 읽으면 진행 중인 ingest 의 self.state 가
            # 통째로 교체되어 그 델타가 조용히 사라진다. writer 의 메모리가 곧 최신이다.
            return
        try:
            mtime = STATE_FILE.stat().st_mtime if STATE_FILE.exists() else 0.0
        except Exception:
            return
        if mtime != self._mtime:
            self._load()

    # ── 계량 ────────────────────────────────────────────────────────────
    def _roll_today(self) -> None:
        """날이 바뀌면 today 를 굴린다. 끝난 날은 버리지 않고 days 에 남긴다."""
        today = _today_str()
        node = self.state["today"]
        if node.get("date") == today:
            return
        done, totals = str(node.get("date") or ""), node.get("totals") or {}
        if done and tokens_of(totals):
            days = self.state.setdefault("days", {})
            days[done] = dict(totals)
            for old in sorted(days)[: max(0, len(days) - DAYS_KEPT)]:
                del days[old]
        self.state["today"] = {"date": today, "totals": _totals()}

    def _roll_hour(self) -> None:
        """시간이 바뀌면 진행 중 버킷을 hours.jsonl 로 내보낸다.

        append 는 시간당 한 번뿐이다. 보관 상한을 넘길 때만 파일을 다시 쓴다.
        """
        now_h = _hour_str()
        node = self.state.setdefault("hour", {"h": now_h, "p": {}})
        if node.get("h") == now_h:
            return
        if node.get("h") and node.get("p") and not self.read_only:
            self._append_hour(node)
        self.state["hour"] = {"h": now_h, "p": {}}

    def _append_hour(self, node: Dict[str, Any]) -> None:
        try:
            line = json.dumps(node, ensure_ascii=False, separators=(",", ":"))
            with HOURS_FILE.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            lines = HOURS_FILE.read_text(encoding="utf-8").splitlines()
            if len(lines) > HOURS_KEPT:
                HOURS_FILE.write_text("\n".join(lines[-HOURS_KEPT:]) + "\n", encoding="utf-8")
        except OSError:
            pass  # 시간 기록은 있으면 좋은 것이다 — 못 써도 계량은 계속된다

    def _bucket_hour(self, delta: TokenDelta, cost: float) -> None:
        """지금 시간 버킷에 프로젝트별로 쌓는다. [토큰, 비용, 호출] 세 칸뿐이다."""
        book = self.state["hour"].setdefault("p", {})
        cell = book.get(delta.project or "(unknown)")
        if not isinstance(cell, list) or len(cell) != 3:
            cell = [0, 0.0, 0]
            book[delta.project or "(unknown)"] = cell
        cell[0] = _int(cell[0]) + delta.total
        cell[1] = round(_float(cell[1]) + cost, 10)
        cell[2] = _int(cell[2]) + 1

    def _roll_rate(self, now: float) -> None:
        """15분이 바뀌면 진행 중 속도 버킷을 rates.jsonl 로 내보낸다."""
        now_h = rate_slot(now)
        node = self.state.setdefault("rate", {"h": now_h, "m": {}})
        if node.get("h") == now_h:
            return
        if node.get("h") and node.get("m") and not self.read_only:
            self._append_rate(node)
        self.state["rate"] = {"h": now_h, "m": {}}

    def _append_rate(self, node: Dict[str, Any]) -> None:
        try:
            line = json.dumps(node, ensure_ascii=False, separators=(",", ":"))
            with RATES_FILE.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            lines = RATES_FILE.read_text(encoding="utf-8").splitlines()
            if len(lines) > RATES_KEPT:
                RATES_FILE.write_text("\n".join(lines[-RATES_KEPT:]) + "\n", encoding="utf-8")
        except OSError:
            pass

    def _resolve_project(self, delta: TokenDelta) -> None:
        """로그에 cwd 가 없는 서비스는 훅이 남긴 라이브 파일에서 프로젝트를 가져온다.

        Grok 처럼 레코드에 cwd 가 아예 없는 서비스는 사용량이 통째로 '(unknown)' 으로
        뭉쳐 프로젝트 패널이 무의미해진다. 훅은 같은 sessionId 로 cwd 를 이미 적어
        두었으므로 여기서 한 번 이어 붙인다. 델타가 들어오는 유일한 길목이라 이 한 곳만
        고치면 projects / hour 버킷 / 세션 기록이 모두 같은 이름을 쓴다.
        """
        if delta.project or not delta.session:
            return
        key = f"{delta.service}/{delta.session}"
        name = self._project_cache.get(key, "")
        if not name:
            # ponytail: 라이브 파일이 아직 없으면 매 델타마다 다시 본다. 훅이 세션 시작에
            #           바로 쓰므로 실제로는 첫 몇 건뿐이다. 비어 있는 값은 캐시하지 않는다.
            rec = _read_json(live_path(delta.service, delta.session)) or {}
            name = str(rec.get("project") or "") or project_key(rec.get("cwd"))
            if name:
                self._project_cache[key] = name
        delta.project = name

    def ingest(self, delta: TokenDelta) -> Dict[str, Any]:
        """델타를 반영하고 저장한다. 갱신된 누적 totals 를 돌려준다."""
        self._resolve_project(delta)
        now = time.time()
        cost = delta.cost()
        saved = delta.saved()
        self._roll_today()
        self._roll_hour()
        self._roll_rate(now)
        st = self.state
        self._bucket_hour(delta, cost)

        _accumulate(st["total"]["totals"], delta, cost, saved)
        _accumulate(st["session"]["totals"], delta, cost, saved)
        _accumulate(st["today"]["totals"], delta, cost, saved)

        touched: List[Tuple[Dict[str, Any], str]] = []
        for group, name in (
            ("projects", delta.project or "(unknown)"),
            ("services", delta.service or "(unknown)"),
            ("models", delta.model or "default"),
            ("vendors", delta.vendor or "unknown"),
            ("plans", delta.plan or "unknown"),
            ("endpoints", delta.endpoint or "unknown"),
        ):
            node = st.setdefault(group, {}).setdefault(name, {})
            node.setdefault("totals", _totals())
            _accumulate(node["totals"], delta, cost, saved)
            node["last_seen"] = now
            if group == "models" and delta.vendor:
                node["vendor"] = delta.vendor  # 서버가 모델→벤더를 되짚을 수 있게
            touched.append((node, f"{group}:{name}"))

        self._track_session(delta, cost, now, touched, saved)
        self._touch_live(delta)
        st["total"]["last_seen"] = now
        self._save()
        return st["total"]["totals"]

    def _touch_live(self, delta: TokenDelta) -> None:
        """토큰이 들어왔다 = 그 세션은 살아 있다.

        라이브 파일 mtime 은 훅이 찍는데, SessionStart/SessionEnd 밖에 없는 서비스는
        갱신 기회가 없어 `live_ttl_hours` 를 넘긴 장시간 세션이 죽은 것으로 오인된다
        (→ 라이브 0개 → 데몬이 세션 도중 자살). 실제 토큰 유입이 가장 정확한 생존
        신호이므로 여기서도 찍는다. 훅 이벤트에만 기대면 서비스마다 새는 곳이 생긴다.
        """
        if self.read_only or not delta.session:
            return
        try:
            os.utime(live_path(delta.service, delta.session))
        except OSError:
            pass  # 훅 없이 watch 만 도는 경우엔 라이브 파일 자체가 없다

    def _track_session(
        self,
        delta: TokenDelta,
        cost: float,
        now: float,
        touched: List[Tuple[Dict[str, Any], str]],
        saved: float = 0.0,
    ) -> None:
        """세션 단위 집계.

        '어떤 모델로 세션이 가장 많이 돌았나' 는 호출 수와 다른 질문이다. 같은 세션이
        모델을 바꾸면 두 모델 모두 1세션으로 센다 — 그래서 축 조합(tag)마다 한 번씩만
        올린다. 세션 기록은 최근 것만 남기고 오래된 것부터 버린다.
        """
        if not delta.session:
            return
        st = self.state
        key = f"{delta.service or '?'}/{delta.session}"
        book = st.setdefault("sessions", {})
        rec = book.get(key)
        if rec is None:
            rec = {
                "service": delta.service,
                "project": delta.project,
                "vendor": delta.vendor,
                "plan": delta.plan,
                "endpoint": delta.endpoint,
                "started_at": now,
                "seen": [],
                "totals": _totals(),
            }
            book[key] = rec
            st["total"]["sessions"] = int(st["total"].get("sessions", 0)) + 1
        if not delta.subagent:
            # 세션 행의 모델/속도는 메인 에이전트를 뜻한다. 부모 sessionId 를 공유하는
            # 서브에이전트가 이 값을 덮으면 여러 모델의 합산 속도가 한 모델처럼 보인다.
            rec["model"] = delta.model
            if delta.vendor:
                rec["vendor"] = delta.vendor
            if delta.plan:
                rec["plan"] = delta.plan
            if delta.endpoint:
                rec["endpoint"] = delta.endpoint
            if delta.effort:  # 없는 서비스도 있다 — 빈 값으로 덮어써서 지우면 안 된다
                rec["effort"] = delta.effort
        else:  # 전체에는 이미 들어가 있으므로, 메인 출력에서 뺄 몫만 병행 집계한다
            rec["sub_cost"] = _float(rec.get("sub_cost")) + cost
            rec["sub_output_tokens"] = _int(rec.get("sub_output_tokens")) + delta.output_tokens
        if delta.ctx_tokens:  # 누적이 아니라 '마지막 턴의' 점유 — 압축되면 같이 내려간다
            rec["ctx"] = delta.ctx_tokens
            # 창은 세션 안에서 줄지 않는다. 롱컨텍스트 세션은 200k 를 넘겨야 드러나므로
            # (로그에 창 크기가 없다) 한 번 커진 창을 되돌리면 ctx% 가 튄다
            rec["ctx_win"] = max(delta.ctx_window, _int(rec.get("ctx_win")))
        rec["last_seen"] = now
        if delta.output_tokens > 0 and not delta.subagent:
            # 모델 처리량은 메인 모델만 센다. 부모와 sessionId 를 공유하는 서브 스트림까지
            # 넣으면 마지막 서브 모델 옆에 여러 에이전트의 처리량이 합쳐져 보인다.
            stream = rate_key(delta.vendor, delta.model)
            previous = rec.get("out_at") if rec.get("out_model") == stream else 0.0
            gap = active_seconds(previous, now)
            rec["out_at"] = now       # 기존 state/도구가 읽는 마지막 메인 출력 시각
            rec["out_model"] = stream
            if gap > 0:
                book = self.state.setdefault("rate", {"h": rate_slot(now), "m": {}})
                add_rate(book.setdefault("m", {}), delta.vendor, delta.model,
                         delta.output_tokens, gap)
        _accumulate(rec["totals"], delta, cost, saved)
        seen = rec.setdefault("seen", [])
        for node, tag in touched:
            if tag not in seen:
                seen.append(tag)
                node["sessions"] = int(node.get("sessions", 0)) + 1

        # ponytail: 오래된 기록을 버리므로, 상한을 넘길 만큼 조용했던 세션이 되살아나면
        #           세션 수가 한 번 더 세어진다. 상한을 키우는 것 말고는 방법이 없다.
        cap = max(20, int(self.config.setting("session_history", 500) or 500))
        if len(book) > cap:
            for old in sorted(book, key=lambda k: _float(book[k].get("last_seen")))[: len(book) - cap]:
                del book[old]

    def status(self) -> Dict[str, Any]:
        """state.json 전체 + 라이브 세션 정보."""
        live = self.live_sessions()
        out = dict(self.state)
        out["live"] = live
        out["live_count"] = len(live)
        return out

    def reset_stats(self) -> None:
        """누적 통계를 전부 초기화한다 (라이브 세션/히스토리는 건드리지 않는다)."""
        now = time.time()
        self.state["total"] = {
            "started_at": now, "last_seen": 0.0, "sessions": 0, "totals": _totals()
        }
        self.state["session"] = {"started_at": now, "totals": _totals()}
        self.state["today"] = {"date": _today_str(), "totals": _totals()}
        self.state["days"] = {}
        self.state["hour"] = {"h": _hour_str(), "p": {}}
        self.state["rate"] = {"h": rate_slot(now), "m": {}}
        self.state["sessions"] = {}
        for group in GROUPS:
            self.state[group] = {}
        for path in (HOURS_FILE, RATES_FILE):
            try:
                path.unlink()
            except OSError:
                pass
        self._save()

    # ── 라이브 세션 파일 (writer = 훅, reader = 데몬/CLI) ─────────────────
    def _live_path(self, service: str, session_id: str) -> Path:
        return live_path(service, session_id)

    def live_sessions(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        try:
            paths = sorted(LIVE_DIR.glob("*.json"))
        except Exception:
            return out
        for p in paths:
            rec = _read_json(p)
            if rec is None:
                continue
            rec.setdefault("service", p.stem.split("__", 1)[0])
            rec.setdefault("session_id", p.stem.split("__", 1)[-1])
            try:
                rec["updated_at"] = p.stat().st_mtime
            except Exception:
                rec["updated_at"] = 0.0
            out.append(rec)
        return out

    def add_live(self, **kw: Any) -> Path:
        """라이브 세션 파일을 만든다 (수동 start / 테스트용)."""
        service = str(kw.get("service") or "manual")
        session_id = str(kw.get("session_id") or f"{service}-{int(time.time())}")
        cwd = str(kw.get("cwd") or os.getcwd())
        rec = {
            "service": service,
            "session_id": session_id,
            "project": kw.get("project") or project_key(cwd) or "(unknown)",
            "cwd": cwd,
            "model": kw.get("model") or "",
            "started_at": float(kw.get("started_at") or time.time()),
            "event": kw.get("event") or "manual",
        }
        path = self._live_path(service, session_id)
        try:
            path.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        return path

    def remove_live(self, service: str, session_id: str) -> bool:
        path = self._live_path(service, session_id)
        try:
            path.unlink()
            return True
        except Exception:
            return False

    def prune_live(self, ttl_hours: float) -> int:
        """종료 훅을 못 받고 남은 오래된 라이브 파일을 지운다. 지운 개수 반환."""
        if ttl_hours <= 0:
            return 0
        cutoff = time.time() - ttl_hours * 3600.0
        n = 0
        try:
            paths = list(LIVE_DIR.glob("*.json"))
        except Exception:
            return 0
        for p in paths:
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    n += 1
            except Exception:
                continue
        return n

    def archive(self, service: str, session_id: str) -> None:
        """세션 종료 시 스냅샷을 data/history/ 에 남긴다."""
        if self.read_only:
            return
        rec = _read_json(self._live_path(service, session_id)) or {
            "service": service,
            "session_id": session_id,
        }
        ended = time.time()
        rec["ended_at"] = ended
        rec["session_totals"] = dict(self.state["session"]["totals"])
        rec["today_totals"] = dict(self.state["today"]["totals"])
        rec["total_totals"] = dict(self.state["total"]["totals"])
        out = HISTORY_DIR / f"{_safe_name(service)}__{_safe_name(session_id)}__{int(ended)}.json"
        try:
            out.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
