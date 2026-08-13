"""시간별 히스토리 집계 (Application 레이어).

`data/hours.jsonl` 의 시간 버킷을 읽어 그래프가 바로 그릴 수 있는 막대 목록으로
바꾼다. Qt 를 모른다 — 좌표 계산까지만 하고 칠하는 일은 overlay 가 한다.

    hours = load_hours(state)
    s = series(hours, "today")
    for bar in s.bars: ...   # bar.total / s.peak 가 막대 높이 비율
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .config import HOURS_FILE

SPANS = ("today", "7d", "30d")
SPAN_TITLES = {"today": "오늘", "7d": "7일", "30d": "30일"}
TOP_PROJECTS = 6  # 색으로 구분할 프로젝트 수. 나머지는 '기타' 로 묶는다
OTHER = "기타"

Bucket = Tuple[str, Dict[str, Any]]  # (시간키 "2026-08-11T15", {프로젝트: [토큰, 비용, 호출]})


@dataclass
class Bar:
    """막대 하나. total 이 높이, parts 가 아래부터 쌓이는 구간이다."""

    label: str
    total: float = 0.0
    parts: List[Tuple[str, float]] = field(default_factory=list)
    calls: int = 0
    tokens: int = 0


@dataclass
class Series:
    bars: List[Bar]
    peak: float = 0.0  # 0 이면 그릴 것이 없다
    projects: List[str] = field(default_factory=list)  # 합계 큰 순 — 색 배정 순서
    total: float = 0.0


def _float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _int(v: Any) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


# ── 읽기 ──────────────────────────────────────────────────────────────────
_cache: Dict[str, Any] = {"key": None, "rows": []}


def _load_file() -> List[Bucket]:
    """hours.jsonl 파싱. mtime·크기가 그대로면 다시 읽지 않는다 (200ms 폴링).

    깨진 줄은 건너뛴다 — 사람이 열어볼 수 있는 파일이고, 한 줄 때문에 그래프
    전체가 사라지면 안 된다.
    """
    try:
        st = HOURS_FILE.stat()
        key = (str(HOURS_FILE), st.st_mtime, st.st_size)
    except OSError:
        _cache["key"], _cache["rows"] = None, []
        return []
    if _cache["key"] == key:
        return _cache["rows"]  # type: ignore[return-value]
    rows: List[Bucket] = []
    try:
        with HOURS_FILE.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("h") and isinstance(rec.get("p"), dict):
                    rows.append((str(rec["h"]), rec["p"]))
    except OSError:
        return []
    _cache["key"], _cache["rows"] = key, rows
    return rows


def load_hours(state: Optional[Dict[str, Any]] = None) -> List[Bucket]:
    """파일에 확정된 시간들 + 진행 중인 한 시간(state 안에 있다)."""
    rows = list(_load_file())
    node = (state or {}).get("hour")
    if isinstance(node, dict) and node.get("h") and isinstance(node.get("p"), dict):
        if not rows or rows[-1][0] != node["h"]:  # 데몬이 막 롤한 직후의 중복 방지
            rows.append((str(node["h"]), node["p"]))
    return rows


# ── 집계 ──────────────────────────────────────────────────────────────────
def _slots(span: str, now: Optional[float] = None) -> List[Tuple[str, str]]:
    """(막대 키, 라벨) 목록. 데이터가 없는 칸도 0 으로 남겨야 축이 균등하다."""
    today = date.fromtimestamp(now) if now else date.today()
    if span == "today":
        stamp = today.isoformat()
        return [(f"{stamp}T{h:02d}", f"{h:02d}") for h in range(24)]
    days = 7 if span == "7d" else 30
    out = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        out.append((d.isoformat(), d.strftime("%m-%d")))
    return out


def _key_of(hour: str, span: str) -> str:
    return hour if span == "today" else hour[:10]  # 일별은 날짜까지만


def series(
    hours: List[Bucket],
    span: str = "today",
    project: Optional[str] = None,
    now: Optional[float] = None,
) -> Series:
    """버킷 목록 → 막대 목록. 값은 비용(USD)이다.

    project 를 주면 그 프로젝트만 남긴다 (세션 행을 클릭했을 때).
    """
    span = span if span in SPANS else SPANS[0]
    slots = _slots(span, now)
    index = {key: Bar(label) for key, label in slots}
    weight: Dict[str, float] = {}

    for hour, book in hours:
        bar = index.get(_key_of(hour, span))
        if bar is None:  # 보관 범위 밖이거나 아직 오지 않은 시간
            continue
        for name, cell in book.items():
            if project is not None and name != project:
                continue
            if not isinstance(cell, (list, tuple)) or len(cell) < 3:
                continue
            cost = _float(cell[1])
            bar.total += cost
            bar.tokens += _int(cell[0])
            bar.calls += _int(cell[2])
            bar.parts.append((str(name), cost))
            weight[str(name)] = weight.get(str(name), 0.0) + cost

    ranked = sorted(weight, key=lambda n: weight[n], reverse=True)
    top = ranked[:TOP_PROJECTS]
    for bar in index.values():
        bar.parts = _fold(bar.parts, top)
    bars = [index[key] for key, _ in slots]
    return Series(
        bars=bars,
        peak=max((b.total for b in bars), default=0.0),
        projects=top + ([OTHER] if len(ranked) > len(top) else []),
        total=round(sum(b.total for b in bars), 6),
    )


def _fold(parts: List[Tuple[str, float]], top: List[str]) -> List[Tuple[str, float]]:
    """같은 프로젝트를 합치고, 색이 모자란 나머지는 '기타' 하나로 묶는다."""
    merged: Dict[str, float] = {}
    for name, value in parts:
        key = name if name in top else OTHER
        merged[key] = merged.get(key, 0.0) + value
    order = {name: i for i, name in enumerate(top)}
    return sorted(merged.items(), key=lambda kv: order.get(kv[0], len(top)))


def summary(s: Series) -> str:
    """그래프 위에 붙는 한 줄."""
    if s.peak <= 0:
        return ""
    calls = sum(b.calls for b in s.bars)
    return f"${s.total:,.2f} · {calls:,}호출"


# ── 자가 검증 ─────────────────────────────────────────────────────────────
def demo() -> None:
    """python3 -m tokenmeter.history"""
    stamp = time.mktime((2026, 8, 11, 15, 0, 0, 0, 0, -1))
    rows: List[Bucket] = [
        ("2026-08-11T09", {"a": [100, 1.0, 2]}),
        ("2026-08-11T15", {"a": [50, 2.0, 1], "b": [10, 0.5, 1]}),
        ("2026-08-10T15", {"a": [70, 4.0, 3]}),
    ]

    s = series(rows, "today", now=stamp)
    assert len(s.bars) == 24 and s.bars[0].label == "00"
    assert s.bars[9].total == 1.0 and s.bars[15].total == 2.5, [b.total for b in s.bars]
    assert s.bars[15].calls == 2 and s.bars[15].tokens == 60
    assert s.peak == 2.5 and s.total == 3.5, s
    assert s.bars[0].total == 0.0, "빈 시간도 칸을 차지한다"
    assert "2026-08-10" not in [b.label for b in s.bars], "오늘 span 은 어제를 안 먹는다"

    d = series(rows, "7d", now=stamp)
    assert len(d.bars) == 7 and d.bars[-1].label == "08-11"
    assert d.bars[-1].total == 3.5 and d.bars[-2].total == 4.0, [b.total for b in d.bars]
    assert d.projects == ["a", "b"], d.projects  # 합계 큰 순

    only = series(rows, "today", project="b", now=stamp)
    assert only.total == 0.5 and only.bars[15].parts == [("b", 0.5)]

    # 프로젝트가 색보다 많으면 나머지는 '기타' 로 접힌다
    many = [("2026-08-11T15", {f"p{i}": [1, float(10 - i), 1] for i in range(9)})]
    m = series(many, "today", now=stamp)
    assert m.projects[-1] == OTHER and len(m.projects) == TOP_PROJECTS + 1, m.projects
    assert m.bars[15].parts[-1][0] == OTHER
    assert round(m.bars[15].parts[-1][1], 6) == 4.0 + 3.0 + 2.0, m.bars[15].parts  # p6~p8

    # 빈 입력에도 축은 남는다
    empty = series([], "30d", now=stamp)
    assert len(empty.bars) == 30 and empty.peak == 0.0 and summary(empty) == ""

    # 파일 읽기 — 깨진 줄은 건너뛰고, 진행 중 버킷은 뒤에 붙되 두 번 세지 않는다
    import tempfile
    from pathlib import Path

    global HOURS_FILE
    original, live = HOURS_FILE, {"a": [1, 1.0, 1]}
    with tempfile.TemporaryDirectory() as tmp:
        HOURS_FILE = Path(tmp) / "hours.jsonl"
        _cache["key"] = None
        HOURS_FILE.write_text(
            '{"h":"2026-08-11T16","p":{"a":[1,1.0,1]}}\n깨진 줄\n{"h":"nope"}\n',
            encoding="utf-8",
        )
        assert len(load_hours(None)) == 1, "깨진 줄 하나로 그래프가 사라지면 안 된다"
        assert len(load_hours({"hour": {"h": "2026-08-11T16", "p": live}})) == 1, \
            "데몬이 막 롤한 직후 같은 시간이 두 번 들어가면 안 된다"
        assert len(load_hours({"hour": {"h": "2026-08-11T17", "p": live}})) == 2
    HOURS_FILE, _cache["key"] = original, None

    print("history 자가 검증 통과")


if __name__ == "__main__":
    demo()
