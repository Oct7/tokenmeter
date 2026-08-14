"""벤더·모델별 활성 출력 tok/s 시계열 (Application).

작업 중인 구간(연속 출력 유입)만 잰다. 벽시계가 아니라 활성 초로 나눈다.
Qt 를 모른다 — 집계만 하고 그리는 일은 overlay 가 한다.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .config import RATES_FILE

RATE_ACTIVE_SECONDS = 30.0
RATE_SLOT_SECONDS = 15 * 60
RATES_KEPT = 4 * 24 * 60  # 15분 버킷 60일
RATE_SPANS = ("1h", "4h", "1d", "7d")
RATE_SPAN_TITLES = {"1h": "1시간", "4h": "4시간", "1d": "1일", "7d": "7일"}
RATE_WINDOWS = {"1h": 3600, "4h": 4 * 3600, "1d": 24 * 3600, "7d": 7 * 24 * 3600}
RATE_BAR = {"1h": RATE_SLOT_SECONDS, "4h": RATE_SLOT_SECONDS, "1d": 3600, "7d": 24 * 3600}

RateBucket = Tuple[str, Dict[str, Any]]  # (슬롯키, {vendor/model: [tokens, active_sec, bursts]})


@dataclass
class RateRow:
    vendor: str
    model: str
    tokens: int = 0
    active_sec: float = 0.0
    rate: float = 0.0
    share: float = 0.0


@dataclass
class RateBar:
    label: str
    tokens: int = 0
    active_sec: float = 0.0
    rate: float = 0.0


@dataclass
class RateSeries:
    span: str
    start: float = 0.0
    end: float = 0.0
    shifted: bool = False
    rows: List[RateRow] = field(default_factory=list)
    bars: List[RateBar] = field(default_factory=list)
    peak: float = 0.0
    total_tokens: int = 0
    total_active: float = 0.0


def _float(v: Any) -> float:
    try:
        value = float(v)
    except (TypeError, ValueError):
        return 0.0
    return value if value == value and abs(value) != float("inf") else 0.0


def _int(v: Any) -> int:
    return int(_float(v))


def rate_slot(ts: float) -> str:
    """'2026-08-14T15:00' — 15분 버킷 키."""
    t = time.localtime(ts)
    minute = t.tm_min - (t.tm_min % 15)
    return time.strftime("%Y-%m-%dT%H:", t) + f"{minute:02d}"


def slot_start(key: str) -> float:
    try:
        return time.mktime(time.strptime(key, "%Y-%m-%dT%H:%M"))
    except (TypeError, ValueError, OverflowError, OSError):
        return 0.0


def slot_end(key: str) -> float:
    start = slot_start(key)
    return start + RATE_SLOT_SECONDS if start else 0.0


def active_seconds(prev_out_at: Any, now: float) -> float:
    """이전 출력이 작업 창 안이면 그 간격, 아니면 0."""
    prev = _float(prev_out_at)
    if prev <= 0:
        return 0.0
    gap = now - prev
    if 0 < gap <= RATE_ACTIVE_SECONDS:
        return gap
    return 0.0


def rate_key(vendor: Any, model: Any) -> str:
    v = str(vendor or "unknown").strip() or "unknown"
    m = str(model or "default").strip() or "default"
    return f"{v}/{m}"


def split_rate_key(name: str) -> Tuple[str, str]:
    vendor, sep, model = str(name).partition("/")
    if not sep:
        return "unknown", vendor or "default"
    return vendor or "unknown", model or "default"


def add_rate(book: Dict[str, Any], vendor: Any, model: Any, tokens: int, active: float) -> None:
    if tokens <= 0 or active <= 0:
        return
    cell = book.get(rate_key(vendor, model))
    if not isinstance(cell, list) or len(cell) < 2:
        cell = [0, 0.0, 0]
        book[rate_key(vendor, model)] = cell
    cell[0] = _int(cell[0]) + int(tokens)
    cell[1] = round(_float(cell[1]) + float(active), 6)
    cell[2] = _int(cell[2] if len(cell) > 2 else 0) + 1


def _cell_parts(cell: Any) -> Tuple[int, float]:
    if not isinstance(cell, (list, tuple)) or len(cell) < 2:
        return 0, 0.0
    return max(0, _int(cell[0])), max(0.0, _float(cell[1]))


# ── 읽기 ──────────────────────────────────────────────────────────────────
_cache: Dict[str, Any] = {"key": None, "rows": []}


def _load_file() -> List[RateBucket]:
    try:
        st = RATES_FILE.stat()
        key = (str(RATES_FILE), st.st_mtime, st.st_size)
    except OSError:
        _cache["key"], _cache["rows"] = None, []
        return []
    if _cache["key"] == key:
        return list(_cache["rows"])
    rows: List[RateBucket] = []
    try:
        with RATES_FILE.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("h") and isinstance(rec.get("m"), dict):
                    rows.append((str(rec["h"]), rec["m"]))
    except OSError:
        return []
    _cache["key"], _cache["rows"] = key, rows
    return list(rows)


def load_rates(state: Optional[Dict[str, Any]] = None) -> List[RateBucket]:
    rows = _load_file()
    node = (state or {}).get("rate")
    if isinstance(node, dict) and node.get("h") and isinstance(node.get("m"), dict):
        if not rows or rows[-1][0] != node["h"]:
            rows.append((str(node["h"]), node["m"]))
    return rows


def _latest_end(buckets: List[RateBucket]) -> float:
    latest = 0.0
    for key, book in buckets:
        if not isinstance(book, dict):
            continue
        if any(_cell_parts(cell)[0] > 0 for cell in book.values()):
            latest = max(latest, slot_end(key))
    return latest


def _window(span: str, now: float, buckets: List[RateBucket]) -> Tuple[float, float, bool]:
    width = RATE_WINDOWS.get(span, RATE_WINDOWS["1h"])
    end = now
    start = end - width
    if _tokens_in(buckets, start, end) > 0:
        return start, end, False
    last = _latest_end(buckets)
    if last <= 0:
        return start, end, False
    return last - width, last, True


def _tokens_in(buckets: List[RateBucket], start: float, end: float) -> int:
    total = 0
    for key, book in buckets:
        t = slot_start(key)
        if t < start or t >= end:
            continue
        if not isinstance(book, dict):
            continue
        total += sum(_cell_parts(cell)[0] for cell in book.values())
    return total


def _bar_label(span: str, t: float) -> str:
    if span == "7d":
        return time.strftime("%m-%d", time.localtime(t))
    if span == "1d":
        return time.strftime("%H", time.localtime(t))
    return time.strftime("%H:%M", time.localtime(t))


def _align(ts: float, step: int) -> float:
    # 로컬 시각 기준으로 칸을 맞춘다. UTC 나눗셈은 타임존에서 칸이 어긋난다.
    t = time.localtime(ts)
    if step >= 24 * 3600:
        return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, t.tm_isdst))
    if step >= 3600:
        return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, 0, 0, 0, 0, t.tm_isdst))
    minute = t.tm_min - (t.tm_min % max(1, step // 60))
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, minute, 0, 0, 0, t.tm_isdst))


def rate_series(
    buckets: List[RateBucket],
    span: str = "1h",
    now: Optional[float] = None,
) -> RateSeries:
    span = span if span in RATE_SPANS else "1h"
    now = time.time() if now is None else now
    start, end, shifted = _window(span, now, buckets)
    step = RATE_BAR[span]
    origin = _align(start, step)
    bars: List[RateBar] = []
    index: Dict[float, RateBar] = {}
    t = origin
    while t < end:
        bar = RateBar(label=_bar_label(span, t))
        bars.append(bar)
        index[t] = bar
        t += step

    weight: Dict[str, List[float]] = {}
    for key, book in buckets:
        if not isinstance(book, dict):
            continue
        slot = slot_start(key)
        if slot < start or slot >= end:
            continue
        bar_at = _align(slot, step)
        bar = index.get(bar_at)
        for name, cell in book.items():
            tokens, active = _cell_parts(cell)
            if tokens <= 0 and active <= 0:
                continue
            node = weight.setdefault(str(name), [0.0, 0.0])
            node[0] += tokens
            node[1] += active
            if bar is not None:
                bar.tokens += tokens
                bar.active_sec += active

    for bar in bars:
        bar.rate = (bar.tokens / bar.active_sec) if bar.active_sec > 0 else 0.0

    total_tokens = int(sum(v[0] for v in weight.values()))
    total_active = sum(v[1] for v in weight.values())
    rows = []
    for name, (tokens, active) in weight.items():
        vendor, model = split_rate_key(name)
        rate = (tokens / active) if active > 0 else 0.0
        rows.append(RateRow(
            vendor=vendor, model=model, tokens=int(tokens),
            active_sec=active, rate=rate,
            share=(tokens / total_tokens) if total_tokens else 0.0,
        ))
    rows.sort(key=lambda row: (-row.tokens, row.vendor, row.model))
    return RateSeries(
        span=span, start=start, end=end, shifted=shifted, rows=rows, bars=bars,
        peak=max((bar.rate for bar in bars), default=0.0),
        total_tokens=total_tokens, total_active=total_active,
    )


def rate_summary(s: RateSeries) -> str:
    if s.total_active <= 0:
        return "아직 작업 속도 기록이 없습니다"
    avg = s.total_tokens / s.total_active
    when = time.strftime("%m-%d %H:%M", time.localtime(s.start))
    prefix = "마지막 기록 · " if s.shifted else ""
    return f"{prefix}{avg:.1f} tok/s · {s.total_tokens:,.0f}토큰 · {when}"
