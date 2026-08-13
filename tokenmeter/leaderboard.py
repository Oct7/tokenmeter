"""선택형 자체 호스팅 랭킹 클라이언트 (Application).

`settings.leaderboard.endpoint` 가 비어 있으면 **네트워크를 한 번도 건드리지 않는다.**
그때는 '나' 한 줄짜리 로컬 랭킹으로 돈다. endpoint 를 채우는 순간 같은 코드가
업로드/조회를 시작한다.

올라가는 것은 **핸들 + 토큰·비용·호출·세션 합계 + 모델/벤더/요금제/클라이언트별 내역** 뿐이다.
프로젝트명과 경로는 올리지 않는다 (state.json 의 projects 버킷은 손대지 않는다).

이 축들이 모이면 서버에서 "어떤 벤더를 많이 쓰나 · 어떤 모델이 호출이 많나 ·
어떤 모델로 세션이 많이 도나 · 구독과 종량제 비율은" 을 사용자 간 비교로 답할 수 있다.

writer 는 데몬 하나다. 데몬이 sync() 로 캐시 파일을 갱신하고,
오버레이/CLI 는 cached() 로 읽기만 한다 — state.json 과 같은 규칙이다.
"""

from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import DATA_DIR, Config
from .endpoints import classify
from .meter import attention_counts, sessions_today
from .meter import tokens_of as _sum_tokens

CACHE_FILE = DATA_DIR / "leaderboard.json"
TIMEOUT = 4.0
SCOPES = ("today", "total")
TOKEN_KEYS = ("input_tokens", "cache_read", "cache_write", "output_tokens")


@dataclass
class Entry:
    """랭킹 한 줄. 서버에서 왔든 내 로컬 상태에서 왔든 같은 모양이다."""

    handle: str
    tokens: int = 0
    cost_usd: float = 0.0
    me: bool = False


@dataclass
class TeamEntry:
    handle: str
    check: int = 0
    working: int = 0
    waiting: int = 0
    risk: int = 0
    cost_usd: float = 0.0
    me: bool = False


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _count(v: Any) -> int:
    try:
        value = float(v)
    except (TypeError, ValueError, OverflowError):
        return 0
    return int(value) if math.isfinite(value) else 0


def _totals(node: Any) -> Dict[str, float]:
    """{...} 또는 {"totals": {...}} 어느 쪽으로 와도 합계 dict 를 꺼낸다."""
    if not isinstance(node, dict):
        return {}
    inner = node.get("totals")
    return inner if isinstance(inner, dict) else node


def tokens_of(totals: Any) -> int:
    """{...} 든 {"totals": {...}} 든 받아 토큰 합을 낸다."""
    return _sum_tokens(_totals(totals))


def cost_of(totals: Any) -> float:
    return _num(_totals(totals).get("cost_usd"))


def bucket(node: Any) -> Dict[str, Any]:
    """합계 dict → 업로드용 평평한 숫자 묶음."""
    t = _totals(node)
    out = {k: int(_num(t.get(k))) for k in TOKEN_KEYS}
    out["cost_usd"] = round(cost_of(t), 8)
    out["calls"] = int(_num(t.get("calls")))
    return out


def _endpoints(state: Dict[str, Any], public: Iterable[str]) -> Dict[str, Any]:
    """엔드포인트 축은 **분류해서** 올린다.

    알려진 공개 엔드포인트만 호스트 이름으로 남고, 사내 게이트웨이는 전부
    self-hosted 한 칸으로 합쳐진다. 합치는 과정에서 숫자는 더해진다.
    """
    raw = state.get("endpoints")
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Any] = {}
    for url, node in raw.items():
        label = classify(str(url), public)
        entry = bucket(node)
        entry["sessions"] = int(_num((node or {}).get("sessions")))
        merged = out.setdefault(label, {k: 0 for k in entry})
        for k, v in entry.items():
            merged[k] = round(merged.get(k, 0) + v, 8) if k == "cost_usd" else merged.get(k, 0) + v
    return out


def _group(state: Dict[str, Any], name: str, cap: int) -> Dict[str, Any]:
    """축 하나를 비용 순으로 잘라 올린다. 축별 세션 수·벤더 표기를 함께 싣는다."""
    raw = state.get(name)
    if not isinstance(raw, dict):
        return {}
    items = sorted(raw.items(), key=lambda kv: -cost_of(kv[1]))[:cap]
    out: Dict[str, Any] = {}
    for key, node in items:
        entry = bucket(node)
        entry["sessions"] = int(_num((node or {}).get("sessions")))
        if isinstance(node, dict) and node.get("vendor"):
            entry["vendor"] = str(node["vendor"])
        out[str(key)] = entry
    return out


def payload(state: Dict[str, Any], handle: str, public: Iterable[str] = ()) -> Dict[str, Any]:
    """업로드 본문. 여기 없는 것은 서버로 나가지 않는다 (프로젝트명·경로는 없다).

    `public` = 관리자가 검증해 이름 그대로 올려도 된다고 정한 호스트 목록.
    """
    today = dict(bucket(state.get("today")))
    today["date"] = str((state.get("today") or {}).get("date", ""))
    today["sessions"] = sessions_today(state)
    today["attention"] = attention_counts(state)
    total = dict(bucket(state.get("total")))
    total["sessions"] = int(_num((state.get("total") or {}).get("sessions")))
    return {
        "handle": handle,
        "updated_at": time.time(),
        "today": today,
        "total": total,
        "models": _group(state, "models", 40),
        "vendors": _group(state, "vendors", 20),
        "plans": _group(state, "plans", 10),
        "clients": _group(state, "services", 20),  # 어떤 CLI 로 쓰는지
        "endpoints": _endpoints(state, public),  # 분류된 라벨만
    }


def parse_entries(raw: Any, scope: str, me: str) -> List[Entry]:
    """서버 응답 → 정렬된 Entry 목록.

    응답 모양은 우리가 올리는 payload 와 같다고 본다 (리스트, 또는 {"entries": [...]}).
    """
    rows = raw.get("entries") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return []
    out: List[Entry] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        handle = str(row.get("handle") or "").strip()
        if not handle:
            continue
        node = row.get(scope if scope in SCOPES else "total")
        out.append(
            Entry(handle=handle, tokens=tokens_of(node), cost_usd=cost_of(node), me=handle == me)
        )
    return rank(out)


def rank(entries: List[Entry]) -> List[Entry]:
    """비용 내림차순. 같으면 토큰, 그다음 핸들 — 정렬이 매 프레임 흔들리면 안 된다."""
    return sorted(entries, key=lambda e: (-e.cost_usd, -e.tokens, e.handle))


def parse_team_entries(raw: Any, me: str) -> List[TeamEntry]:
    """서버 응답의 오늘 관심 집계를 팀 표 행으로 정규화한다."""
    rows = raw.get("entries") if isinstance(raw, dict) else raw
    out: List[TeamEntry] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        handle = str(row.get("handle") or "").strip()
        if not handle:
            continue
        today = row.get("today") if isinstance(row.get("today"), dict) else {}
        attention = today.get("attention") if isinstance(today.get("attention"), dict) else {}
        out.append(TeamEntry(
            handle=handle,
            check=_count(attention.get("check")),
            working=_count(attention.get("working")),
            waiting=_count(attention.get("waiting")),
            risk=_count(attention.get("risk")),
            cost_usd=cost_of(today),
            me=handle == me,
        ))
    return team_rank(out)


def team_rank(entries: List[TeamEntry]) -> List[TeamEntry]:
    return sorted(entries, key=lambda e: (-e.check, -e.risk, -e.working, -e.cost_usd, e.handle))


def my_entry(state: Dict[str, Any], handle: str, scope: str) -> Entry:
    node = state.get(scope if scope in SCOPES else "total")
    return Entry(handle=handle, tokens=tokens_of(node), cost_usd=cost_of(node), me=True)


def _default_handle() -> str:
    import getpass

    try:
        return getpass.getuser() or "me"
    except Exception:
        return "me"


class Leaderboard:
    """설정을 읽어 업로드/조회하고 결과를 data/leaderboard.json 에 캐싱한다."""

    def __init__(self, config: Optional[Config] = None) -> None:
        cfg = config
        self.endpoint = str((cfg.setting("leaderboard.endpoint", "") if cfg else "") or "").strip()
        self.handle = str((cfg.setting("leaderboard.handle", "") if cfg else "") or "").strip() or _default_handle()
        self.sync_seconds = float((cfg.setting("leaderboard.sync_seconds", 60) if cfg else 60) or 60)
        headers = (cfg.setting("leaderboard.headers", {}) if cfg else {}) or {}
        self.headers = {str(k): str(v) for k, v in headers.items()} if isinstance(headers, dict) else {}
        # 관리자가 검증해 이름 그대로 올려도 된다고 정한 호스트. 나머지는 self-hosted 로 뭉갠다
        public = (cfg.setting("leaderboard.public_endpoints", []) if cfg else []) or []
        self.public: List[str] = [str(h) for h in public] if isinstance(public, list) else []
        self._last_sync = 0.0
        self._cache: Dict[str, Any] = {}
        self._mtime = -1.0

    @property
    def online(self) -> bool:
        return bool(self.endpoint)

    # ── HTTP (표준 라이브러리만 — 훅 경로에서 import 돼도 안전해야 한다) ────
    def _request(self, method: str, body: Optional[bytes]) -> Any:
        req = urllib.request.Request(self.endpoint, data=body, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        for k, v in self.headers.items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            text = resp.read().decode("utf-8", errors="replace").strip()
        return json.loads(text) if text else None

    # ── 동기화 (데몬 전용) ────────────────────────────────────────────────
    def sync(self, state: Dict[str, Any], force: bool = False) -> Dict[str, Any]:
        """올리고 받아서 캐시에 쓴다. 실패해도 예외를 던지지 않는다 — 측정은 계속돼야 한다."""
        now = time.time()
        if not force and now - self._last_sync < self.sync_seconds:
            return self._cache
        self._last_sync = now
        if not self.online:
            return self._cache  # 올릴 곳이 없다 — 파일도 건드리지 않는다
        try:
            body = json.dumps(payload(state, self.handle, self.public), ensure_ascii=False)
            self._request("POST", body.encode("utf-8"))
            raw = self._request("GET", None)
        # RuntimeError: payload 를 만드는 사이 워처 스레드가 models 를 늘리면 난다.
        # 여기서 예외가 새면 데몬의 유휴 루프가 죽어 자동 종료까지 멈춘다.
        except (urllib.error.URLError, OSError, ValueError, RuntimeError) as exc:
            return self._write_cache(None, f"동기화 실패: {type(exc).__name__}")
        rows = raw.get("entries") if isinstance(raw, dict) else raw
        return self._write_cache(rows if isinstance(rows, list) else [], "")

    def _write_cache(self, rows: Optional[List[Any]], status: str) -> Dict[str, Any]:
        """rows=None 이면 지난 응답을 유지한다 — 한 번 끊겼다고 랭킹이 사라지면 안 된다."""
        data = {
            "fetched_at": time.time() if rows is not None else _num(self._cache.get("fetched_at")),
            "entries": rows if rows is not None else (self._cache.get("entries") or []),
            "status": status,
            "handle": self.handle,
        }
        self._cache = data
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = CACHE_FILE.with_name(f"{CACHE_FILE.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, CACHE_FILE)
            self._mtime = CACHE_FILE.stat().st_mtime
        except OSError:
            pass
        return data

    # ── 조회 (오버레이/CLI) ───────────────────────────────────────────────
    def cached(self) -> Dict[str, Any]:
        """캐시 파일을 mtime 이 바뀔 때만 다시 읽는다 (오버레이가 200ms 마다 부른다)."""
        try:
            mtime = CACHE_FILE.stat().st_mtime
        except OSError:
            return self._cache
        if mtime != self._mtime:
            self._mtime = mtime
            try:
                obj = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
                self._cache = obj if isinstance(obj, dict) else {}
            except (OSError, json.JSONDecodeError):
                pass
        return self._cache

    def board(self, state: Dict[str, Any], scope: str = "today") -> Tuple[List[Entry], str]:
        """(랭킹, 상태문구). 서버에 내 행이 아직 없으면 로컬 값으로 채워 넣는다."""
        cache = self.cached()
        mine = my_entry(state, self.handle, scope)
        entries = parse_entries(cache.get("entries"), scope, self.handle)
        if not any(e.me for e in entries):
            entries = rank(entries + [mine])
        else:  # 서버 값은 최대 sync_seconds 만큼 낡았다 — 내 행만 로컬 값으로 앞당긴다
            entries = rank([mine if e.me else e for e in entries])
        if not self.online:
            return entries, "혼자 달리는 중 · endpoint 를 채우면 참가"
        status = str(cache.get("status") or "")
        if status:
            return entries, status
        fetched = _num(cache.get("fetched_at"))
        when = time.strftime("%H:%M:%S", time.localtime(fetched)) if fetched else "-"
        return entries, f"동기화 {when} · {len(entries)}명"

    def team(self, state: Dict[str, Any]) -> Tuple[List[TeamEntry], str]:
        """(팀 현황, 상태문구). 내 행은 현재 라이브 집계로 덮어쓴다."""
        counts = attention_counts(state)
        mine = TeamEntry(
            handle=self.handle, cost_usd=cost_of(state.get("today")), me=True, **counts,
        )
        if not self.online:
            return [mine], "혼자 달리는 중 · endpoint 를 채우면 참가"
        cache = self.cached()
        entries = parse_team_entries(cache.get("entries"), self.handle)
        entries = team_rank(entries + [mine] if not any(e.me for e in entries) else [
            mine if e.me else e for e in entries
        ])
        status = str(cache.get("status") or "")
        if status:
            return entries, status
        fetched = _num(cache.get("fetched_at"))
        when = time.strftime("%H:%M:%S", time.localtime(fetched)) if fetched else "-"
        return entries, f"동기화 {when} · {len(entries)}명"


def _demo() -> None:
    """python3 -m tokenmeter.leaderboard — 랭킹 정렬/병합/오프라인 폴백 자가 검증."""
    global CACHE_FILE
    import tempfile

    tmp_dir = tempfile.TemporaryDirectory(prefix="tokenmeter-lb-")
    CACHE_FILE = Path(tmp_dir.name) / "leaderboard.json"  # 진짜 data/ 를 안 건드린다
    state = {
        "today": {"date": "2026-08-10", "totals": {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.5}},
        "total": {"totals": {"input_tokens": 100, "output_tokens": 50, "cost_usd": 9.0}, "sessions": 3},
        "models": {
            "claude-opus-5": {"totals": {"input_tokens": 100, "cost_usd": 9.0, "calls": 12},
                              "sessions": 3, "vendor": "anthropic"},
            "gpt-5.6-sol": {"totals": {"cost_usd": 1.0, "calls": 40}, "sessions": 1, "vendor": "openai"},
        },
        "vendors": {"anthropic": {"totals": {"cost_usd": 9.0, "calls": 12}, "sessions": 3}},
        "plans": {"subscription": {"totals": {"cost_usd": 9.0, "calls": 12}, "sessions": 3}},
        "services": {"claude-code": {"totals": {"cost_usd": 9.0, "calls": 12}, "sessions": 3}},
        "endpoints": {
            "https://api.anthropic.com": {"totals": {"cost_usd": 9.0, "calls": 12}, "sessions": 3},
            "https://llm.example.test/v1": {"totals": {"cost_usd": 1.0, "calls": 5}, "sessions": 1},
            "https://api.example.test/v1": {"totals": {"cost_usd": 2.0, "calls": 7}, "sessions": 2},
        },
    }
    # 업로드 본문에는 합계와 축별 내역만 담긴다 (프로젝트는 절대 나가지 않는다)
    body = payload(dict(state, projects={"secret-client": {"totals": {"cost_usd": 1.0}}}), "alice")
    assert set(body) == {"handle", "updated_at", "today", "total", "models",
                         "vendors", "plans", "clients", "endpoints"}, sorted(body)
    # 사내 게이트웨이 두 곳은 주소가 지워진 채 한 칸으로 합쳐진다
    assert set(body["endpoints"]) == {"api.anthropic.com", "self-hosted"}, body["endpoints"]
    assert "example" not in json.dumps(body) and "api-service" not in json.dumps(body)
    assert body["endpoints"]["self-hosted"]["calls"] == 12  # 5 + 7
    assert body["endpoints"]["self-hosted"]["sessions"] == 3  # 1 + 2
    assert body["endpoints"]["api.anthropic.com"]["cost_usd"] == 9.0
    # 관리자가 검증한 호스트는 그때부터 이름 그대로 올라간다
    verified = payload(state, "alice", ["api.example.test"])
    assert set(verified["endpoints"]) == {"api.anthropic.com", "api.example.test", "self-hosted"}
    assert verified["endpoints"]["self-hosted"]["calls"] == 5
    assert "projects" not in json.dumps(body) and "secret-client" not in json.dumps(body)
    assert body["today"]["cost_usd"] == 0.5 and body["total"]["input_tokens"] == 100
    assert body["total"]["sessions"] == 3
    # 축 내역: 호출 수·세션 수·벤더가 함께 올라가고 비용 순으로 잘린다
    assert list(body["models"]) == ["claude-opus-5", "gpt-5.6-sol"], list(body["models"])
    assert body["models"]["gpt-5.6-sol"]["calls"] == 40
    assert body["models"]["claude-opus-5"]["sessions"] == 3
    assert body["models"]["gpt-5.6-sol"]["vendor"] == "openai"
    assert body["vendors"]["anthropic"]["calls"] == 12
    assert body["plans"]["subscription"]["sessions"] == 3
    assert body["clients"]["claude-code"]["cost_usd"] == 9.0

    # 정렬: 비용 내림차순, 동점이면 토큰 → 핸들
    ranked = rank([Entry("a", 10, 1.0), Entry("b", 30, 2.0), Entry("c", 99, 1.0)])
    assert [e.handle for e in ranked] == ["b", "c", "a"], [e.handle for e in ranked]

    rows = [body, {"handle": "friend", "today": {"cost_usd": 99.0, "input_tokens": 7}}]
    got = parse_entries({"entries": rows}, "today", "alice")
    assert [e.handle for e in got] == ["friend", "alice"]
    assert got[1].me and got[1].tokens == 15
    assert parse_entries([{"nope": 1}, "쓰레기"], "today", "alice") == []

    # 오프라인: 네트워크를 건드리지 않고 나 혼자 나온다
    lb = Leaderboard(None)
    lb.endpoint = ""
    lb.handle = "alice"
    lb._mtime, lb._cache = 0.0, {}
    entries, note = lb.board(state, "total")
    assert len(entries) == 1 and entries[0].me and entries[0].cost_usd == 9.0
    assert "endpoint" in note

    # 서버에 내 행이 있으면 로컬 값으로 덮어써 최신을 보여준다
    lb._cache = {"entries": [{"handle": "alice", "total": {"cost_usd": 0.01}}], "fetched_at": 1.0}
    lb._mtime = -2.0  # 파일이 없으므로 캐시가 그대로 쓰인다
    lb.endpoint = "http://127.0.0.1:1/board"  # 즉시 거절되는 주소 — 네트워크를 타지 않는다
    entries, _ = lb.board(state, "total")
    assert entries[0].cost_usd == 9.0, entries[0]

    # 끊겨도 지난 랭킹은 유지된다
    lb.sync(state, force=True)
    assert lb._cache["entries"], "동기화 실패로 랭킹을 비우면 안 된다"
    assert lb._cache["status"].startswith("동기화 실패")
    assert CACHE_FILE.exists()
    tmp_dir.cleanup()
    print("leaderboard.py 자가 검증 통과")


if __name__ == "__main__":
    _demo()
