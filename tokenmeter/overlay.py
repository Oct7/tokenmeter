"""토큰 미터 + 선택형 자체 호스팅 랭킹 오버레이 (Presentation).

시각 요소는 QPainter로 그리고, 동작 영역은 네이티브 QToolButton으로 겹쳐 키보드와
스크린리더에서도 조작한다. 새 의존성은 없다.

  · 위쪽은 **미터기** — 세그먼트 게이지는 로그에서 관측한 **전체 출력 처리량**(tok/s)이다.
    유입이 끊기면 지수감쇠로 내려가 0 에서 멈춘다. 곁눈질만으로 에이전트가
    일하는 중인지 알 수 있다는 점이 이 도구의 존재 이유다.
  · 아래쪽은 **보이는 패널** — 세션 / 한도 / 속도 / 일별 / 팀을 탭으로 바로 고른다.
  · ⌘K/Ctrl+K 검색은 세션과 명령을 빠르게 실행하는 선택형 보조 경로다.
  · 확인이 있으면 속도보다 확인 세션을 먼저 보여준다.
  · 입력/출력/캐시는 이름과 절제된 의미색을 함께 써 색에만 의존하지 않는다.

GUI 를 못 띄우는 환경에서는 run_overlay() 가 예외 대신 False 를 돌려준다.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .history import SPAN_TITLES, SPANS, Series, load_hours, series, summary
from .leaderboard import Entry, Leaderboard
from .leaderboard import tokens_of as _tokens_of
from .meter import ATTENTION_LABELS, ATTENTION_ORDER, attention_counts, session_views
from .rates import RATE_SPAN_TITLES, RATE_SPANS, RateSeries, load_rates, rate_series, rate_summary
from .views import (
    SESSION_FILTER_TITLES, SESSION_FILTERS, check_reason, cost_caption,
    ctx_status_caption, filter_sessions, health_note,
    money_caption, project_label, wait_caption,
)

try:  # PyQt6 가 없어도 import 자체는 성공해야 한다 (CLI 가 이 모듈을 import 한다)
    from PyQt6.QtCore import QEvent, QPoint, QRectF, Qt, QTimer, pyqtSignal
    from PyQt6.QtGui import QAction, QColor, QFont, QFontDatabase, QFontMetricsF, QPainter, QPixmap
    from PyQt6.QtWidgets import (
        QApplication, QLineEdit, QMenu, QMessageBox, QSystemTrayIcon, QToolButton, QWidget,
    )

    QT_ERROR = ""
except Exception as exc:  # pragma: no cover - 환경 의존
    QT_ERROR = f"{type(exc).__name__}: {exc}"
    QWidget = object  # type: ignore[assignment,misc]  # 클래스 정의만 가능하게 둔다
    QSystemTrayIcon = None  # type: ignore[assignment,misc]
    QPixmap = None  # type: ignore[assignment,misc]
    def pyqtSignal(*_args: Any) -> None:  # type: ignore[no-redef]
        return None

# ── 상수 ──────────────────────────────────────────────────────────────────
SPACE_1, SPACE_2, SPACE_3, SPACE_4, SPACE_5, SPACE_6 = 4, 8, 12, 16, 20, 24
RADIUS_SM, RADIUS_MD, RADIUS_LG, RADIUS_XL = 9, 12, 16, 20
MOTION_FAST_MS, MOTION_NORMAL_MS = 120, 180

BASE_W = 340
HEADER_H = 28
SEARCH_H = 38
METER_H = 104               # 상단 조작부를 포함한 TokenMeter 계기판
ROW_H = 27
FOOT_H = 22
PAD = 11
HEAD_H = 24                 # 직접 전환하는 패널 탭
FILTER_H = 24
# tok/s = 로그에서 관측한 **출력 토큰 델타의 처리량**. 실제 스트리밍 생성 시간은 로그에
# 없으므로 모델 벤치마크가 아니다. 캐시 읽기는 생성된 출력이 아니어서 분자에서 제외한다.
RATE_TAU = 20.0             # tok/s 지수평균 시정수 (초)
# 게이지 만땅 기준 (출력 tok/s). 세션 하나가 대략 40 tok/s 이므로, 여기까지 차오르는
# 것은 정말 많이 돌 때뿐이다. 체감이 다르면 settings.overlay.full_scale 로 조정.
DEFAULT_FULL_SCALE = 3000.0
SEGMENTS = 28
PREFS_NAME = "overlay.json"

# TokenMeter의 주 탐색은 보이는 패널 탭이다. 검색은 키보드 보조 경로로만 둔다.
PANELS = ("sessions", "quota", "rates", "days", "board")  # 첫 항목이 기본 패널
PANEL_TITLES = {"board": "팀", "days": "일별", "sessions": "세션", "rates": "속도", "quota": "한도"}
PANEL_ROWS = {"board": 5, "days": 7, "sessions": 10, "rates": 6, "quota": 8}
CHIP_H = 18                 # 한도 칩 한 줄 (자격 있을 때만 높이에 더한다)
MARK_W = {"board": 16.0, "days": 36.0}  # 등수(1자) vs 날짜(5자)
Row = Tuple[str, str, int, float, bool]  # (좌측 표식, 이름, 토큰, 비용, 강조)
# 세션 줄 — (키, 상태, 프로젝트, 모델, effort, tok/s, 벤더, 라이브, 시각,
#              누적토큰, ctx점유율, 창크기앎, 서브몫)
SessionRow = Tuple[str, str, str, str, str, float, str, bool, str, int, float, bool, float]
WHEEL_LINE = 60.0           # 세션 목록 한 줄을 넘기는 휠 델타 (한 칸=120 → 두 줄)
# 세션 표 칸 — 상태/프로젝트/속도/누적/컨텍스트는 항상 같은 위치에 둔다.
SESSION_COLS = ((0.00, 0.16, False, 7.5), (0.16, 0.52, False, 8.5),
                (0.52, 0.68, True, 8.0), (0.68, 0.84, True, 7.5),
                (0.84, 1.00, True, 7.5))
SESSION_COLS_WIDE = ((0.00, 0.09, False, 7.0), (0.09, 0.31, False, 8.5),
                     (0.31, 0.47, False, 7.5), (0.47, 0.58, True, 8.0),
                     (0.58, 0.69, True, 7.5), (0.69, 0.82, True, 7.5),
                     (0.82, 0.93, False, 7.0), (0.93, 1.00, False, 7.0))
CELL_PAD = 4.0
SESSION_HEAD = ("상태", "프로젝트", "메인/s", "누적", "컨텍스트")
SESSION_HEAD_WIDE = ("상태", "프로젝트", "모델", "메인/s", "누적", "컨텍스트", "시각", "추론")
DETAIL_H = 44.0
CARD_H = 32.0
COLHEAD_H = 14              # 칸 이름 한 줄 높이
CTX_WARN, CTX_HOT = 0.70, 0.90  # 컨텍스트 점유 경고선 (앰버 / 빨강)

# 미니 모드 — 게이지 한 줄만. 회의·녹화 중 화면을 비우려고 쓴다
MINI_W, MINI_H = 0.62, 30.0
# 화면에 보이는 직접 조작 — S(미터) · M(패널) · L(상세)
MODES = ("S", "M", "L")
MODE_BTN = 24.0
MIN_HIT = 28.0
HINT = "오늘/누적 · S/M/L · ⌘K 검색 · ⋯ 메뉴"
HINT_SEC = 20.0

# 검색/명령 런처
PALETTE_W = 420
PALETTE_ROW_H = 46
PALETTE_ROWS = 7

# ── 확장(히스토리) 모드 ──
EXPAND_W = 1.5
EXPAND_ROWS = 2             # 목록 줄 수 배수
GRAPH_H = 124               # 그래프 영역 높이 (scale 1.0 기준)
GRAPH_TOP = 8               # 구분선과 '시간별 사용량' 사이 여백
GRAPH_GAP = 1.6             # 막대 사이 간격
BTN_W, BTN_H = 38.0, 24.0   # 범위 버튼 [오늘][7일][30일]
RATE_BTN_W = 44.0           # [1시간][4시간][1일][7일]
EFFORTS = {"minimal": "min", "low": "low", "medium": "med", "high": "high",
           "xhigh": "xhi", "max": "max"}

THEMES: Dict[str, Dict[str, Any]] = {
    "dark": {
        "background_primary": "#0A0C11",
        "background_secondary": "#11141B",
        "surface_glass": "#171A22",
        "surface_glass_elevated": "#20242E",
        "surface_hover": "#FFFFFF",
        "text_primary": "#F5F7FB",
        "text_secondary": "#B3BAC8",
        "text_tertiary": "#8992A5",
        "tint": "#8CCBFF",
        "separator": "#FFFFFF",
        "destructive": "#FF7B88",
        "success": "#69D58E",
        "warning": "#F0C56D",
        "shadow": "#000000",
        "surface_alpha": 238,
        "edge_alpha": 24,
        "hover_alpha": 13,
    },
    "light": {
        "background_primary": "#E7ECF2",
        "background_secondary": "#DDE4EC",
        "surface_glass": "#F8FAFD",
        "surface_glass_elevated": "#FFFFFF",
        "surface_hover": "#172033",
        "text_primary": "#18202B",
        "text_secondary": "#4F5B6D",
        "text_tertiary": "#5F6B7B",
        "tint": "#006EBE",
        "separator": "#172033",
        "destructive": "#B72D3D",
        "success": "#247B46",
        "warning": "#8A5B00",
        "shadow": "#24344A",
        "surface_alpha": 240,
        "edge_alpha": 24,
        "hover_alpha": 12,
    },
}

# ── 작은 헬퍼 ─────────────────────────────────────────────────────────────
def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def _int(v: Any, default: int = 0) -> int:
    """state.json 은 우리가 쓰는 파일이 아니다 — 숫자 자리에 뭐가 있어도 죽지 않는다."""
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _stamp(ts: Any) -> str:
    """세션이 열린 시각 — 'MM-DD HH:MM'. 오늘이라고 날짜를 빼지 않는다(표기가 흔들린다)."""
    v = _float(ts)
    if v <= 0:
        return ""
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(v))
    except (ValueError, OSError, OverflowError):
        return ""


def _day_noon(day: str) -> float:
    """'2026-08-11' → 그 날 정오의 timestamp. 그 날의 24시간을 뽑는 기준점이다."""
    try:
        t = time.strptime(day, "%Y-%m-%d")
        return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 12, 0, 0, 0, 0, -1))
    except (ValueError, OverflowError):
        return time.time()


def _fmt(n: Any) -> str:
    """12345 → '12.3k', 1234567 → '1.2M'."""
    n = _float(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return f"{int(n)}"


def _money(v: Any) -> str:
    v = _float(v)
    return f"${v:.4f}" if v < 10 else f"${v:,.2f}"


def _money_short(v: Any) -> str:
    """랭킹 열용 — 자릿수를 일정하게 유지한다. 세로로 쌓이면 정렬이 곧 가독성이다."""
    v = _float(v)
    if v >= 100:
        return f"${v:,.0f}"
    if v >= 0.1:
        return f"${v:.2f}"
    return f"${v:.3f}"


def gauge_target(rate: float, full_scale: float = DEFAULT_FULL_SCALE) -> float:
    """tok/s → 게이지 채움 0~1. 저유입에서도 눈에 보이도록 sqrt 로 누른다."""
    return math.sqrt(_clamp(rate / max(1.0, full_scale), 0.0, 1.0))


def short_model(name: Any) -> str:
    """'claude-opus-5' → 'opus-5'. 세션 줄에서 모델 칸은 11자쯤밖에 못 준다."""
    s = str(name or "").strip()
    if "/" in s:  # openrouter 스타일 provider/model
        s = s.split("/", 1)[1]
    s = re.sub(r"^claude-", "", s)
    return re.sub(r"-20\d{6}$", "", s) or "?"  # 날짜 접미사


def short_effort(value: Any) -> str:
    s = str(value or "").strip().lower()
    return EFFORTS.get(s, s[:4])


def _rate(v: float) -> str:
    return f"{_fmt(v)}/s" if v >= 0.5 else ""  # 0 을 늘어놓으면 눈이 갈 곳을 잃는다


def ctx_ratio(rec: Any) -> float:
    """세션 기록 → 컨텍스트 점유율 0~1. 창 크기를 모르면 0 (= 안 그린다)."""
    if not isinstance(rec, dict):
        return 0.0
    window = _float(rec.get("ctx_win"))
    return _clamp(_float(rec.get("ctx")) / window, 0.0, 1.0) if window > 0 else 0.0


def sub_ratio(rec: Any) -> float:
    """세션 기록 → 하위 에이전트가 태운 비용 몫 0~1.

    서브에이전트는 부모와 같은 세션에 합산되므로 총비용만 보면 어디서 나갔는지 모른다.
    실측에서 세션에 따라 이 몫이 절반을 넘는다 — 그래서 따로 보여준다.
    """
    if not isinstance(rec, dict):
        return 0.0
    total = _float((rec.get("totals") or {}).get("cost_usd")) if isinstance(rec.get("totals"), dict) else 0.0
    return _clamp(_float(rec.get("sub_cost")) / total, 0.0, 1.0) if total > 0 else 0.0


def ctx_color(ratio: float) -> str:
    """컨텍스트는 차오를수록 위험 신호다 — 압축이 임박했다는 뜻이라 색이 바뀐다."""
    if ratio >= CTX_HOT:
        return str(THEMES["dark"]["destructive"])
    return str(THEMES["dark"]["warning"] if ratio >= CTX_WARN else THEMES["dark"]["text_tertiary"])


def visible_pos(pos: Any, screens: List[Tuple[int, int, int, int]], fallback: Tuple[int, int]) -> Tuple[int, int]:
    """저장된 좌표가 연결된 화면 밖이면 fallback 으로 되돌린다.

    외장 모니터에 놓아둔 채 케이블을 뽑으면 좌표가 그대로 남아 창이 영영
    안 보인다 (Qt 는 경고만 찍고 옮겨주지 않는다). screens 는 (x, y, w, h).
    """
    try:
        x, y = int(pos[0]), int(pos[1])
    except (TypeError, ValueError, IndexError):
        return fallback
    if any(sx <= x < sx + sw and sy <= y < sy + sh for sx, sy, sw, sh in screens):
        return x, y
    return fallback


def visible_rect_pos(
    pos: Any,
    size: Tuple[int, int],
    screens: List[Tuple[int, int, int, int]],
    fallback: Tuple[int, int],
) -> Tuple[int, int]:
    """창 전체가 한 화면의 사용 가능 영역에 들어오도록 좌표를 보정한다."""
    x, y = visible_pos(pos, screens, fallback)
    screen = next(
        ((sx, sy, sw, sh) for sx, sy, sw, sh in screens
         if sx <= x < sx + sw and sy <= y < sy + sh),
        screens[0] if screens else (fallback[0], fallback[1], size[0], size[1]),
    )
    sx, sy, sw, sh = screen
    width, height = max(0, int(size[0])), max(0, int(size[1]))
    return (
        max(sx, min(x, sx + max(0, sw - width))),
        max(sy, min(y, sy + max(0, sh - height))),
    )


def session_sort_key(row: Dict[str, Any]) -> Tuple[int, int, float, str]:
    """실시간 속도와 무관한 안정 순서. 상태가 같으면 최신 세션을 먼저 둔다."""
    attention = str(row.get("attention") or "done")
    return (
        ATTENTION_ORDER.get(attention, len(ATTENTION_ORDER)),
        0 if row.get("live") else 1,
        -_float(row.get("started_at")),
        str(row.get("key") or ""),
    )


def _age_caption(ts: Any, now: Optional[float] = None) -> str:
    value = _float(ts)
    if value <= 0:
        return "갱신 시각 미상"
    sec = max(0, int((time.time() if now is None else now) - value))
    if sec < 60:
        return "방금 갱신"
    if sec < 3600:
        return f"{sec // 60}분 전 갱신"
    if sec < 86400:
        return f"{sec // 3600}시간 전 갱신"
    return f"{sec // 86400}일 전 갱신"


def search_score(query: str, *parts: Any) -> int:
    """짧은 런처 검색 점수. 포함 검색 뒤에 공백 없는 부분수열만 허용한다."""
    # ponytail: 로컬 세션 수 규모의 선형 검색. 수천 항목이 실측될 때만 인덱스를 둔다.
    needle = " ".join(query.casefold().split())
    if not needle:
        return 1
    haystack = " ".join(str(part or "").casefold() for part in parts)
    if haystack.startswith(needle):
        return 300 - min(100, len(haystack) - len(needle))
    if needle in haystack:
        return 220 - min(100, haystack.index(needle))
    compact_needle, compact_haystack = needle.replace(" ", ""), haystack.replace(" ", "")
    cursor, gap = -1, 0
    for char in compact_needle:
        found = compact_haystack.find(char, cursor + 1)
        if found < 0:
            return 0
        gap += found - cursor - 1
        cursor = found
    return max(1, 120 - gap)


def _qss_rgba(value: str, alpha: int) -> str:
    text = str(value or "").lstrip("#")
    if len(text) != 6:
        return f"rgba(0,0,0,{alpha})"
    return f"rgba({int(text[0:2], 16)},{int(text[2:4], 16)},{int(text[4:6], 16)},{alpha})"


def _prefs_path() -> Path:
    from .config import DATA_DIR

    return DATA_DIR / PREFS_NAME


def _load_prefs() -> Dict[str, Any]:
    try:
        return json.loads(_prefs_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_prefs(data: Dict[str, Any]) -> None:
    try:
        p = _prefs_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


if not QT_ERROR:

    def _c(value: Any, alpha: int = 255) -> "QColor":
        c = QColor(value)
        c.setAlpha(alpha)
        return c


class MeterWindow(QWidget):
    """미터 + 랭킹 창. 애니메이션 상태는 여기 있고 paintEvent 는 그리기만 한다."""

    sync_finished = pyqtSignal(str)

    def _resolved_theme(self) -> str:
        if self.theme_mode in THEMES:
            return self.theme_mode
        try:
            return "light" if QApplication.styleHints().colorScheme() == Qt.ColorScheme.Light else "dark"
        except Exception:
            return "dark"

    def _state_color(self, state: str) -> str:
        return {
            "check": self.colors["destructive"],
            "working": self.colors["success"],
            "waiting": self.colors["text_secondary"],
            "done": self.colors["text_tertiary"],
        }.get(state, self.colors["text_tertiary"])

    def _kind_color(self, kind: str) -> str:
        return {
            "input": self.colors["success"],
            "output": self.colors["tint"],
            "cache_read": self.colors["text_tertiary"],
            "cache_write": self.colors["warning"],
        }.get(kind, self.colors["text_secondary"])

    def _ctx_color(self, ratio: float) -> str:
        if ratio >= CTX_HOT:
            return self.colors["destructive"]
        return self.colors["warning"] if ratio >= CTX_WARN else self.colors["text_tertiary"]

    def _seg_color(self, position: float) -> str:
        if position < 0.55:
            return self.colors["success"]
        if position < 0.8:
            return self.colors["warning"]
        return self.colors["destructive"]

    def _control_style(self) -> str:
        hover = _qss_rgba(self.colors["surface_hover"], self.colors["hover_alpha"])
        pressed = _qss_rgba(self.colors["surface_hover"], self.colors["hover_alpha"] * 2)
        focus = _qss_rgba(self.colors["tint"], 18)
        return (
            "QToolButton{background:transparent;border:0;border-radius:8px;}"
            f"QToolButton:hover{{background:{hover};}}"
            f"QToolButton:pressed{{background:{pressed};}}"
            f"QToolButton:focus{{border:2px solid {self.colors['tint']};background:{focus};}}"
        )

    def _input_style(self) -> str:
        selection = _qss_rgba(self.colors["tint"], 70)
        return (
            "QLineEdit{background:transparent;border:0;padding:0;"
            f"color:{self.colors['text_primary']};"
            f"placeholder-text-color:{self.colors['text_tertiary']};"
            f"selection-background-color:{selection};}}"
        )

    def _menu_style(self) -> str:
        selected = _qss_rgba(self.colors["tint"], 32)
        return (
            f"QMenu{{background:{self.colors['surface_glass_elevated']};"
            f"color:{self.colors['text_primary']};border:1px solid "
            f"{_qss_rgba(self.colors['separator'], self.colors['edge_alpha'])};"
            "border-radius:10px;padding:6px;}"
            "QMenu::item{padding:7px 20px;border-radius:7px;}"
            f"QMenu::item:selected{{background:{selected};}}"
            f"QMenu::item:disabled{{color:{self.colors['text_tertiary']};}}"
        )

    def _apply_theme(self) -> None:
        self.colors = dict(THEMES[self._resolved_theme()])
        style = self._control_style()
        for button in self._controls.values():
            button.setStyleSheet(style)
        if hasattr(self, "_search_field"):
            self._search_field.setStyleSheet(self._input_style())
        self.update()

    def __init__(self, meter: Any = None, board: Any = None) -> None:
        super().__init__()
        self.meter = meter

        # ── 설정 / 사용자 환경설정 ──
        fps, cfg_scale, cfg_pos = 30, 1.0, [40, 80]
        self.full_scale = DEFAULT_FULL_SCALE  # 설정을 못 읽어도 미터는 떠야 한다
        config = None
        try:
            from .config import load_config

            config = load_config()
            fps = int(config.setting("overlay.fps", 60) or 60)
            cfg_scale = float(config.setting("overlay.scale", 1.0) or 1.0)
            self.full_scale = float(config.setting("overlay.full_scale", DEFAULT_FULL_SCALE)
                                    or DEFAULT_FULL_SCALE)
            pos = config.setting("overlay.position", cfg_pos)
            if isinstance(pos, (list, tuple)) and len(pos) == 2:
                cfg_pos = [int(pos[0]), int(pos[1])]
        except Exception:
            pass
        self.board = board if board is not None else Leaderboard(config)

        # ── 데이터 상태 (창 크기가 패널 줄 수를 따라가므로 창보다 먼저 잡는다) ──
        self.status: Dict[str, Any] = {}
        self.entries: List[Entry] = []
        self.rows: List[Any] = []  # Row (랭킹·일별) 또는 SessionRow
        self.note = ""
        self.state_error = ""
        self._last_output: Optional[int] = None
        self.rates: Dict[str, float] = {}       # 세션 키 → tok/s (미터와 같은 지수평균)
        self._seen_out: Dict[str, int] = {}     # 세션 키 → 마지막으로 본 누적 출력

        prefs = _load_prefs()
        self.theme_mode = str(prefs.get("theme") or "dark")
        if self.theme_mode not in ("dark", "light", "system"):
            self.theme_mode = "dark"
        self.reduce_transparency = bool(prefs.get("reduce_transparency", False))
        self.reduce_motion = bool(prefs.get("reduce_motion", False))
        self.colors = dict(THEMES[self._resolved_theme()])
        self.scale = _clamp(_float(prefs.get("scale"), cfg_scale), 1.0, 2.0)
        self.rows_on = bool(prefs.get("rows", True))
        self.on_top = bool(prefs.get("on_top", True))
        self.scope = prefs.get("scope") if prefs.get("scope") in ("today", "total") else "today"
        self.panel = prefs.get("panel") if prefs.get("panel") in PANELS else PANELS[0]
        if self.panel == "board" and not getattr(self.board, "online", False):
            self.panel = "sessions"
        self.expanded = bool(prefs.get("expanded", False))
        self.span = prefs.get("span") if prefs.get("span") in SPANS else SPANS[0]
        self.rate_span = prefs.get("rate_span") if prefs.get("rate_span") in RATE_SPANS else "1h"
        self.session_filter = (
            prefs.get("filter") if prefs.get("filter") in SESSION_FILTERS else "live"
        )
        self.end_card = bool(prefs.get("end_card", True))
        self.mini = bool(prefs.get("mini", False))
        # 힌트는 딱 한 번 — 본 적이 없을 때만 잠깐 띄우고 그 사실을 prefs 에 남긴다
        self.hint_seen = bool(prefs.get("hint_seen", False))
        self.hint_until = 0.0 if self.hint_seen else time.monotonic() + HINT_SEC
        # 드릴다운 — ("project", 이름) 또는 ("day", "2026-08-11"). ESC 로 푼다.
        self.focus: Optional[Tuple[str, str]] = None
        self.open_key = ""     # 세션 상세가 열린 키
        self.card: Optional[Tuple[float, str]] = None  # (사라질 시각, 한 줄)
        self._live_keys: set[str] = set()
        self.scroll = 0        # 세션 목록에서 건너뛴 줄 수 (휠로 움직인다)
        self._wheel = 0.0      # 휠 델타 누적 — 트랙패드는 같은 양을 잘게 쪼개 보낸다
        self._hit: Dict[str, Any] = {}  # 그린 클릭 영역 (그리는 쪽이 좌표의 주인이다)
        self._controls: Dict[str, Any] = {}
        self._control_signature: Tuple[Any, ...] = ()
        self._tray = None
        self._tray_menu = None
        self.quota: Dict[str, Any] = {"windows": [], "errors": {}}
        self._quota_busy = False
        self._sync_busy = False
        self._feedback = ""
        self._screen_connected = False
        self._quitting = False
        self.palette_open = False
        self.palette_index = 0
        self.palette_results: List[Dict[str, Any]] = []
        pos = prefs.get("pos") if isinstance(prefs.get("pos"), list) else cfg_pos

        # ── 창 ──
        self.setWindowTitle("TokenMeter")
        self.setAccessibleName("TokenMeter 사용량 오버레이")
        self._apply_flags()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._font = (QFont("SF Mono") if sys.platform == "darwin" else
                      QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self._search_font = (QFont("SF Pro Text") if sys.platform == "darwin" else
                             QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont))
        self._search_field = QLineEdit(self)
        self._search_field.setFont(self._search_font)
        self._search_field.setPlaceholderText("세션 또는 명령 검색")
        self._search_field.setAccessibleName("세션 또는 명령 검색")
        self._search_field.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._search_field.setTextMargins(24, 0, 42, 0)
        self._search_field.setStyleSheet(self._input_style())
        self._search_field.textChanged.connect(self._refresh_palette)
        self._search_field.installEventFilter(self)
        self._search_field.hide()
        size = self._size()
        self.resize(*size)
        home = QApplication.primaryScreen().availableGeometry()
        rects = [
            (g.x(), g.y(), g.width(), g.height())
            for g in (s.availableGeometry() for s in QApplication.screens())
        ]
        self.move(*visible_rect_pos(pos, size, rects, (home.x() + 40, home.y() + 80)))

        # ── 애니메이션 / 계측 상태 ──
        self._t0 = time.monotonic()
        self._last_frame = self._t0
        self.t = 0.0
        self.rate = 0.0           # 전체 출력 델타 처리량의 지수평균 (tok/s)
        self.gauge = 0.0          # 게이지 표시값 (target 을 관성으로 따라간다)
        self.peak = 0.0           # 피크 홀드 마커
        self.pulse = 0.0          # 유입 순간 번쩍임 0~1

        self._drag: Optional["QPoint"] = None

        self._refresh_state()
        self._setup_tray()
        self.sync_finished.connect(self._sync_done)
        try:
            QApplication.styleHints().colorSchemeChanged.connect(
                lambda _scheme: self._apply_theme() if self.theme_mode == "system" else None
            )
        except Exception:
            pass

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._active_interval = max(16, int(1000 / max(1, fps)))
        self._timer.start(self._active_interval)
        self._poll = QTimer(self)
        self._poll.timeout.connect(self._refresh_state)
        self._poll.start(200)

    # ── 창 관리 ──────────────────────────────────────────────────────────
    def _row_count(self) -> int:
        """보여줄 줄 수. 데이터가 적을 때 빈 칸을 띄우지 않는다."""
        if not self.rows_on:
            return 0
        return min(self._cap(self.panel), max(1, len(self.rows)))

    def _visible_panels(self) -> Tuple[str, ...]:
        """팀 기능은 endpoint가 설정된 경우에만 기본 탐색에 노출한다."""
        return PANELS if getattr(self.board, "online", False) else tuple(
            panel for panel in PANELS if panel != "board"
        )

    def _shows_graph(self) -> bool:
        return self.expanded and self.panel in ("rates", "days")

    def _cap(self, panel: str) -> int:
        """확장 화면은 목록을 늘리되 세션 표는 위치 안정성을 위해 10줄로 고정한다."""
        n = PANEL_ROWS.get(panel, 5)
        return n if (not self.expanded or panel == "sessions") else n * EXPAND_ROWS

    def _size(self) -> Tuple[int, int]:
        if self.palette_open:
            rows = max(3, min(PALETTE_ROWS, len(self.palette_results)))
            w = max(BASE_W, PALETTE_W)
            h = PAD * 2 + HEADER_H + SPACE_2 + SEARCH_H + SPACE_2 + rows * PALETTE_ROW_H + FOOT_H
            return int(w * self.scale), int(h * self.scale)
        if self.mini:
            return int(BASE_W * MINI_W * self.scale), int(MINI_H * self.scale)
        rows = self._row_count()
        if self.panel == "sessions":
            extra = FILTER_H + COLHEAD_H
        elif self.panel == "rates":
            extra = COLHEAD_H + (0 if self.expanded else BTN_H)
        elif self.panel == "quota":
            extra = COLHEAD_H
        else:
            extra = 0
        head = HEAD_H + extra
        h = (PAD * 2 + METER_H + self._chip_h()
             + (head + rows * ROW_H + 8 if rows else 0) + FOOT_H)
        w = BASE_W * (EXPAND_W if self.expanded else 1.0)
        if self._shows_graph():
            h += GRAPH_H
        if self.card:
            h += CARD_H
        if self.open_key and self.panel == "sessions":
            h += DETAIL_H
        return int(w * self.scale), int(h * self.scale)

    def _screen_rects(self) -> List[Tuple[int, int, int, int]]:
        return [
            (g.x(), g.y(), g.width(), g.height())
            for g in (screen.availableGeometry() for screen in QApplication.screens())
        ]

    def _resize_to_content(self) -> None:
        """내용 크기와 화면 경계를 한 곳에서 맞춘다."""
        desired = self._size()
        screen = QApplication.screenAt(self.frameGeometry().center()) or QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            factor = min(1.0, area.width() / max(1, desired[0]), area.height() / max(1, desired[1]))
            if factor < 1.0 and self.scale > 1.0:
                self.scale = max(1.0, self.scale * factor)
                desired = self._size()
        if (self.width(), self.height()) != desired:
            self.resize(*desired)
        rects = self._screen_rects()
        if rects:
            fallback = (rects[0][0], rects[0][1])
            fitted = visible_rect_pos((self.x(), self.y()), desired, rects, fallback)
            if fitted != (self.x(), self.y()):
                self.move(*fitted)

    def _apply_flags(self) -> None:
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if self.on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        # macOS 는 Tool 창(NSPanel)을 앱이 비활성화되면 자동으로 숨긴다.
        # 사용자는 늘 터미널/에디터에 있으므로 이게 없으면 미터가 거의 안 보인다.
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow, True)

    def _store_prefs(self) -> None:
        _save_prefs(
            {
                "pos": [self.x(), self.y()],
                "scale": round(self.scale, 3),
                "rows": self.rows_on,
                "on_top": self.on_top,
                "scope": self.scope,
                "panel": self.panel,
                "expanded": self.expanded,
                "span": self.span,
                "rate_span": self.rate_span,
                "filter": self.session_filter,
                "end_card": self.end_card,
                "mini": self.mini,
                "hint_seen": self.hint_seen,
                "theme": self.theme_mode,
                "reduce_transparency": self.reduce_transparency,
                "reduce_motion": self.reduce_motion,
            }
        )

    # ── 상태 갱신 (200ms) ────────────────────────────────────────────────
    def _refresh_state(self) -> None:
        st: Dict[str, Any] = {}
        try:
            if self.meter is not None:
                self.meter.reload()
                st = self.meter.status() or {}
            self.state_error = ""
        except Exception:
            st = self.status  # 일시적인 읽기 실패가 세션 종료/빈 화면으로 보이면 안 된다
            self.state_error = "상태 읽기 실패 · tokenmeter doctor"
        self.status = st if isinstance(st, dict) else {}
        self._maybe_quota()
        self._track_ended()
        self._track_rates()
        self._rebuild_rows()
        if self.palette_open:
            self._refresh_palette()
        self._update_accessibility()
        self._update_tray()

        # 유입 감지 = 누적 출력 토큰의 증가분. 이 델타가 곧 tok/s 의 분자다.
        cur = _int(self._totals().get("output_tokens"))
        if self._last_output is None:  # 첫 로드는 기준점만 (과거 누적으로 미터가 튀면 안 된다)
            self._last_output = cur
            return
        gained = max(0, cur - self._last_output)
        self._last_output = cur
        if gained > 0:
            # 임펄스 주입 + 연속 감쇠 = 시정수 TAU 의 이동평균. 장기적으로 정확히
            # '초당 관측한 출력 델타' 로 수렴하고, 유입이 끊기면 절벽 없이 0 으로 간다.
            self.rate += gained / RATE_TAU
            self.pulse = 1.0

    def _track_rates(self) -> None:
        """세션별 메인 출력 처리량. 미터와 같은 임펄스 + 지수감쇠로 잰다.

        어느 에이전트가 지금 실제로 돌고 있는지는 '마지막 활동 시각' 으로는 안 보인다
        (막 끝난 세션도 방금 활동했다). 세션마다 속도가 있어야 구분된다.
        """
        book = self._sessions()
        for key, rec in book.items():
            if not isinstance(rec, dict):
                continue
            totals = rec.get("totals")
            total_out = _int((totals or {}).get("output_tokens")) if isinstance(totals, dict) else 0
            cur = max(0, total_out - _int(rec.get("sub_output_tokens")))
            prev = self._seen_out.get(key)
            self._seen_out[key] = cur
            if prev is not None and cur > prev:  # 첫 관측은 기준점만 (과거 누적으로 튀면 안 된다)
                self.rates[key] = self.rates.get(key, 0.0) + (cur - prev) / RATE_TAU
        for gone in [k for k in self._seen_out if k not in book]:  # 잘려나간 세션 기록
            del self._seen_out[gone]
            self.rates.pop(gone, None)

    def _totals(self) -> Dict[str, Any]:
        """미터가 읽는 합계 — 유입 감지는 항상 누적(total)으로 한다.

        오늘/누적 토글은 표시 숫자만 바꾼다. 자정에 today 가 0 으로 굴러갈 때
        누적을 보고 있어야 '유입이 끊겼다'고 오판하지 않는다.
        """
        node = self.status.get("total")
        totals = node.get("totals") if isinstance(node, dict) else None
        return totals if isinstance(totals, dict) else {}

    def _scoped(self) -> Dict[str, Any]:
        node = self.status.get(self.scope)
        totals = node.get("totals") if isinstance(node, dict) else None
        return totals if isinstance(totals, dict) else {}

    def _sessions(self) -> Dict[str, Any]:
        book = self.status.get("sessions")
        return book if isinstance(book, dict) else {}

    def _approx(self) -> bool:
        """구독분이 섞여 있나. 그렇다면 이 금액은 청구서가 아니라 **API 환산가**다."""
        node = (self.status.get("plans") or {}).get("subscription")
        return bool(node) and _tokens_of(node) > 0

    def _quota_marks(self) -> List[Tuple[str, str]]:
        from .quota import chips

        return chips(list((self.quota or {}).get("windows") or []))

    def _chip_h(self) -> float:
        return CHIP_H if self._quota_marks() else 0.0

    def _maybe_quota(self) -> None:
        from .quota import due, load, refresh

        try:
            snap = load()
        except Exception:
            snap = {"windows": [], "errors": {}, "updated_at": 0.0}
        self.quota = snap
        if self.meter is None or self._quota_busy or not due(snap):
            return
        self._quota_busy = True

        def work() -> None:
            try:
                refresh()
            except Exception:
                pass
            finally:
                self._quota_busy = False

        threading.Thread(target=work, name="tokenmeter-quota", daemon=True).start()

    def _track_ended(self) -> None:
        """라이브가 사라진 세션이 있으면 짧은 종료 카드를 남긴다."""
        views = session_views(self.status)
        current = {str(row["key"]) for row in views if row.get("live")}
        gone = self._live_keys - current
        self._live_keys = current
        if not gone or not self.end_card:
            return
        key = sorted(gone)[-1]
        rec = next((row for row in views if row["key"] == key), None)
        if rec is None:
            return
        name = project_label(rec.get("project"), rec.get("cwd"))
        cost = cost_caption(self._approx(), rec.get("cost_usd"))
        self.card = (time.monotonic() + 5.0, f"{name} 종료 · {cost}")

    def _counts(self) -> Dict[str, int]:
        try:
            return attention_counts(self.status)
        except Exception:
            return {"check": 0, "working": 0, "waiting": 0, "risk": 0}

    def _check_project(self) -> str:
        projects = {
            project_label(rec.get("project"), rec.get("cwd"))
            for rec in session_views(self.status) if rec.get("attention") == "check"
        }
        return next(iter(projects)) if len(projects) == 1 else ""

    # ── 아래 패널 (관심현황 / 일별 히스토리 / 최근 세션 / 속도) ──────────
    def _rebuild_rows(self) -> None:
        try:
            self.rows, self.note = self._build_rows()
        except Exception:
            self.rows, self.note = [], ""
        self._resize_to_content()

    def _build_rows(self) -> Tuple[List[Any], str]:
        if self.panel == "days":
            return self._day_rows()
        if self.panel == "sessions":
            return self._session_rows()
        if self.panel == "rates":
            return self._rate_rows()
        if self.panel == "quota":
            return self._quota_rows()
        entries, note = self.board.team(self.status)
        rows = [(e.handle, e.check, e.working, e.waiting, e.cost_usd, e.me)
                for e in entries]
        counts = self._counts()
        head = f"확인 {counts['check']} · 작업 {counts['working']} · 대기 {counts['waiting']}"
        return rows, f"{head} · {note}"

    def _day_rows(self) -> Tuple[List[Row], str]:
        """날짜별 토큰·비용, 최근 날짜부터. 진행 중인 오늘도 같은 표에 놓는다."""
        days = self.status.get("days")
        merged: Dict[str, Any] = dict(days) if isinstance(days, dict) else {}
        node = self.status.get("today")
        today = str(node.get("date") or "") if isinstance(node, dict) else ""
        if today:
            merged[today] = node.get("totals") or {}
        rows: List[Row] = []
        for day in sorted(merged, reverse=True)[:self._cap("days")]:
            totals = merged[day] if isinstance(merged[day], dict) else {}
            rows.append((day, "진행 중" if day == today else "",
                         _tokens_of(totals), _float(totals.get("cost_usd")), day == today))
        if not rows:
            return [], "히스토리 없음 — 하루가 지나면 쌓입니다"
        spent = sum(r[3] for r in rows)
        return rows, f"최근 {len(rows)}일 합계 {_money(spent)}"

    def _session_rows(self) -> Tuple[List[SessionRow], str]:
        """세션 목록 — 상태를 먼저, 그 안에서는 위치가 흔들리지 않게 정렬한다."""
        views = filter_sessions(session_views(self.status), self.session_filter)
        views.sort(key=session_sort_key)
        cap = self._cap("sessions")
        self.scroll = max(0, min(self.scroll, len(views) - cap))  # 목록이 줄면 따라 올라온다
        rows: List[SessionRow] = []
        for rec in views[self.scroll:self.scroll + cap]:
            key = str(rec["key"])
            rows.append((
                key,
                str(rec["attention"]),
                project_label(rec.get("project") or rec.get("service") or "(unknown)",
                              rec.get("cwd")),
                short_model(rec.get("model")),
                short_effort(rec.get("effort")),
                self.rates.get(key, 0.0),
                str(rec.get("vendor") or ""),
                bool(rec.get("live")),
                _stamp(rec.get("started_at")),
                _tokens_of(rec),
                ctx_ratio(rec),
                _float(rec.get("ctx_win")) > 0,
                sub_ratio(rec),
            ))
        if not rows:
            empty = {
                "check": "확인이 필요한 세션 없음",
                "live": "라이브 세션 없음",
            }.get(self.session_filter, "기록된 세션 없음")
            return [], empty
        more = f" · {self.scroll + len(rows)}/{len(views)}" if len(views) > cap else ""
        return rows, (f"세션 {len(views)}개 · 라이브 "
                      f"{self.status.get('live_count', 0)}개{more}")

    def _quota_rows(self) -> Tuple[List[Any], str]:
        from .quota import panel_rows

        snap = self.quota or {}
        rows = panel_rows(list(snap.get("windows") or []))
        if not rows:
            errors = snap.get("errors") or {}
            if errors:
                return [], "한도를 못 읽음 · " + ", ".join(str(v) for v in errors.values())
            return [], "자격 없음 · Claude/Codex/Grok 로그인"
        warn = sum(1 for row in snap.get("windows") or [] if row.get("status") in ("warn", "exhausted"))
        age = _age_caption(snap.get("updated_at"))
        errors = snap.get("errors") or {}
        note = f"한도 {len(rows)}개 · {age}"
        if warn:
            note += f" · 경고 {warn}"
        if errors:
            note += " · 일부 갱신 실패"
        return rows, note

    def _rate_view(self) -> RateSeries:
        return rate_series(load_rates(self.status), self.rate_span)

    def _rate_rows(self) -> Tuple[List[Any], str]:
        data = self._rate_view()
        rows = [
            (row.vendor, short_model(row.model), row.tokens, row.rate, row.share)
            for row in data.rows[: self._cap("rates")]
        ]
        return rows, rate_summary(data)

    # ── 검색 / 명령 런처 ────────────────────────────────────────────────
    def _palette_catalog(self) -> List[Dict[str, Any]]:
        panel_copy = {
            "sessions": ("세션", "현재 에이전트 상태와 컨텍스트"),
            "rates": ("속도", "모델별 출력 처리량"),
            "days": ("사용량 히스토리", "오늘 · 7일 · 30일"),
            "quota": ("플랜 한도", "Claude · Codex · Grok"),
            "board": ("팀", "공유 상태와 사용량"),
        }
        items: List[Dict[str, Any]] = []
        for index, panel in enumerate(self._visible_panels()):
            title, subtitle = panel_copy[panel]
            items.append({
                "target": f"panel:{panel}", "title": title, "subtitle": subtitle,
                "tag": "현재" if panel == self.panel else "보기", "order": 20 + index,
                "keywords": f"{panel} {PANEL_TITLES[panel]}",
            })
        items.extend([
            {"target": "filter:check", "title": "확인 필요한 세션", "subtitle": "응답을 기다리는 작업만",
             "tag": "필터", "order": 12, "keywords": "attention check 확인"},
            {"target": "filter:live", "title": "라이브 세션", "subtitle": "확인 · 작업 · 대기",
             "tag": "필터", "order": 13, "keywords": "active live 라이브"},
            {"target": "filter:all", "title": "모든 세션", "subtitle": "종료된 기록 포함",
             "tag": "필터", "order": 14, "keywords": "all history 전체"},
            {"target": "mode:S", "title": "미터만 보기", "subtitle": "콘텐츠 목록 접기",
             "tag": "보기", "order": 40, "keywords": "small compact s"},
            {"target": "mode:M", "title": "기본 보기", "subtitle": "미터와 핵심 목록",
             "tag": "보기", "order": 41, "keywords": "medium default m"},
            {"target": "mode:L", "title": "상세 보기", "subtitle": "넓은 히스토리와 더 많은 결과",
             "tag": "보기", "order": 42, "keywords": "large detail l"},
            {"target": "command:scope", "title": "누적 보기" if self.scope == "today" else "오늘 보기",
             "subtitle": "사용량 범위 전환", "tag": "명령", "order": 43,
             "keywords": "today total scope 오늘 누적"},
            {"target": "command:settings", "title": "빠른 설정", "subtitle": "모양 · 동작 · 창 크기",
             "tag": "⌘,", "order": 44, "keywords": "settings preferences 설정"},
        ])
        for rec in sorted(session_views(self.status), key=session_sort_key):
            key = str(rec.get("key") or "")
            attention = str(rec.get("attention") or "done")
            project = project_label(rec.get("project") or rec.get("service") or "(unknown)", rec.get("cwd"))
            model = short_model(rec.get("model"))
            context = ctx_status_caption(ctx_ratio(rec), _float(rec.get("ctx_win")) > 0)
            subtitle = " · ".join(part for part in (
                ATTENTION_LABELS.get(attention, attention), model, f"컨텍스트 {context}",
            ) if part)
            order = {"check": 0, "working": 2, "waiting": 4, "done": 60}.get(attention, 50)
            items.append({
                "target": f"session:{key}", "title": project, "subtitle": subtitle,
                "tag": "세션", "order": order,
                "keywords": f"{key} {rec.get('vendor') or ''} {rec.get('service') or ''}",
            })
        return items

    def _refresh_palette(self, _text: str = "") -> None:
        if not self.palette_open:
            return
        query = self._search_field.text().strip()
        ranked: List[Tuple[int, int, str, Dict[str, Any]]] = []
        for item in self._palette_catalog():
            score = search_score(query, item["title"], item["subtitle"], item["keywords"])
            if score:
                ranked.append((-score, int(item["order"]), str(item["title"]), item))
        ranked.sort(key=lambda row: row[:3])
        self.palette_results = [row[3] for row in ranked[:PALETTE_ROWS]]
        self.palette_index = max(0, min(self.palette_index, len(self.palette_results) - 1))
        self._resize_to_content()
        self.update()

    def _open_palette(self) -> None:
        if self.palette_open:
            self._search_field.selectAll()
            self._search_field.setFocus(Qt.FocusReason.ShortcutFocusReason)
            return
        self.palette_open, self.palette_index = True, 0
        self._search_field.clear()
        self._refresh_palette()
        self._search_field.show()
        self._resize_to_content()
        self.update()
        self._search_field.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def _close_palette(self) -> None:
        if not self.palette_open:
            return
        self.palette_open = False
        self._search_field.hide()
        self._search_field.clear()
        self.palette_results = []
        self._resize_to_content()
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        self.update()

    def _move_palette(self, step: int) -> None:
        if not self.palette_results:
            return
        self.palette_index = (self.palette_index + step) % len(self.palette_results)
        self.update()

    def _activate_palette(self) -> bool:
        if not 0 <= self.palette_index < len(self.palette_results):
            return False
        target = str(self.palette_results[self.palette_index]["target"])
        self._close_palette()
        return self._activate_target(target)

    def _open_session(self, key: str) -> None:
        views = sorted(filter_sessions(session_views(self.status), "all"), key=session_sort_key)
        index = next((i for i, rec in enumerate(views) if str(rec.get("key")) == key), 0)
        self.panel, self.session_filter, self.rows_on = "sessions", "all", True
        self.scroll = max(0, index - self._cap("sessions") // 2)
        self.open_key = key
        rec = next((rec for rec in views if str(rec.get("key")) == key), None)
        project = project_label(rec.get("project"), rec.get("cwd")) if rec else ""
        self.focus = ("project", project) if project else None
        self._rebuild_rows()
        self._resize_to_content()
        self._store_prefs()
        self.update()

    # ── 프레임 ───────────────────────────────────────────────────────────
    def _tick(self) -> None:
        now_m = time.monotonic()
        dt = max(0.0, now_m - self._last_frame)  # 단조 시계 — 절전/NTP 로 되감기지 않는다
        self._last_frame = now_m
        self.t = now_m - self._t0
        if self.hint_until and now_m >= self.hint_until:
            self._end_hint()  # 다 보여줬으면 그 사실을 남긴다 (다음 실행부터 안 뜬다)
        if self.card and now_m >= self.card[0]:
            self.card = None
            self._resize_to_content()
        self._advance(dt)
        self.update()
        active = bool(self.rate > 0.01 or self.gauge > 0.004 or self.pulse > 0.0 or self.card)
        interval = self._active_interval if active and not self.reduce_motion else 250
        if self._timer.interval() != interval:
            self._timer.setInterval(interval)

    def _end_hint(self) -> None:
        if not self.hint_until:
            return
        self.hint_until, self.hint_seen = 0.0, True
        self._store_prefs()

    def _advance(self, dt: float) -> None:
        """미터 물리: 유입이 있으면 차오르고, 끊기면 지수감쇠로 내려가 0 에서 멈춘다."""
        decay = math.exp(-dt / RATE_TAU)
        self.rate *= decay
        # 잔량 컷. 토큰 1개도 1/TAU 를 주입하므로 이보다 훨씬 아래여야 실제 유입을 못 죽인다
        if self.rate < 0.01:
            self.rate = 0.0
        for key, value in list(self.rates.items()):  # 세션 줄의 tok/s 도 같이 식는다
            value *= decay
            if value < 0.01:
                del self.rates[key]
            else:
                self.rates[key] = value

        target = gauge_target(self.rate, self.full_scale)
        if self.reduce_motion:
            self.gauge, self.peak, self.pulse = target, 0.0, 0.0
            return
        # 프레임이 오래 밀려도 애니메이션이 튀지 않게 보간 dt 만 묶는다 (감쇠는 실제 dt)
        step = min(dt, 0.05)
        # 차오르는 건 빠르게, 내려가는 건 느리게 — 관성이 있어야 계기판처럼 읽힌다
        self.gauge += (target - self.gauge) * min(1.0, step * (9.0 if target > self.gauge else 1.6))
        if target <= 0.0 and self.gauge < 0.004:
            self.gauge = 0.0
        self.peak = max(target, self.peak - step * 0.22)
        self.pulse = max(0.0, self.pulse - step * 2.4)

    # ── 그리기 ───────────────────────────────────────────────────────────
    def _f(self, size: float, bold: bool = False) -> None:
        self._font.setBold(bold)
        self._font.setPointSizeF(max(6.0, size * self.scale))
        self._painter.setFont(self._font)

    def _text(self, x: float, y: float, w: float, h: float, s: str,
              color: Optional[str] = None, right: bool = False, alpha: int = 255) -> None:
        self._painter.setPen(_c(color or self.colors["text_primary"], alpha))
        align = Qt.AlignmentFlag.AlignRight if right else Qt.AlignmentFlag.AlignLeft
        self._painter.drawText(QRectF(x, y, w, h), align | Qt.AlignmentFlag.AlignVCenter, s)

    def _control_caption(self, name: str) -> str:
        labels = {
            "search": "세션 또는 명령 검색 · Command K",
            "scope": "오늘과 누적 전환",
            "attention": "확인이 필요한 세션 보기",
            "menu": "더 보기 메뉴",
            "close": "오버레이 숨기기 · 측정 계속",
            "card": "종료 요약 닫기",
            "back": "전체 시간 범위로 돌아가기",
            "act:copy": "세션 영수증 복사",
            "act:project": "이 프로젝트의 일별 그래프 보기",
        }
        if name in labels:
            return labels[name]
        kind, _, value = name.partition(":")
        if kind == "palette":
            try:
                item = self.palette_results[int(value)]
                return f"{item['title']} · {item['subtitle']}"
            except (IndexError, TypeError, ValueError):
                return "검색 결과"
        if kind == "mode":
            return {"S": "작은 미터", "M": "기본 미터와 패널", "L": "넓은 상세 보기"}.get(value, value)
        if kind == "panel":
            return f"{PANEL_TITLES.get(value, value)} 패널"
        if kind == "filter":
            return f"세션 필터 {SESSION_FILTER_TITLES.get(value, value)}"
        if kind == "span":
            return f"그래프 범위 {SPAN_TITLES.get(value, value)}"
        if kind == "rate":
            return f"속도 범위 {RATE_SPAN_TITLES.get(value, value)}"
        if kind == "chip":
            return "한도 패널 보기"
        if kind == "row":
            try:
                row = self.rows[int(value)]
                if self.panel == "sessions":
                    speed = f"메인 {_rate(row[5])} tokens/s" if row[5] >= 0.01 else "메인 속도 없음"
                    context = ctx_status_caption(row[10], row[11])
                    return (f"{ATTENTION_LABELS.get(row[1], row[1])} · {row[2]} · "
                            f"{row[3] or '모델 미상'} · {speed} · 누적 {_fmt(row[9])} · "
                            f"컨텍스트 {context} · 세션 상세")
                return f"{PANEL_TITLES.get(self.panel, self.panel)} 항목 {row[0]}"
            except (IndexError, TypeError, ValueError):
                return "목록 항목"
        return name

    def _sync_controls(self) -> None:
        """QPainter 위에 키보드·스크린리더용 네이티브 버튼을 얹는다."""
        signature = tuple(
            (name, *(round(float(value), 1) for value in rect), self._control_caption(name))
            for name, rect in self._hit.items()
        )
        if signature == self._control_signature:
            return
        self._control_signature = signature
        wanted = set(self._hit)
        for name in self._hit:
            button = self._controls.get(name)
            if button is None:
                button = QToolButton(self)
                button.setStyleSheet(self._control_style())
                button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                button.installEventFilter(self)
                button.clicked.connect(lambda _checked=False, key=name: self._activate_target(key))
                self._controls[name] = button
            x, y, width, height = self._hit[name]
            width, height = max(MIN_HIT, width), max(MIN_HIT, height)
            x -= max(0.0, width - self._hit[name][2]) / 2.0
            y -= max(0.0, height - self._hit[name][3]) / 2.0
            button.setGeometry(
                max(0, int(round(x))), max(0, int(round(y))),
                max(1, min(self.width(), int(round(width)))),
                max(1, min(self.height(), int(round(height)))),
            )
            caption = self._control_caption(name)
            button.setAccessibleName(caption)
            button.setToolTip(caption)
            button.show()
            button.raise_()
        for name, button in self._controls.items():
            if name not in wanted:
                button.hide()

    def _update_accessibility(self) -> None:
        counts = self._counts()
        scope = "오늘" if self.scope == "today" else "누적"
        status = self.state_error or health_note(self.status, time.time())
        bits = [
            f"{scope} 전체 출력 처리량 {_rate(self.rate) or '0'} tokens/s",
            cost_caption(self._approx(), self._scoped().get("cost_usd")),
            f"확인 {counts['check']}개, 작업 {counts['working']}개, 대기 {counts['waiting']}개",
        ]
        if status:
            bits.append(status)
        self.setAccessibleDescription(". ".join(bits))
        self.setToolTip(
            "API 환산가는 구독 사용량을 공개 API 단가로 계산한 값이며 실제 청구액이 아닙니다."
            if self._approx() else "비용은 로그 토큰과 공개 API 단가로 계산한 예상값입니다."
        )

    def _move_control_focus(self, current: Any, step: int) -> None:
        buttons = sorted(
            (button for button in self._controls.values() if button.isVisible()),
            key=lambda button: (button.y(), button.x()),
        )
        if not buttons:
            return
        try:
            index = buttons.index(current)
        except ValueError:
            index = 0
        buttons[(index + step) % len(buttons)].setFocus(Qt.FocusReason.TabFocusReason)

    def eventFilter(self, watched: Any, event: Any) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.KeyPress:
            modifiers = event.modifiers()
            command = bool(modifiers & (Qt.KeyboardModifier.ControlModifier |
                                        Qt.KeyboardModifier.MetaModifier))
            if command and event.key() == Qt.Key.Key_K:
                self._open_palette()
                return True
            if command and event.key() == Qt.Key.Key_Comma:
                self._popup_menu()
                return True
            if watched is self._search_field:
                if event.key() == Qt.Key.Key_Up:
                    self._move_palette(-1)
                    return True
                if event.key() == Qt.Key.Key_Down:
                    self._move_palette(1)
                    return True
                if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    self._activate_palette()
                    return True
                if event.key() == Qt.Key.Key_Escape:
                    self._close_palette()
                    return True
            if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Up):
                self._move_control_focus(watched, -1)
                return True
            if event.key() in (Qt.Key.Key_Right, Qt.Key.Key_Down):
                self._move_control_focus(watched, 1)
                return True
            if event.key() == Qt.Key.Key_Escape:
                self._escape()
                return True
        if event.type() == QEvent.Type.Wheel:
            self._scroll_rows(event.angleDelta().y())
            return True
        if event.type() == QEvent.Type.ContextMenu:
            self._popup_menu(event.globalPos())
            return True
        return super().eventFilter(watched, event)

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        p = QPainter(self)
        self._painter = p
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        try:
            s = self.scale
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            p.fillRect(QRectF(0, 0, self.width(), self.height()), QColor(0, 0, 0, 0))
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            # ponytail: 단일 alpha surface. 프로파일링으로 여유가 확인될 때만 native blur를 붙인다.
            surface = QRectF(1.0 * s, 1.0 * s, self.width() - 2.0 * s, self.height() - 2.0 * s)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_c(self.colors["shadow"], 34 if not self.reduce_transparency else 20))
            p.drawRoundedRect(surface.translated(0, 1.5 * s), RADIUS_XL * s, RADIUS_XL * s)
            alpha = 255 if self.reduce_transparency else int(self.colors["surface_alpha"])
            p.setBrush(_c(self.colors["surface_glass"], alpha))
            p.drawRoundedRect(surface, RADIUS_XL * s, RADIUS_XL * s)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(_c(self.colors["separator"], int(self.colors["edge_alpha"])))
            p.drawRoundedRect(surface, RADIUS_XL * s, RADIUS_XL * s)
            self._hit = {}
            if self.palette_open:
                y = self._draw_header(PAD * s, PAD * s, self.width() - PAD * 2 * s)
                y = self._draw_search(PAD * s, y, self.width() - PAD * 2 * s)
                self._draw_palette(PAD * s, y, self.width() - PAD * 2 * s)
                self._f(7.5)
                self._text(PAD * s, self.height() - FOOT_H * s, self.width() - PAD * 2 * s,
                           FOOT_H * s, "↑↓ 이동 · ↵ 열기 · esc 닫기", self.colors["text_tertiary"])
            elif self.mini:
                self._draw_mini(6 * s, 0, self.width() - 12 * s)
            else:
                y = self._draw_meter(PAD * s, PAD * s, self.width() - PAD * 2 * s)
                y = self._draw_chips(PAD * s, y, self.width() - PAD * 2 * s)
                if self.card:
                    y = self._draw_card(PAD * s, y, self.width() - PAD * 2 * s)
                if self._shows_graph():
                    y = self._draw_graph(PAD * s, y, self.width() - PAD * 2 * s)
                if self.rows_on:
                    y = self._draw_rows(PAD * s, y, self.width() - PAD * 2 * s)
                self._f(7.5)
                hint = self.hint_until > 0.0
                sick = self.state_error or health_note(self.status, time.time())
                foot = HINT if hint else (sick or self._feedback or self.note)
                color = (self.colors["warning"] if hint else
                         (self.colors["destructive"] if sick else self.colors["text_tertiary"]))
                self._text(PAD * s, self.height() - FOOT_H * s, self.width() - PAD * 2 * s,
                           FOOT_H * s, foot, color)
        finally:
            p.end()
            self._painter = None  # type: ignore[assignment]
        self._sync_controls()

    def _draw_header(self, x: float, y: float, w: float) -> float:
        """브랜드와 전역 조작만 둔다. 콘텐츠 탐색은 바로 아래 검색이 맡는다."""
        s, p = self.scale, self._painter
        counts = self._counts()
        dot = self.colors["destructive"] if counts["check"] else (
            self.colors["success"] if counts["working"] else self.colors["text_tertiary"]
        )
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_c(dot))
        p.drawEllipse(QRectF(x, y + 10 * s, 7 * s, 7 * s))
        self._f(10.5, True)
        self._text(x + 13 * s, y, w * 0.42, HEADER_H * s, "TokenMeter")

        close_w, menu_w, scope_w = MODE_BTN * s, MODE_BTN * s, 48 * s
        right = x + w
        self._f(8, True)
        label = "오늘" if self.scope == "today" else "누적"
        self._text(right - close_w - menu_w - scope_w, y, scope_w, HEADER_H * s,
                   label, self.colors["tint"], right=True)
        self._hit["scope"] = (right - close_w - menu_w - scope_w, y, scope_w, HEADER_H * s)
        self._f(11, True)
        self._text(right - close_w - menu_w, y, menu_w, HEADER_H * s, "⋯",
                   self.colors["text_tertiary"], right=True)
        self._hit["menu"] = (right - close_w - menu_w, y, menu_w, HEADER_H * s)
        self._f(10, True)
        self._text(right - close_w, y, close_w, HEADER_H * s, "×",
                   self.colors["text_tertiary"], right=True)
        self._hit["close"] = (right - close_w, y, close_w, HEADER_H * s)
        return y + HEADER_H * s + SPACE_2 * s

    def _draw_search(self, x: float, y: float, w: float) -> float:
        s, p = self.scale, self._painter
        rect = QRectF(x, y, w, SEARCH_H * s)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_c(self.colors["surface_hover"], 18 if self.palette_open else 10))
        p.drawRoundedRect(rect, RADIUS_MD * s, RADIUS_MD * s)
        if self.palette_open:
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(_c(self.colors["tint"], 110))
            p.drawRoundedRect(rect, RADIUS_MD * s, RADIUS_MD * s)
            font = QFont(self._search_font)
            font.setPointSizeF(max(9.0, 9.5 * s))
            self._search_field.setFont(font)
            self._search_field.setTextMargins(int(12 * s), 0, int(44 * s), 0)
            self._search_field.setGeometry(
                int(x + 1 * s), int(y), max(1, int(w - 2 * s)), max(1, int(SEARCH_H * s)),
            )
            self._search_field.show()
            self._f(8)
            self._text(x + w - 40 * s, y, 28 * s, SEARCH_H * s, "esc",
                       self.colors["text_tertiary"], right=True)
        else:
            self._f(9)
            self._text(x + 14 * s, y, w - 76 * s, SEARCH_H * s,
                       "세션 또는 명령 검색", self.colors["text_secondary"])
            self._f(7.5, True)
            self._text(x, y, w - 13 * s, SEARCH_H * s, "⌘K",
                       self.colors["text_tertiary"], right=True)
            self._hit["search"] = (x, y, w, SEARCH_H * s)
        return y + SEARCH_H * s + SPACE_3 * s

    def _draw_palette(self, x: float, y: float, w: float) -> float:
        s, p = self.scale, self._painter
        if not self.palette_results:
            self._f(9)
            self._text(x, y, w, PALETTE_ROW_H * 2 * s, "일치하는 세션이나 명령이 없습니다",
                       self.colors["text_secondary"])
            return y + PALETTE_ROW_H * 3 * s
        for i, item in enumerate(self.palette_results):
            ry, h = y + i * PALETTE_ROW_H * s, (PALETTE_ROW_H - 3) * s
            selected = i == self.palette_index
            if selected:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(_c(self.colors["tint"], 24))
                p.drawRoundedRect(QRectF(x, ry, w, h), RADIUS_SM * s, RADIUS_SM * s)
                p.setBrush(_c(self.colors["tint"], 180))
                p.drawRoundedRect(QRectF(x, ry + 9 * s, 2 * s, h - 18 * s), 1 * s, 1 * s)
            self._f(9.5, True)
            self._text(x + 14 * s, ry + 3 * s, w - 92 * s, 20 * s, str(item["title"]))
            self._f(7.5)
            self._text(x + 14 * s, ry + 21 * s, w - 92 * s, 18 * s, str(item["subtitle"]),
                       self.colors["text_secondary"])
            self._f(7.5, True)
            self._text(x, ry, w - 14 * s, h, str(item["tag"]),
                       self.colors["tint"] if selected else self.colors["text_tertiary"], right=True)
            self._hit[f"palette:{i}"] = (x, ry, w, h)
        return y + len(self.palette_results) * PALETTE_ROW_H * s

    def _draw_meter(self, x: float, y: float, w: float) -> float:
        s, p = self.scale, self._painter
        y0 = y
        tot = self._scoped()

        # TokenMeter의 핵심 조작은 숨기지 않는다: 범위, S/M/L, 메뉴, 닫기.
        counts = self._counts()
        self._f(7.5, True)
        self._text(x, y, w, MODE_BTN * s, "TOKEN METER", self.colors["text_tertiary"])
        bar = MODE_BTN * s * (len(MODES) + 2)
        label = "오늘" if self.scope == "today" else "누적"
        lw = 40 * s
        self._text(x, y, w - bar - 4 * s, MODE_BTN * s, label,
                   self.colors["tint"], right=True)
        self._hit["scope"] = (x + w - bar - lw, y, lw, MODE_BTN * s)
        self._draw_modes(x + w - bar, y)

        y = y0 + 26 * s
        rate = f"{self.rate:.1f}" if 0 < self.rate < 10 else _fmt(self.rate)
        money = cost_caption(self._approx(), tot.get("cost_usd"))
        if counts["check"]:
            side = min(150 * s, w * 0.46)
            p.fillRect(QRectF(x, y, 72 * s, 24 * s), _c(self.colors["destructive"], 28))
            self._f(12, True)
            self._text(x + 6 * s, y, 68 * s, 24 * s, f"확인 {counts['check']}",
                       self.colors["destructive"])
            project = self._check_project()
            self._f(8.5, True)
            self._text(x + 78 * s, y, max(0.0, w - side - 82 * s), 24 * s,
                       project or "세션 목록에서 확인")
            self._hit["attention"] = (x, y, max(96 * s, w - side - 4 * s), 24 * s)
            self._f(7.5, True)
            self._text(x, y, w, 12 * s, f"전체 출력 {rate} tok/s",
                       self.colors["text_tertiary"], right=True)
            self._text(x, y + 12 * s, w, 12 * s, money,
                       self.colors["text_tertiary"], right=True)
        else:
            self._f(19, True)
            self._text(x, y, w, 26 * s, rate,
                       self.colors["text_primary"] if self.rate > 0 else self.colors["text_tertiary"])
            self._f(9, True)
            rate_w = min(w * 0.48, 13 * s * (len(rate) + 1))
            self._text(x + rate_w, y, w - rate_w, 26 * s, "전체 출력 tok/s",
                       self.colors["text_tertiary"])
            self._f(10, True)
            self._text(x, y, w, 26 * s, money, self.colors["text_secondary"], right=True)

        # 세그먼트 게이지가 TokenMeter의 고유한 glanceable identity다.
        y = y0 + 55 * s
        gw, bar_h, lit = w / SEGMENTS, 9 * s, self.gauge * SEGMENTS
        for i in range(SEGMENTS):
            position = i / (SEGMENTS - 1.0)
            on = i < lit
            color = _c(self._seg_color(position), 255 if on else 24)
            if on and i >= lit - 1.0:
                color = _c(self.colors["text_primary"], int(120 + 135 * self.pulse))
            p.fillRect(QRectF(x + i * gw, y, gw - 1.6 * s, bar_h), color)
        if self.peak > 0.01:
            px = x + _clamp(self.peak, 0.0, 1.0) * w - 1.5 * s
            p.fillRect(QRectF(px, y - 2 * s, 1.6 * s, bar_h + 4 * s),
                       _c(self.colors["text_primary"], 150))

        y = y0 + 70 * s
        self._f(7.5, True)
        cache = _int(tot.get("cache_read")) + _int(tot.get("cache_write"))
        saved = _float(tot.get("cache_saved_usd"))
        cw = w / 4.0
        for i, (mark, val, kind) in enumerate((
            ("입력", _fmt(tot.get("input_tokens")), "input"),
            ("출력", _fmt(tot.get("output_tokens")), "output"),
            ("캐시", _fmt(cache), "cache_write"),
            ("절감", _money_short(saved), "input"),
        )):
            self._text(x + cw * i, y, cw - 3 * s, 15 * s, f"{mark} {val}",
                       self._kind_color(kind) if i < 3 else self.colors["text_tertiary"])

        return y0 + METER_H * s

    def _draw_chips(self, x: float, y: float, w: float) -> float:
        marks = self._quota_marks()
        if not marks:
            return y
        s = self.scale
        cw = w / len(marks)
        self._f(7.5, True)
        for i, (text, status) in enumerate(marks):
            color = self.colors["destructive"] if status == "exhausted" else (
                self.colors["warning"] if status in ("warn", "stale") else self.colors["text_tertiary"]
            )
            self._text(x + i * cw, y, cw, CHIP_H * s, text, color)
            self._hit[f"chip:{i}"] = (x + i * cw, y, cw, CHIP_H * s)
        return y + CHIP_H * s

    def _draw_modes(self, x: float, y: float) -> None:
        """항상 보이는 S/M/L, 메뉴, 닫기 — TokenMeter의 직접 조작 경로."""
        s, p = self.scale, self._painter
        bw, h, current = MODE_BTN * s, MODE_BTN * s, self._mode()
        self._f(7.5, True)
        for i, name in enumerate(MODES):
            bx = x + i * bw
            selected = name == current
            p.fillRect(QRectF(bx, y, bw - 2 * s, h),
                       _c(self.colors["tint"] if selected else self.colors["surface_hover"],
                          28 if selected else 10))
            self._text(bx + 4.5 * s, y, bw, h, name,
                       self.colors["tint"] if selected else self.colors["text_tertiary"])
            self._hit[f"mode:{name}"] = (bx, y, bw, h)
        bx = x + len(MODES) * bw
        self._f(10, True)
        self._text(bx + 3 * s, y, bw, h, "⋯", self.colors["text_tertiary"])
        self._hit["menu"] = (bx, y, bw, h)
        bx += bw
        self._f(9, True)
        self._text(bx + 3.5 * s, y, bw, h, "×", self.colors["text_tertiary"])
        self._hit["close"] = (bx, y, bw, h)

    def _draw_mini(self, x: float, y: float, w: float) -> None:
        """미니 모드 — 확인 배지 · 속도 · 세그먼트 게이지만 남긴다."""
        s, p = self.scale, self._painter
        h = float(self.height())
        tot = self._scoped()
        checks = self._counts().get("check", 0)
        if checks:
            pulse = 1.0 if self.reduce_motion else 0.45 + 0.55 * (0.5 + 0.5 * math.sin(self.t * 4.0))
            badge = f"확인 {checks}"
            self._f(8.5, True)
            self._text(x, y, 52 * s, h, badge, self.colors["destructive"],
                       alpha=int(140 + 115 * pulse))
            gx0 = 56 * s
        else:
            rate = f"{self.rate:.1f}" if 0 < self.rate < 10 else _fmt(self.rate)
            self._f(9, True)
            self._text(x, y, 46 * s, h, f"{rate}/s",
                       self.colors["text_primary"] if self.rate > 0 else self.colors["text_tertiary"])
            gx0 = 50 * s
        self._f(8.5, True)
        money = money_caption(self._approx(), tot.get("cost_usd"))
        self._text(x, y, w - MODE_BTN * s, h, money, self.colors["text_secondary"], right=True)
        self._f(10, True)
        menu_x = x + w - MODE_BTN * s
        self._text(menu_x, y, MODE_BTN * s, h, "⋯", self.colors["text_tertiary"], right=True)
        self._hit["menu"] = (menu_x, y, MODE_BTN * s, h)

        segments = SEGMENTS // 2
        gx, gw = x + gx0, max(10.0, w - gx0 - 90 * s)
        bar_h, by, lit = 6 * s, y + (h - 6 * s) / 2.0, self.gauge * segments
        for i in range(segments):
            on = i < lit
            color = _c(self._seg_color(i / (segments - 1.0)), 255 if on else 24)
            if on and i >= lit - 1.0:
                color = _c(self.colors["text_primary"], int(120 + 135 * self.pulse))
            p.fillRect(QRectF(gx + i * gw / segments, by,
                              gw / segments - 1.6 * s, bar_h), color)

    def _draw_card(self, x: float, y: float, w: float) -> float:
        """세션이 막 끝났을 때 한 줄. 클릭하면 사라진다."""
        if not self.card:
            return y
        s, p = self.scale, self._painter
        h = (CARD_H - 6) * s
        p.fillRect(QRectF(x, y, w, h), _c(self.colors["warning"], 24))
        self._f(8, True)
        self._text(x + 6 * s, y, w - 12 * s, h, self.card[1], self.colors["warning"])
        self._hit["card"] = (x, y, w, h)
        return y + CARD_H * s

    # ── 히스토리 그래프 ──────────────────────────────────────────────────
    def _series(self) -> Series:
        """지금 화면에 그릴 시계열. 드릴다운이 걸려 있으면 거기에 맞춰 좁힌다."""
        hours = load_hours(self.status)
        kind, value = self.focus or ("", "")
        if kind == "day":
            return series(hours, "today", now=_day_noon(value))
        return series(hours, self.span, project=value if kind == "project" else None)

    def _draw_graph(self, x: float, y: float, w: float) -> float:
        s, p = self.scale, self._painter
        p.fillRect(QRectF(x, y - 4 * s, w, 1.0), _c(self.colors["separator"], 22))
        y += GRAPH_TOP * s
        if self.panel == "rates":
            return self._draw_rate_graph(x, y, w)

        data = self._series()

        # 머리글 — 무엇을 보고 있는지 + 범위 버튼
        self._f(7.5, True)
        kind, value = self.focus or ("", "")
        title = {"project": f"‹ 전체 · {value}", "day": f"‹ 전체 · {value}"}.get(kind) \
            or "시간별 API 환산 비용 · USD"
        self._text(x, y, w * 0.5, HEAD_H * s, title,
                   self.colors["tint"] if kind else self.colors["text_tertiary"])
        if kind:
            self._hit["back"] = (x, y, w * 0.5, HEAD_H * s)
        self._f(7.5)
        self._text(x + w * 0.5, y, w * 0.5 - BTN_W * 3 * s - 6 * s, HEAD_H * s,
                   summary(data), self.colors["text_tertiary"], right=True)
        self._draw_spans(x + w - BTN_W * 3 * s, y, disabled=kind == "day")
        y += HEAD_H * s + 3 * s

        # 막대 — 비용 비교에는 한 색만 써서 상태색/선택색과 섞이지 않게 한다.
        bh = GRAPH_H * s - GRAPH_TOP * s - HEAD_H * s - 14 * s
        if data.peak <= 0:
            self._f(8)
            self._text(x, y, w, bh, "아직 쌓인 시간이 없습니다 — 한 시간이 지나면 그려집니다",
                       self.colors["text_tertiary"], alpha=150)
            return y + bh + 11 * s
        bw = w / max(1, len(data.bars))
        tallest = max(range(len(data.bars)), key=lambda i: data.bars[i].total)
        for i, bar in enumerate(data.bars):
            bx = x + i * bw
            if bar.total <= 0:  # 빈 칸도 바닥선을 남겨야 축이 이어져 보인다
                p.fillRect(QRectF(bx, y + bh - 1.0 * s, bw - GRAPH_GAP * s, 1.0 * s),
                           _c(self.colors["separator"], 22))
                continue
            part = bh * (bar.total / data.peak)
            cy = y + bh - part
            p.fillRect(QRectF(bx, cy, bw - GRAPH_GAP * s, part), _c(self.colors["tint"], 190))
            if i == tallest:  # 최고점에만 숫자를 붙인다 — 칸마다 쓰면 뭉개진다
                self._f(7)
                self._text(bx - bw, cy - 10 * s, bw * 3, 10 * s, _money_short(bar.total))

        # 축 라벨 — 칸마다 쓰면 뭉개진다. 4칸에 하나씩만.
        y += bh + 1 * s
        self._f(6.5)
        step = max(1, len(data.bars) // 8)
        for i, bar in enumerate(data.bars):
            if i % step == 0:
                self._text(x + i * bw, y, bw * 2, 9 * s, bar.label,
                           self.colors["text_tertiary"], alpha=140)
        return y + 10 * s

    def _draw_rate_graph(self, x: float, y: float, w: float) -> float:
        s, p = self.scale, self._painter
        data = self._rate_view()
        self._f(7.5, True)
        self._text(x, y, w * 0.42, HEAD_H * s, "메인 모델 출력 처리량 · tok/s",
                   self.colors["text_tertiary"])
        self._f(7.0)
        self._text(x + w * 0.42, y, w * 0.18, HEAD_H * s, rate_summary(data),
                   self.colors["text_tertiary"], right=True)
        self._draw_rate_spans(x + w - RATE_BTN_W * 4 * s, y)
        y += HEAD_H * s + 3 * s
        bh = GRAPH_H * s - GRAPH_TOP * s - HEAD_H * s - 14 * s
        if data.peak <= 0:
            self._f(8)
            self._text(x, y, w, bh, "작업이 이어지면 속도가 쌓입니다",
                       self.colors["text_tertiary"], alpha=150)
            return y + bh + 11 * s
        bw = w / max(1, len(data.bars))
        tallest = max(range(len(data.bars)), key=lambda i: data.bars[i].rate)
        for i, bar in enumerate(data.bars):
            bx = x + i * bw
            if bar.rate <= 0:
                p.fillRect(QRectF(bx, y + bh - 1.0 * s, bw - GRAPH_GAP * s, 1.0 * s),
                           _c(self.colors["separator"], 22))
                continue
            part = bh * (bar.rate / data.peak)
            p.fillRect(QRectF(bx, y + bh - part, bw - GRAPH_GAP * s, part),
                       _c(self._kind_color("output"), 200))
            if i == tallest:
                self._f(7)
                self._text(bx - bw, y + bh - part - 10 * s, bw * 3, 10 * s,
                           f"{bar.rate:.0f}")
        y += bh + 1 * s
        self._f(6.5)
        step = max(1, len(data.bars) // 8)
        for i, bar in enumerate(data.bars):
            if i % step == 0:
                self._text(x + i * bw, y, bw * 2, 9 * s, bar.label,
                           self.colors["text_tertiary"], alpha=140)
        return y + 10 * s

    def _draw_rate_spans(self, x: float, y: float) -> None:
        s = self.scale
        self._f(7.0, True)
        for i, name in enumerate(RATE_SPANS):
            bx = x + i * RATE_BTN_W * s
            on = name == self.rate_span
            self._painter.fillRect(QRectF(bx, y, RATE_BTN_W * s - 2 * s, BTN_H * s),
                                   _c(self.colors["tint"] if on else self.colors["surface_hover"],
                                      30 if on else 12))
            self._text(bx, y, RATE_BTN_W * s - 2 * s, BTN_H * s, RATE_SPAN_TITLES[name],
                       self.colors["tint"] if on else self.colors["text_tertiary"])
            self._hit[f"rate:{name}"] = (bx, y, RATE_BTN_W * s - 2 * s, BTN_H * s)

    def _draw_spans(self, x: float, y: float, disabled: bool = False) -> None:
        """[오늘][7일][30일]. 그린 자리를 _hit 에 남겨 클릭이 같은 좌표를 쓰게 한다."""
        s, p = self.scale, self._painter
        self._f(7.0, True)
        for i, name in enumerate(SPANS):
            bx = x + i * BTN_W * s
            rect = QRectF(bx, y, BTN_W * s - 2 * s, BTN_H * s)
            on = (not disabled) and name == self.span
            p.fillRect(rect, _c(self.colors["tint"] if on else self.colors["surface_hover"],
                                30 if on else 12))
            self._text(bx, y, BTN_W * s - 2 * s, BTN_H * s, SPAN_TITLES[name],
                       self.colors["tint"] if on else self.colors["text_tertiary"],
                       alpha=90 if disabled else 255)
            self._hit[f"span:{name}"] = (bx, y, BTN_W * s - 2 * s, BTN_H * s)

    def _draw_rows(self, x: float, y: float, w: float) -> float:
        """세션·한도·속도·일별·팀을 보이는 탭으로 직접 전환한다."""
        s, p = self.scale, self._painter
        p.fillRect(QRectF(x, y - 4 * s, w, 1.0), _c(self.colors["separator"], 22))
        panels = self._visible_panels()
        tab_w = w / max(1, len(panels))
        self._f(7.5, True)
        for i, name in enumerate(panels):
            bx = x + i * tab_w
            selected = name == self.panel
            self._text(bx, y, tab_w, HEAD_H * s, PANEL_TITLES[name],
                       self.colors["tint"] if selected else self.colors["text_tertiary"])
            self._hit[f"panel:{name}"] = (bx, y, tab_w, HEAD_H * s)
        y += HEAD_H * s

        rows = self.rows[:self._row_count()]
        if self.panel == "sessions":
            y = self._draw_session_filters(x, y, w)
            y = self._draw_session_head(x, y, w)
            self._draw_session_rows(x, y, w, rows)
            bottom = y + len(rows) * ROW_H * s + 6 * s
            if self.open_key:
                bottom = self._draw_session_detail(x, bottom, w)
            return bottom
        if self.panel == "rates":
            return self._draw_rate_rows(x, y, w, rows)
        if self.panel == "quota":
            return self._draw_quota_rows(x, y, w, rows)
        if self.panel == "board":
            return self._draw_team_rows(x, y, w, rows)
        mw = MARK_W.get(self.panel, 30.0)  # 표식 칸이 좁으면 이름과 붙어 못 읽는다
        top = max([abs(c) for _, _, _, c, _ in rows] or [0.0]) or 1.0
        for i, (mark, label, tokens, cost, hot) in enumerate(rows):
            ry = y + i * ROW_H * s
            h = (ROW_H - 3) * s
            # 비용 비례 막대가 행 배경이 된다 — 숫자를 읽기 전에 격차가 보인다
            p.fillRect(QRectF(x, ry, w * _clamp(cost / top, 0.0, 1.0), h),
                       _c(self.colors["tint"] if hot else self.colors["surface_hover"],
                          20 if hot else 10))
            self._hit[f"row:{i}"] = (x, ry, w, ROW_H * s)
            if hot:
                p.fillRect(QRectF(x, ry, 2.0 * s, h), _c(self.colors["tint"]))
            self._f(8, True)
            shown_mark = mark[5:] if self.panel == "days" and len(mark) == 10 else mark
            self._text(x + 6 * s, ry, mw * s, h, shown_mark,
                       self.colors["tint"] if i == 0 else self.colors["text_tertiary"])
            self._f(8.5, hot)
            self._text(x + (10 + mw) * s, ry, w * 0.38, h, label,
                       self.colors["text_primary"] if hot else self.colors["text_secondary"])
            self._f(8)
            self._text(x, ry, w - 58 * s, h, _fmt(tokens), self.colors["text_tertiary"], right=True)
            self._f(8.5, True)
            self._text(x, ry, w - 6 * s, h, _money_short(cost),
                       self.colors["text_primary"] if hot else self.colors["text_secondary"], right=True)
        return y + len(rows) * ROW_H * s + 6 * s

    def _draw_session_filters(self, x: float, y: float, w: float) -> float:
        s = self.scale
        self._f(7.0, True)
        width = 58 * s
        for i, name in enumerate(SESSION_FILTERS):
            bx = x + i * width
            selected = name == self.session_filter
            self._painter.fillRect(
                QRectF(bx, y, width - 3 * s, FILTER_H * s),
                _c(self.colors["tint"] if selected else self.colors["surface_hover"],
                   28 if selected else 10),
            )
            self._text(bx, y, width - 3 * s, FILTER_H * s, SESSION_FILTER_TITLES[name],
                       self.colors["tint"] if selected else self.colors["text_tertiary"])
            self._hit[f"filter:{name}"] = (bx, y, width - 3 * s, FILTER_H * s)
        return y + FILTER_H * s

    def _draw_rate_rows(self, x: float, y: float, w: float, rows: List[Any]) -> float:
        s, p = self.scale, self._painter
        if not self.expanded:
            self._draw_rate_spans(x + w - RATE_BTN_W * 4 * s, y)
            y += BTN_H * s + 2 * s
        self._f(6.5, True)
        self._text(x, y, w * 0.34, COLHEAD_H * s, "프로바이더", self.colors["text_tertiary"], alpha=145)
        self._text(x + w * 0.34, y, w * 0.30, COLHEAD_H * s, "메인 모델", self.colors["text_tertiary"], alpha=145)
        self._text(x + w * 0.64, y, w * 0.18 - 4 * s, COLHEAD_H * s,
                   "누적", self.colors["text_tertiary"], right=True, alpha=145)
        self._text(x + w * 0.82, y, w * 0.18 - 6 * s, COLHEAD_H * s,
                   "tok/s", self.colors["text_tertiary"], right=True, alpha=145)
        y += COLHEAD_H * s
        top = max([row[3] for row in rows] or [0.0]) or 1.0
        for i, (vendor, model, tokens, rate, share) in enumerate(rows):
            ry, h = y + i * ROW_H * s, (ROW_H - 3) * s
            p.fillRect(QRectF(x + w * 0.82, ry + h - 2 * s,
                              w * 0.18 * _clamp(rate / top, 0.0, 1.0), 2 * s),
                       _c(self.colors["tint"]))
            self._f(8, True)
            self._text(x + 6 * s, ry, w * 0.34, h, vendor)
            self._f(8)
            self._text(x + w * 0.34, ry, w * 0.30, h, model, self.colors["text_secondary"])
            self._text(x + w * 0.64, ry, w * 0.18 - 4 * s, h, _fmt(tokens),
                       self.colors["text_tertiary"], right=True)
            self._f(8.5, True)
            self._text(x + w * 0.82, ry, w * 0.18 - 6 * s, h, f"{rate:.1f}",
                       self.colors["tint"], right=True)
        if not rows:
            self._f(8)
            self._text(x, y, w, ROW_H * s, "아직 작업 속도 기록이 없습니다",
                       self.colors["text_tertiary"], alpha=150)
        return y + max(1, len(rows)) * ROW_H * s + 6 * s

    def _draw_quota_rows(self, x: float, y: float, w: float, rows: List[Any]) -> float:
        s, p = self.scale, self._painter
        self._f(6.5, True)
        self._text(x, y, w * 0.30, COLHEAD_H * s, "서비스", self.colors["text_tertiary"], alpha=145)
        self._text(x + w * 0.30, y, w * 0.22, COLHEAD_H * s, "기간", self.colors["text_tertiary"], alpha=145)
        self._text(x + w * 0.52, y, w * 0.22 - 4 * s, COLHEAD_H * s,
                   "사용량", self.colors["text_tertiary"], right=True, alpha=145)
        self._text(x + w * 0.74, y, w * 0.26 - 6 * s, COLHEAD_H * s,
                   "다음 리셋", self.colors["text_tertiary"], right=True, alpha=145)
        y += COLHEAD_H * s
        for i, (title, label, used, reset, status) in enumerate(rows):
            ry, h = y + i * ROW_H * s, (ROW_H - 3) * s
            if used >= 0:
                hot = status in ("warn", "exhausted")
                p.fillRect(QRectF(x, ry, w * _clamp(used, 0.0, 1.0), h),
                           _c(self.colors["destructive"] if status == "exhausted" else self.colors["warning"],
                              22 if hot else 12))
            color = self.colors["destructive"] if status == "exhausted" else (
                self.colors["warning"] if status == "warn" else self.colors["text_primary"]
            )
            self._f(8, True)
            self._text(x + 6 * s, ry, w * 0.28, h, title, color)
            self._f(8)
            self._text(x + w * 0.30, ry, w * 0.22, h, label, self.colors["text_secondary"])
            pct = f"사용 {used * 100:.0f}%" if used >= 0 else "미상"
            self._text(x + w * 0.52, ry, w * 0.22 - 4 * s, h, pct,
                       self.colors["text_tertiary"], right=True)
            self._text(x + w * 0.74, ry, w * 0.26 - 6 * s, h,
                       reset or "미상", self.colors["text_tertiary"], right=True)
        if not rows:
            self._f(8)
            self._text(x, y, w, ROW_H * s, "한도 없음", self.colors["text_tertiary"], alpha=150)
        return y + max(1, len(rows)) * ROW_H * s + 6 * s

    def _draw_team_rows(self, x: float, y: float, w: float, rows: List[Any]) -> float:
        s, p = self.scale, self._painter
        if not rows:
            self._f(8)
            self._text(x, y, w, ROW_H * s, "아직 팀 데이터가 없습니다",
                       self.colors["text_tertiary"], alpha=150)
        for i, (handle, check, working, waiting, cost, me) in enumerate(rows):
            ry, h = y + i * ROW_H * s, (ROW_H - 3) * s
            if me:
                p.fillRect(QRectF(x, ry, 2.0 * s, h), _c(self.colors["warning"]))
            self._f(8.5, me)
            self._text(x + 6 * s, ry, w * 0.34, h, handle,
                       self.colors["text_primary"] if me else self.colors["text_secondary"])
            self._f(8, True)
            mark = f"확인 {check}" if check else f"작업 {working}"
            color = self.colors["destructive"] if check else (
                self.colors["success"] if working else self.colors["text_tertiary"])
            self._text(x + w * 0.36, ry, w * 0.34, h, mark, color)
            self._f(8)
            self._text(x, ry, w - 6 * s, h, money_caption(False, cost),
                       self.colors["text_primary"] if me else self.colors["text_tertiary"], right=True)
        return y + max(1, len(rows)) * ROW_H * s + 6 * s

    def _draw_session_detail(self, x: float, y: float, w: float) -> float:
        rec = next((row for row in session_views(self.status) if row["key"] == self.open_key), None)
        if rec is None:
            return y
        s, p = self.scale, self._painter
        h = (DETAIL_H - 4) * s
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_c(self.colors["surface_glass_elevated"],
                      255 if self.reduce_transparency else 220))
        p.drawRoundedRect(QRectF(x, y, w, h), RADIUS_MD * s, RADIUS_MD * s)
        why = check_reason(rec.get("event"))
        waited = wait_caption(time.time(), rec.get("attention_at") or rec.get("last_seen"))
        bits = [part for part in (
            why, f"확인 {waited}" if rec.get("attention") == "check" and waited else "",
            cost_caption(self._approx(), rec.get("cost_usd")),
            short_model(rec.get("model")),
            str(rec.get("vendor") or ""),
            f"서브 {sub_ratio(rec) * 100:.0f}%" if sub_ratio(rec) > 0 else "",
        ) if part]
        self._f(7.5, True)
        self._text(x + 10 * s, y + 2 * s, w - 20 * s, h * 0.5,
                   " · ".join(bits) or "세션 상세")
        self._f(7, True)
        bw = 56 * s
        self._text(x + 10 * s, y + h * 0.5, bw, h * 0.5, "영수증 복사", self.colors["tint"])
        self._hit["act:copy"] = (x + 8 * s, y + h * 0.5, bw + 8 * s, h * 0.5)
        self._text(x + 20 * s + bw, y + h * 0.5, 88 * s, h * 0.5,
                   "프로젝트 사용량", self.colors["tint"])
        self._hit["act:project"] = (x + 16 * s + bw, y + h * 0.5, 96 * s, h * 0.5)
        return y + DETAIL_H * s

    def _draw_session_head(self, x: float, y: float, w: float) -> float:
        """세션 값과 같은 좌표에 칸 이름을 고정한다."""
        s, wide = self.scale, self.expanded
        self._f(6.5, True)
        for name, (start, end, right, _size) in zip(
            SESSION_HEAD_WIDE if wide else SESSION_HEAD,
            SESSION_COLS_WIDE if wide else SESSION_COLS,
        ):
            self._text(x + start * w + CELL_PAD * s, y,
                       (end - start) * w - CELL_PAD * s, COLHEAD_H * s, name,
                       self.colors["text_tertiary"], right=right, alpha=145)
        return y + COLHEAD_H * s

    def _draw_session_rows(self, x: float, y: float, w: float, rows: List[SessionRow]) -> None:
        """상태·프로젝트·속도·누적·컨텍스트를 고정 칸에 그린다."""
        s, p = self.scale, self._painter
        top = max([row[5] for row in rows] or [0.0]) or 1.0
        wide = self.expanded
        focused = self.focus[1] if (self.focus or ("", ""))[0] == "project" else None
        columns = SESSION_COLS_WIDE if wide else SESSION_COLS
        for i, (_key, attention, project, model, effort, rate, vendor, live,
                opened, tokens, ctx, has_window, sub) in enumerate(rows):
            ry = y + i * ROW_H * s
            h = (ROW_H - 3) * s
            state_color = self._state_color(attention)
            if project == focused or _key == self.open_key:
                p.fillRect(QRectF(x, ry, w, h), _c(self.colors["tint"], 20))
            state_end = columns[0][1]
            p.fillRect(QRectF(x, ry, w * state_end, h), _c(state_color, 18))
            p.fillRect(QRectF(x, ry, 2.0 * s, h), _c(state_color))
            rate_column = columns[3] if wide else columns[2]
            if rate >= 0.01:
                p.fillRect(QRectF(x + rate_column[0] * w, ry + h - 2 * s,
                                  (rate_column[1] - rate_column[0]) * w
                                  * _clamp(rate / top, 0.0, 1.0), 2 * s),
                           _c(self.colors["tint"]))
            context_column = columns[5] if wide else columns[4]
            if has_window:
                p.fillRect(QRectF(x + context_column[0] * w, ry + h - 2 * s,
                                  (context_column[1] - context_column[0]) * w
                                  * _clamp(ctx, 0.0, 1.0), 2 * s),
                           _c(self._ctx_color(ctx)))
            self._hit[f"row:{i}"] = (x, ry, w, ROW_H * s)
            speed = _rate(rate) if rate >= 0.01 else "—"
            context = ctx_status_caption(ctx, has_window)
            if wide:
                cells = [
                    (ATTENTION_LABELS.get(attention, attention), state_color, True),
                    (project, self.colors["text_primary"] if live else self.colors["text_secondary"], live),
                    (model or "미상", self.colors["text_secondary"], False),
                    (speed, self.colors["tint"] if rate >= 0.01 else self.colors["text_tertiary"],
                     rate >= 0.01),
                    (_fmt(tokens), self.colors["text_tertiary"], False),
                    (context, self._ctx_color(ctx) if has_window else self.colors["text_tertiary"],
                     ctx >= CTX_WARN),
                    (opened, self.colors["text_tertiary"], False),
                    (effort or "—", self.colors["warning"], False),
                ]
            else:
                cells = [
                    (ATTENTION_LABELS.get(attention, attention), state_color, True),
                    (project, self.colors["text_primary"] if live else self.colors["text_secondary"], live),
                    (speed, self.colors["tint"] if rate >= 0.01 else self.colors["text_tertiary"],
                     rate >= 0.01),
                    (_fmt(tokens), self.colors["text_tertiary"], False),
                    (context, self._ctx_color(ctx) if has_window else self.colors["text_tertiary"],
                     ctx >= CTX_WARN),
                ]
            for (text, color, bold), (start, end, right, size) in zip(cells, columns):
                if not text:
                    continue
                self._f(size, bold)
                cell_x = x + start * w + CELL_PAD * s
                cell_width = (end - start) * w - CELL_PAD * s
                shown = QFontMetricsF(self._font).elidedText(
                    text, Qt.TextElideMode.ElideRight, cell_width,
                )
                self._text(cell_x, ry, cell_width, h, shown, color, right=right)

    # ── 상호작용 ─────────────────────────────────────────────────────────
    def _hit_test(self, pos: Any) -> str:
        """클릭 지점이 어느 영역인지. 그린 쪽이 남긴 좌표(_hit)를 그대로 쓴다."""
        px, py = pos.x(), pos.y()
        for name, (hx, hy, hw, hh) in self._hit.items():
            if hx <= px <= hx + hw and hy <= py <= hy + hh:
                return name
        return ""

    def mousePressEvent(self, event: Any) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._end_hint()  # 손을 댔으면 이미 읽었거나 필요 없다는 뜻이다
        if self._click(event.position()):
            return  # 클릭으로 소비했다 — 드래그를 시작하면 창이 같이 따라 움직인다
        self._drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def _click(self, pos: Any) -> bool:
        """클릭 대상 — 여닫이 버튼, 범위 버튼, 목록 행. 처리했으면 True."""
        return self._activate_target(self._hit_test(pos))

    def _activate_target(self, target: str) -> bool:
        """포인터와 네이티브 키보드 버튼이 공유하는 단일 동작 경로."""
        if not target:
            return False
        if target == "search":
            self._open_palette()
            return True
        if target.startswith("palette:"):
            try:
                self.palette_index = int(target.split(":", 1)[1])
            except ValueError:
                return False
            return self._activate_palette()
        if target.startswith("session:"):
            self._open_session(target.split(":", 1)[1])
            return True
        if target == "command:scope":
            self._toggle_scope()
            return True
        if target == "command:settings":
            self._popup_menu()
            return True
        if target.startswith("mode:"):
            self._set_mode(target.split(":", 1)[1])
            return True
        if target == "close":
            self._hide_overlay()
            return True
        if target == "menu":
            self._popup_menu()
            return True
        if target == "scope":
            self._toggle_scope()
            return True
        if target == "attention":
            self.panel, self.session_filter, self.rows_on, self.scroll = "sessions", "check", True, 0
            self.open_key = ""
            self.focus = None
            self._rebuild_rows()
            self._store_prefs()
            self.update()
            return True
        if target == "back":
            self.focus = None
            self.update()
            return True
        if target == "card":
            self.card = None
            self._resize_to_content()
            return True
        if target.startswith("panel:"):
            self._set_panel(target.split(":", 1)[1])
            return True
        if target.startswith("filter:"):
            self._set_filter(target.split(":", 1)[1])
            return True
        if target.startswith("chip:"):
            self._set_panel("quota")
            return True
        if target.startswith("rate:"):
            self._set_rate_span(target.split(":", 1)[1])
            return True
        if target == "act:copy":
            self._copy_receipt(self.open_key)
            return True
        if target == "act:project":
            rec = next((row for row in session_views(self.status)
                        if row["key"] == self.open_key), None)
            if rec is not None:
                self.focus = ("project", str(rec.get("project") or ""))
                self.panel, self.expanded, self.rows_on, self.open_key = "days", True, True, ""
                self._rebuild_rows()
                self._store_prefs()
                self.update()
            return True
        if target.startswith("row:"):
            self._focus_row(int(target.split(":", 1)[1]))
            return True
        if not self.expanded:
            return False
        if target.startswith("span:"):
            self._set_span(target.split(":", 1)[1])
            return True
        return False

    def _focus_row(self, index: int) -> None:
        """세션 행 = 상세 토글, 일별 행 = 그래프 좁히기."""
        rows = self.rows[:self._row_count()]
        if not 0 <= index < len(rows):
            return
        if self.panel == "sessions":
            key = str(rows[index][0])
            project = str(rows[index][2]).split("/")[-1]
            if self.open_key == key:
                self.open_key = ""
                self.focus = None
            else:
                self.open_key = key
                self.focus = ("project", project)
            self._resize_to_content()
            self.update()
            return
        if self.panel == "days":
            focus = ("day", str(rows[index][0]))
            self.focus = None if self.focus == focus else focus
            if self.focus and not self.expanded:
                self.expanded = True
                self._resize_to_content()
                self._store_prefs()
            self.update()

    def _set_span(self, name: str) -> None:
        self.span = name if name in SPANS else SPANS[0]
        if (self.focus or ("", ""))[0] == "day":
            self.focus = None  # 날짜를 보다가 범위를 누르면 그 범위로 빠져나온다
        self._store_prefs()
        self.update()

    def _set_rate_span(self, name: str) -> None:
        self.rate_span = name if name in RATE_SPANS else RATE_SPANS[0]
        self._rebuild_rows()
        self._store_prefs()
        self.update()

    def _set_filter(self, name: str) -> None:
        self.panel, self.rows_on = "sessions", True
        self.session_filter = name if name in SESSION_FILTERS else "live"
        self.scroll = 0
        self.open_key = ""
        self.focus = None
        self._rebuild_rows()
        self._store_prefs()
        self.update()

    def _copy_receipt(self, key: str) -> None:
        try:
            from .cli import format_receipt, receipt_data

            data = receipt_data(self.status, key)
            if not data:
                return
            QApplication.clipboard().setText(format_receipt(data, "markdown"))
            self._feedback = "영수증을 복사했습니다"
            self.update()
        except Exception:
            pass

    def _mode(self) -> str:
        """지금 크기 — S(미터기만) / M(패널까지) / L(히스토리까지)."""
        return "L" if self.expanded else "M" if self.rows_on else "S"

    def _set_mode(self, name: str) -> None:
        """크기 버튼. 접기/펴기 두 스위치(expanded·rows_on)를 한 단계로 묶는다."""
        if name not in MODES or name == self._mode():
            return
        self.expanded, self.rows_on = name == "L", name != "S"
        if not self.expanded:
            self.focus = None
        self._rebuild_rows()
        self._resize_to_content()
        self._store_prefs()
        self.update()

    def _toggle_expand(self) -> None:
        """히스토리(그래프) 여닫이. 아래 목록(rows_on)은 건드리지 않는다 — 따로 접고 편다."""
        self.expanded = not self.expanded
        if not self.expanded:
            self.focus = None
        self._rebuild_rows()
        self._resize_to_content()
        self._store_prefs()

    def _escape(self) -> None:
        if self.palette_open:
            self._close_palette()
        elif self.focus or self.open_key:
            self.focus = None
            self.open_key = ""
            self._resize_to_content()
            self.update()
        else:
            self._hide_overlay()

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802
        modifiers = event.modifiers()
        command = bool(modifiers & (Qt.KeyboardModifier.ControlModifier |
                                    Qt.KeyboardModifier.MetaModifier))
        if command and event.key() == Qt.Key.Key_K:
            self._open_palette()
            return
        if command and event.key() == Qt.Key.Key_Comma:
            self._popup_menu()
            return
        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            rows = [self._controls[name] for name in sorted(self._controls)
                    if name.startswith("row:") and self._controls[name].isVisible()]
            if rows:
                rows[0 if event.key() == Qt.Key.Key_Down else -1].setFocus(
                    Qt.FocusReason.TabFocusReason)
                return
        if event.key() == Qt.Key.Key_Escape:
            self._escape()

    def mouseMoveEvent(self, event: Any) -> None:  # noqa: N802
        if self._drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, event: Any) -> None:  # noqa: N802
        if self._drag is not None:
            self._drag = None
            self._resize_to_content()
            self._store_prefs()

    def showEvent(self, event: Any) -> None:  # noqa: N802
        super().showEvent(event)
        handle = self.windowHandle()
        if handle is not None and not self._screen_connected:
            handle.screenChanged.connect(lambda _screen: self._resize_to_content())
            self._screen_connected = True
        self._resize_to_content()

    def closeEvent(self, event: Any) -> None:  # noqa: N802
        if self._quitting:
            event.accept()
            return
        event.ignore()
        self._hide_overlay()

    def mouseDoubleClickEvent(self, event: Any) -> None:  # noqa: N802
        if self.mini:
            self._toggle_mini()  # 미니에서 나가는 길이 메뉴뿐이면 갇힌 것처럼 느껴진다

    def wheelEvent(self, event: Any) -> None:  # noqa: N802
        d = event.angleDelta().y()
        if not d:
            return
        # 크기는 S/M/L 만. 휠은 목록만 움직인다.
        if self.rows_on:
            self._scroll_rows(d)

    def _scroll_rows(self, delta: float) -> None:
        """세션 목록 스크롤. 델타를 모아 한 줄치가 차면 넘긴다 (트랙패드가 잘게 보낸다)."""
        self._wheel += delta
        step = int(self._wheel / WHEEL_LINE)  # 0 방향 절삭이라 음수도 그대로 맞는다
        if not step:
            return
        self._wheel -= step * WHEEL_LINE
        self.scroll = max(0, self.scroll - step)  # 위 끝은 여기서, 아래 끝은 _session_rows 에서
        self._rebuild_rows()
        self.update()

    def _act(self, menu: Any, label: str, fn: Any, enabled: bool = True) -> None:
        a = QAction(label, self)
        a.setEnabled(bool(enabled))
        a.triggered.connect(fn)
        menu.addAction(a)

    def contextMenuEvent(self, event: Any) -> None:  # noqa: N802
        self._popup_menu(event.globalPos())

    def _popup_menu(self, pos: Any = None) -> None:
        if self.palette_open:
            self._close_palette()
        menu = QMenu(self)
        menu.setStyleSheet(self._menu_style())
        self._end_hint()
        read_only = self.meter is None or getattr(self.meter, "read_only", False)
        where = pos or self.mapToGlobal(
            QPoint(max(0, self.width() - int(PAD * self.scale)), int((PAD + MODE_BTN) * self.scale))
        )

        if self.mini:  # 미니에서는 패널·그래프 항목이 아무 일도 안 한다 — 아예 안 보인다
            for label, fn in (("검색 또는 명령…  ⌘K", self._open_palette),
                              ("미니 모드 해제", self._toggle_mini),
                              ("항상 위 끄기" if self.on_top else "항상 위 켜기", self._toggle_top),
                              ("오버레이 숨기기 · 측정 계속", self._hide_overlay),
                              ("TokenMeter 종료 · 측정 중지", self._quit)):
                self._act(menu, label, fn)
            menu.exec(where)
            return

        self._act(menu, "검색 또는 명령…  ⌘K", self._open_palette)

        panels = menu.addMenu("콘텐츠")
        panels.setStyleSheet(self._menu_style())
        for name in self._visible_panels():
            mark = "● " if name == self.panel else "   "
            self._act(panels, mark + PANEL_TITLES[name], lambda _=False, n=name: self._set_panel(n))

        filters = menu.addMenu("세션 필터")
        filters.setStyleSheet(self._menu_style())
        for name in SESSION_FILTERS:
            mark = "● " if self.panel == "sessions" and name == self.session_filter else "   "
            self._act(filters, mark + SESSION_FILTER_TITLES[name],
                      lambda _=False, n=name: self._set_filter(n))

        sizes = menu.addMenu(f"창 크기 ({self.scale * 100:.0f}%)")
        sizes.setStyleSheet(self._menu_style())
        for name, label in (("S", "미터만"), ("M", "기본"), ("L", "상세")):
            mark = "● " if name == self._mode() else "   "
            self._act(sizes, mark + label, lambda _=False, n=name: self._set_mode(n))
        sizes.addSeparator()
        self._act(sizes, "크게  +10%", lambda: self._set_scale(self.scale * 1.1), self.scale < 2.0)
        self._act(sizes, "작게  −10%", lambda: self._set_scale(self.scale / 1.1), self.scale > 1.0)
        self._act(sizes, "기본 크기 (100%)", lambda: self._set_scale(1.0))

        appearance = menu.addMenu("모양과 접근성")
        appearance.setStyleSheet(self._menu_style())
        for name, label in (("system", "시스템 테마"), ("dark", "다크"), ("light", "라이트")):
            mark = "● " if name == self.theme_mode else "   "
            self._act(appearance, mark + label, lambda _=False, n=name: self._set_theme(n))
        appearance.addSeparator()
        self._act(appearance, ("✓ " if self.reduce_transparency else "   ") + "투명도 줄이기",
                  self._toggle_transparency)
        self._act(appearance, ("✓ " if self.reduce_motion else "   ") + "모션 줄이기",
                  self._toggle_motion)

        for label, fn, enabled in [
            ("상세 보기 닫기" if self.expanded else "상세 보기 열기", self._toggle_expand, True),
            ("보던 구간 해제", lambda: (setattr(self, "focus", None), self.update()),
             self.focus is not None),
            ("누적 보기" if self.scope == "today" else "오늘 보기", self._toggle_scope, True),
            ("패널 접기" if self.rows_on else "패널 펼치기", self._toggle_rows, True),
            ("미니 모드 (한 줄만)", self._toggle_mini, True),
            ("항상 위 끄기" if self.on_top else "항상 위 켜기", self._toggle_top, True),
            ("종료 요약 끄기" if self.end_card else "종료 요약 켜기", self._toggle_end_card, True),
            ("동기화 중…" if self._sync_busy else "지금 동기화", self._sync_now,
             self.board.online and not self._sync_busy),
            ("통계 초기화", self._reset_stats, not read_only),  # 데몬만 상태를 쓴다
            ("오버레이 숨기기 · 측정 계속", self._hide_overlay, True),
            ("TokenMeter 종료 · 측정 중지", self._quit, True),
        ]:
            self._act(menu, label, fn, enabled)
        menu.exec(where)

    def _set_theme(self, name: str) -> None:
        self.theme_mode = name if name in ("system", "dark", "light") else "dark"
        self._apply_theme()
        self._store_prefs()

    def _toggle_transparency(self) -> None:
        self.reduce_transparency = not self.reduce_transparency
        self._store_prefs()
        self.update()

    def _toggle_motion(self) -> None:
        self.reduce_motion = not self.reduce_motion
        self._store_prefs()
        self.update()

    def _set_scale(self, value: float) -> None:
        self.scale = _clamp(value, 1.0, 2.0)
        self._resize_to_content()
        self._store_prefs()
        self.update()

    def _set_panel(self, name: str) -> None:
        panels = self._visible_panels()
        self.panel = name if name in panels else panels[0]
        self.scroll = 0
        self.open_key = ""
        self.focus = None
        self.rows_on = True  # 패널을 골랐다 = 보겠다는 뜻. 접힌 채로 두면 아무 일도 안 난다
        self._rebuild_rows()
        self._store_prefs()
        self.update()

    def _toggle_end_card(self) -> None:
        self.end_card = not self.end_card
        self._store_prefs()

    def _toggle_scope(self) -> None:
        self.scope = "total" if self.scope == "today" else "today"
        self._refresh_state()
        self._store_prefs()

    def _toggle_rows(self) -> None:
        self.rows_on = not self.rows_on
        self._resize_to_content()
        self._store_prefs()
        self.update()

    def _toggle_mini(self) -> None:
        """미니 ↔ 기본. 접었던 상태(패널·히스토리)는 그대로 두고 돌아온다."""
        self.mini = not self.mini
        self._resize_to_content()
        self._store_prefs()
        self.update()

    def _toggle_top(self) -> None:
        self.on_top = not self.on_top
        self._screen_connected = False  # setWindowFlags가 네이티브 핸들을 다시 만든다
        self._apply_flags()
        self.show()
        self._resize_to_content()
        self._store_prefs()

    def _sync_now(self) -> None:
        if self._sync_busy or not self.board.online:
            return
        self._sync_busy = True
        self._feedback = "동기화 중…"
        snapshot = self.status
        self.update()

        def work() -> None:
            try:
                result = self.board.sync(snapshot, force=True)
                message = str(result.get("status") or "동기화 완료") if isinstance(result, dict) \
                    else "동기화 완료"
            except Exception as exc:
                message = f"동기화 실패 · {type(exc).__name__}"
            self.sync_finished.emit(message)

        threading.Thread(target=work, name="tokenmeter-sync", daemon=True).start()

    def _sync_done(self, message: str) -> None:
        self._sync_busy = False
        self._feedback = message
        self._refresh_state()

    def _reset_stats(self) -> None:
        if self.meter is None or getattr(self.meter, "read_only", False):
            return  # 읽기 전용이면 저장이 안 된다 → 초기화된 척하게 두면 안 됨
        answer = QMessageBox.question(
            self,
            "통계 초기화",
            "누적 통계와 로컬 히스토리를 모두 초기화할까요? 이 작업은 되돌릴 수 없습니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.meter.reset_stats()
        except Exception as exc:
            self._feedback = f"초기화 실패 · {type(exc).__name__}"
            self.update()
            return
        self._last_output = None
        self.rate = 0.0
        self._feedback = "통계를 초기화했습니다"
        self._refresh_state()

    def _setup_tray(self) -> None:
        if QSystemTrayIcon is None:
            return
        try:
            if not QSystemTrayIcon.isSystemTrayAvailable():
                return
            self._tray = QSystemTrayIcon(self)
            self._tray.activated.connect(self._tray_click)
            self._tray_menu = QMenu()
            self._act(self._tray_menu, "TokenMeter 열기", self._tray_click)
            self._act(self._tray_menu, "TokenMeter 종료 · 측정 중지", self._quit)
            self._tray.setContextMenu(self._tray_menu)
            self._update_tray()
            self._tray.show()
        except Exception:
            self._tray = None

    def _tray_click(self, _reason: Any = None) -> None:
        if self.mini:
            self._toggle_mini()
        self.show()
        self.raise_()

    def _update_tray(self) -> None:
        if self._tray is None:
            return
        try:
            n = int(self._counts().get("check") or 0)
            pix = QPixmap(18, 18)
            pix.fill(_c(self.colors["background_primary"]))
            painter = QPainter(pix)
            painter.fillRect(0, 0, 18, 18,
                             _c(self.colors["destructive"] if n else self.colors["text_tertiary"]))
            painter.setPen(_c(self.colors["background_primary"]))
            painter.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, str(n) if n else "·")
            painter.end()
            self._tray.setIcon(pix)
            self._tray.setToolTip(f"확인 {n}" if n else "TokenMeter")
        except Exception:
            pass

    def _quit(self) -> None:
        self._store_prefs()
        if self._tray is not None:
            self._tray.hide()
        self._quitting = True
        self.close()
        try:
            QApplication.quit()
        except Exception:
            pass

    def _hide_overlay(self) -> None:
        if self.palette_open:
            self._close_palette()
        self._store_prefs()
        self.hide()


_WINDOW: Optional[Any] = None  # app_exec=False 일 때 GC 방지용 참조


def _headless() -> bool:
    """디스플레이가 없으면 QApplication 이 abort 하므로 미리 걸러낸다."""
    if os.environ.get("QT_QPA_PLATFORM"):
        return False
    if sys.platform.startswith("linux"):
        return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return False


def run_overlay(meter: Any, app_exec: bool = True, board: Any = None) -> bool:
    """오버레이 실행. GUI 를 못 띄우면 예외 대신 False 를 반환한다.

    app_exec=False 면 창만 띄우고 즉시 반환한다 (데몬이 Qt 루프를 직접 돌 때).
    """
    global _WINDOW
    if QT_ERROR:
        print(f"[TokenMeter] 오버레이를 띄울 수 없습니다 (PyQt6 없음): {QT_ERROR}", file=sys.stderr)
        return False
    if _headless():
        print("[TokenMeter] 오버레이를 띄울 수 없습니다 (디스플레이 없음)", file=sys.stderr)
        return False
    try:
        app = QApplication.instance() or QApplication(sys.argv)
        _WINDOW = MeterWindow(meter, board)
        _WINDOW.show()
    except Exception as exc:
        print(f"[TokenMeter] 오버레이 초기화 실패: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False
    if app_exec:
        try:
            app.exec()
        except Exception as exc:
            print(f"[TokenMeter] 오버레이 루프 종료: {exc}", file=sys.stderr)
            return False
    return True


def _demo() -> None:
    """python3 -m tokenmeter.overlay — 미터 물리 + 레이아웃 자가 검증 (헤드리스).

    "유입이 있으면 차오르고 끊기면 0 으로 내려간다" 가 이 창의 유일한 비자명 로직이다.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if QT_ERROR:
        print(f"PyQt6 없음 — 건너뜁니다: {QT_ERROR}")
        return

    # 게이지 매핑: 단조 증가하고 0~1 에서 클램프된다
    assert gauge_target(0) == 0.0 and gauge_target(DEFAULT_FULL_SCALE * 99) == 1.0
    assert gauge_target(10) < gauge_target(50) < gauge_target(DEFAULT_FULL_SCALE)

    app = QApplication.instance() or QApplication([])
    global _save_prefs
    _save_prefs = lambda _data: None  # noqa: E731 - 자가 검증이 사용자 설정을 덮어쓰면 안 된다
    w = MeterWindow(None)
    w.expanded, w.scroll = False, 0   # 남아 있는 사용자 prefs 와 무관하게 같은 자리에서 시작한다
    w._timer.stop()
    w._poll.stop()
    dt = 1.0 / 60.0

    def run_for(seconds: float, tokens_per_sec: float) -> None:
        """tokens_per_sec 로 출력 토큰이 흘러 들어오는 상황을 그대로 재생한다."""
        for _ in range(int(seconds / dt)):
            if tokens_per_sec:
                w.rate += tokens_per_sec * dt / RATE_TAU
            w._advance(dt)

    assert w.gauge == 0.0 and w.rate == 0.0  # 멈춘 상태에서 시작한다

    # tok/s 는 '초당 들어온 출력 토큰' 으로 수렴한다 — 이게 이 미터의 유일한 계약이다
    run_for(120.0, 60.0)
    assert 57.0 < w.rate < 63.0, w.rate
    over = w.full_scale * 2.0  # 만땅 기준의 두 배로 흘려보낸다
    run_for(120.0, over)
    assert over * 0.95 < w.rate < over * 1.05, w.rate
    assert w.gauge > 0.999, w.gauge  # 상한을 넘기면 클램프된다

    # 한 턴(1500 토큰)이 통째로 도착해도 순간 속도가 1500 tok/s 로 튀지 않는다
    w.rate = 0.0
    w._advance(dt)
    w.rate += 1500 / RATE_TAU
    assert w.rate < 100.0, w.rate

    # 유입이 끊기면 절벽 없이 단조 감소해 완전히 0 이 된다 (미세 잔량이 남지 않는다)
    w.rate, prev = 600.0, 1e9
    for _ in range(int(10.0 / dt)):
        w._advance(dt)
        assert w.rate <= prev, "감쇠가 단조롭지 않다"
        prev = w.rate
    assert 0 < w.rate < 600.0, w.rate  # 10초(<TAU)에는 아직 살아 있다
    run_for(300.0, 0.0)
    assert w.rate == 0.0 and w.gauge == 0.0 and w.peak <= 0.01

    # 오늘/누적 토글은 표시만 바꾸고, 유입 감지는 언제나 누적을 본다
    now = time.time()
    w.status = {"today": {"date": "2026-08-11",
                          "totals": {"cost_usd": 1.0, "output_tokens": 5, "cache_saved_usd": 0.4}},
                "total": {"totals": {"cost_usd": 9.0, "output_tokens": 50,
                                     "cache_saved_usd": 12.0}},
                "days": {"2026-08-09": {"cost_usd": 2.0, "output_tokens": 20},
                         "2026-08-10": {"cost_usd": 3.0, "output_tokens": 30}},
                "sessions": {"claude-code/s1": {"project": "tokenmeter", "last_seen": now - 20,
                                                "model": "claude-opus-5", "vendor": "anthropic",
                                                "effort": "xhigh", "started_at": 1786377600.0,
                                                "ctx": 150_000, "ctx_win": 200_000,
                                                "totals": {"cost_usd": 4.0, "output_tokens": 40}},
                             "claude-code/s2": {"project": "other", "last_seen": now - 10,
                                                "model": "gpt-5.6", "vendor": "openai",
                                                "totals": {"cost_usd": 0.5, "output_tokens": 4}},
                             "claude-code/s3": {"project": "waiting", "last_seen": now - 120,
                                                "totals": {"cost_usd": 0.0, "output_tokens": 0}},
                             "claude-code/s4": {"project": "done", "last_seen": now - 300,
                                                "totals": {"cost_usd": 0.0, "output_tokens": 0}}},
                "live": [{"service": "claude-code", "session_id": "s1", "attention": "check",
                          "attention_at": now - 5},
                         {"service": "claude-code", "session_id": "s2", "attention": "working",
                          "attention_at": now - 10},
                         {"service": "claude-code", "session_id": "s3", "attention": "working",
                          "attention_at": now - 120}],
                "live_count": 3}
    w.scope = "today"
    assert w._scoped()["cost_usd"] == 1.0 and w._totals()["output_tokens"] == 50
    w.scope = "total"
    assert w._scoped()["cost_usd"] == 9.0

    # 랭킹: 비용 내림차순, 내 줄이 표시된다
    entries, note = w.board.board(w.status, "total")
    assert entries and entries[0].me and entries[0].cost_usd == 9.0, entries
    assert note

    # 일별 히스토리: 최근 날짜부터, 진행 중인 오늘이 맨 위에 강조돼 함께 놓인다
    w.panel = "days"
    rows, note = w._build_rows()
    assert [r[0] for r in rows] == ["2026-08-11", "2026-08-10", "2026-08-09"], rows
    assert rows[0][4] and rows[0][3] == 1.0 and not rows[1][4], rows
    assert "6.00" in note, note  # 1 + 3 + 2

    # 세션 목록: 상태 · 프로젝트 · 메인 속도 · 누적 · 컨텍스트를 분리한다.
    w.panel = "sessions"
    w.session_filter = "all"
    w.status["sessions"]["claude-code/s1"]["sub_cost"] = 1.0  # 4.0 중 1.0 = 25%
    rows, note = w._build_rows()
    opened = time.strftime("%m-%d %H:%M", time.localtime(1786377600.0))
    assert [ATTENTION_LABELS[row[1]] for row in rows] == ["확인", "작업", "대기", "종료"], rows
    assert rows[0] == ("claude-code/s1", "check", "tokenmeter", "opus-5", "xhi", 0.0,
                       "anthropic", True, opened, 40, 0.75, True, 0.25), rows[0]
    assert rows[1] == ("claude-code/s2", "working", "other", "gpt-5.6", "", 0.0,
                       "openai", True, "", 4, 0.0, False, 0.0), rows[1]
    assert len(opened) == 11, "날짜~분까지만 — 초는 붙지 않는다"

    # 기본 필터는 라이브 — 종료를 숨긴다. 확인만은 확인 줄만 남긴다.
    w.session_filter = "live"
    live_rows, _ = w._build_rows()
    assert [row[1] for row in live_rows] == ["check", "working", "waiting"], live_rows
    w.session_filter = "check"
    assert [row[1] for row in w._build_rows()[0]] == ["check"]
    w.session_filter = "all"

    # 속도 패널은 프로바이더/모델 행을 만들고, 빈 기록에도 죽지 않는다
    w.panel = "rates"
    rate_rows, rate_note = w._build_rows()
    assert isinstance(rate_rows, list) and ("tok/s" in rate_note or "없습니다" in rate_note)
    w.panel = "sessions"

    # 하위 에이전트 몫: 세션 비용에 이미 들어 있으므로 비율은 1 을 넘지 않는다
    assert sub_ratio({"sub_cost": 3.0, "totals": {"cost_usd": 4.0}}) == 0.75
    assert sub_ratio({"totals": {"cost_usd": 4.0}}) == 0.0, "안 쓴 세션은 빈칸이다"
    assert sub_ratio({"sub_cost": 9.0, "totals": {"cost_usd": 4.0}}) == 1.0
    assert sub_ratio({"sub_cost": 1.0}) == 0.0, "총비용을 모르면 비율도 없다"

    # 컨텍스트 점유: 창을 모르면 아예 안 그리고, 차오를수록 색이 바뀐다
    assert ctx_ratio({"ctx": 150_000, "ctx_win": 200_000}) == 0.75
    assert ctx_ratio({"ctx": 999_999, "ctx_win": 0}) == 0.0, "창을 모르면 % 를 지어내지 않는다"
    assert ctx_ratio({"ctx": 300_000, "ctx_win": 200_000}) == 1.0, "100% 를 넘지 않는다"
    assert ctx_color(0.1) == THEMES["dark"]["text_tertiary"]
    assert ctx_color(0.75) == THEMES["dark"]["warning"]
    assert ctx_color(0.95) != ctx_color(0.75)

    # 구독분이 섞여 있으면 금액에 '≈' 가 붙는다 (청구서가 아니라 환산가)
    assert not w._approx()
    w.status["plans"] = {"subscription": {"totals": {"output_tokens": 10, "cost_usd": 1.0}}}
    assert w._approx()
    del w.status["plans"]
    # 멈춘 세션은 속도 —, 누적 40으로 서로 다른 칸에 남는다.
    assert _rate(0.0) == "" and _fmt(rows[0][9]) == "40"

    # 세션 tok/s: 그 세션의 출력 토큰 증가분만 먹고, 유입이 끊기면 0 으로 식는다.
    # 첫 관측은 기준점이므로 이미 쌓여 있던 누적(40)이 속도로 잡히면 안 된다
    w._track_rates()
    assert not w.rates, w.rates
    w.status["sessions"]["claude-code/s1"]["totals"]["output_tokens"] = 640
    w._track_rates()
    assert 29.0 < w.rates["claude-code/s1"] < 31.0, w.rates  # 600 / TAU
    assert "claude-code/s2" not in w.rates, "안 움직인 세션에 속도가 붙었다"
    rows, _ = w._build_rows()
    assert rows[0][5] > 0 and rows[1][5] == 0.0, rows
    for _ in range(int(300.0 / dt)):
        w._advance(dt)
    assert not w.rates, w.rates
    w.status["sessions"]["claude-code/s1"]["totals"]["output_tokens"] = 40
    w._seen_out.clear()

    # 상태가 먼저이고 그 안에서는 지금 도는 세션이 위로 온다.
    w._track_rates()
    w.rates["claude-code/s2"] = 50.0
    rows, _ = w._build_rows()
    assert rows[0][1] == "check", rows
    w.rates.clear()

    # 이름 줄이기: 칸이 11자쯤이라 접두사/날짜를 떼야 모델이 읽힌다
    assert short_model("claude-sonnet-4-5-20250929") == "sonnet-4-5"
    assert short_model("anthropic/claude-opus-5") == "opus-5"
    assert short_model("gpt-5.6-sol") == "gpt-5.6-sol" and short_model("") == "?"
    assert short_effort("medium") == "med" and short_effort("") == ""
    assert search_score("tknmtr", "TokenMeter") > 0
    assert search_score("사용량", "사용량 히스토리") > search_score("사용량", "팀 사용량")
    assert search_score("없는명령", "TokenMeter") == 0

    # 히스토리가 비어도 죽지 않고 안내만 남는다
    saved, w.status = w.status, {}
    for panel in PANELS:
        w.panel = panel
        w._rebuild_rows()
        assert isinstance(w.note, str)

    # 패널이 늘면 창도 늘고, 패널별 상한을 넘지 않는다
    w.panel, w.rows_on = "board", True
    w.rows = []
    solo = w._size()[1]
    w.rows = [(str(i), f"u{i}", 100 * i, float(i), i == 2) for i in range(PANEL_ROWS["days"] + 3)]
    assert w._row_count() == PANEL_ROWS["board"] and w._size()[1] > solo
    w.panel = "days"
    assert w._row_count() == PANEL_ROWS["days"], "패널마다 줄 수가 달라야 한다"

    # 레이아웃: 어떤 배율/패널에서도 미터·목록·푸터가 창 안에 들어가고 실제로 그려진다
    w.status = saved
    for scale in (0.6, 1.0, 2.0):
        w.scale = scale
        for panel in PANELS:
            w.panel = panel
            w._rebuild_rows()  # 패널마다 줄 모양이 다르다 — 실제 데이터로 그려 봐야 한다
            for rows_on in (True, False):
                w.rows_on = rows_on
                n = w._row_count()
                ww, wh = w._size()
                w.resize(ww, wh)
                extra = (COLHEAD_H + (0 if w.expanded else BTN_H)) if panel == "rates" else (
                    COLHEAD_H if panel == "quota" else 0
                )
                if panel == "sessions":
                    extra = FILTER_H + COLHEAD_H
                head = HEAD_H + extra
                need = (PAD * 2 + METER_H + w._chip_h()
                        + (head + n * ROW_H + SPACE_2 if n else 0) + FOOT_H) * scale
                assert wh >= need - 1.0, (scale, panel, rows_on, wh, need)
                assert not w.grab().isNull(), "paintEvent 가 죽지 않는지 실제로 한 번 그려본다"

    # 메뉴로 패널을 직접 고를 수 있다 (순환만으로는 원하는 패널에 못 닿는다)
    for name in w._visible_panels():
        w._set_panel(name)
        assert w.panel == name
    w._set_panel("없는패널")
    assert w.panel == w._visible_panels()[0], "모르는 이름이면 기본 패널로"
    w.rows_on = False  # 접어둔 상태에서 패널을 고르면 펼쳐진다 (아니면 메뉴가 먹통으로 보인다)
    w._set_panel("days")
    assert w.rows_on and w.panel == "days"

    # ── 확장(히스토리) 모드 ──
    w.scale, w.rows_on, w.panel = 1.0, True, "sessions"
    w._rebuild_rows()
    narrow = w._size()
    w._toggle_expand()
    wide = w._size()
    assert w.expanded and wide[0] > narrow[0] and wide[1] == narrow[1], (narrow, wide)
    assert wide[0] == int(BASE_W * EXPAND_W), wide
    assert w._cap("board") == PANEL_ROWS["board"] * EXPAND_ROWS
    assert w._cap("sessions") == 10
    w.expanded = False
    assert w._cap("sessions") == 10
    w.expanded = True

    # 히스토리와 아래 목록은 서로 독립이다 (접어둔 목록이 그래프에 끌려 열리면 안 된다)
    w.rows_on = False
    w._toggle_expand()
    assert not w.rows_on and not w.expanded
    w._toggle_expand()
    assert not w.rows_on and w.expanded
    w.rows_on = True

    # 스크롤: 10줄을 넘는 세션은 휠로 굴려 본다. 위/아래 끝에서 멈춘다
    w.panel = "sessions"
    book = w.status["sessions"]
    for i in range(14):
        book[f"claude-code/x{i}"] = {"project": f"p{i}", "last_seen": 1786300000.0 - i,
                                     "totals": {"output_tokens": 10, "cost_usd": 0.1}}
    w._rebuild_rows()
    assert len(w.rows) == 10 and "18" in w.note, (len(w.rows), w.note)
    first = w.rows[0]
    w._scroll_rows(-WHEEL_LINE * 3)  # 아래로 세 줄
    assert w.scroll == 3 and w.rows[0] != first
    w._scroll_rows(-WHEEL_LINE * 99)  # 끝을 넘겨도 마지막 화면에서 멈춘다
    assert w.scroll == len(book) - 10, (w.scroll, len(book))
    w._scroll_rows(WHEEL_LINE * 99)
    assert w.scroll == 0 and w.rows[0] == first
    w._scroll_rows(WHEEL_LINE / 3)  # 한 줄에 못 미치는 델타는 모아뒀다 쓴다
    assert w.scroll == 0
    for key in [k for k in book if "/x" in k]:
        del book[key]
    w._rebuild_rows()

    # 기본 화면에서 크기·패널·필터를 직접 누를 수 있다. 검색은 숨겨진 보조 경로다.
    w.panel, w.expanded, w.rows_on = "days", True, True
    w._rebuild_rows()
    assert not w.grab().isNull(), "확장 모드 paintEvent"
    assert "mode:S" in w._hit and "close" in w._hit and "span:7d" in w._hit, w._hit.keys()
    assert "scope" in w._hit and "panel:sessions" in w._hit and "search" not in w._hit, w._hit.keys()
    w.panel = "sessions"
    w._rebuild_rows()
    w.grab()
    assert "panel:rates" in w._hit and "filter:live" in w._hit, w._hit.keys()
    assert not any(k.startswith("span:") for k in w._hit), w._hit.keys()

    # 화면에 그린 S/M/L을 직접 누르면 해당 크기로 전환된다.
    for name, (rows_on, expanded) in (("S", (False, False)), ("M", (True, False)),
                                      ("L", (True, True))):
        w.grab()
        hx, hy, hw, hh = w._hit[f"mode:{name}"]
        assert w._click(QPoint(int(hx + hw / 2), int(hy + hh / 2)))
        assert (w.rows_on, w.expanded) == (rows_on, expanded), (name, w.rows_on, w.expanded)
        assert w._mode() == name, (name, w._mode())
        w.resize(*w._size())
        w.grab()

    # 선택형 빠른 검색: 열면 입력할 수 있고, 닫으면 직접 조작 화면으로 돌아온다.
    w._open_palette()
    w.grab()
    assert w.palette_open and not w._search_field.isHidden() and w.palette_results
    assert "palette:0" in w._hit and not any(k.startswith("panel:") for k in w._hit)
    w._search_field.setText("tknmtr")
    assert any(item["title"] == "tokenmeter" for item in w.palette_results), w.palette_results
    before = w.palette_index
    w._move_palette(1)
    assert w.palette_index != before or len(w.palette_results) == 1
    w._close_palette()
    assert not w.palette_open and w._search_field.isHidden()

    # 라이트·불투명·모션 감소에서도 같은 계층과 렌더 경로를 유지한다.
    w._set_theme("light")
    w.reduce_transparency = w.reduce_motion = True
    w.rate = 100.0
    w._advance(0.1)
    assert w.gauge == gauge_target(w.rate, w.full_scale) and w.peak == 0.0
    assert not w.grab().isNull()
    w._set_theme("dark")
    w.reduce_transparency = w.reduce_motion = False

    # 범위 버튼: 그린 자리를 누르면 그 범위가 된다
    w.panel, w.expanded = "days", True
    w._rebuild_rows()
    w.grab()
    for name in SPANS:
        hx, hy, hw, hh = w._hit[f"span:{name}"]
        assert w._click(QPoint(int(hx + hw / 2), int(hy + hh / 2)))
        assert w.span == name, (name, w.span)

    # 행 클릭 = 그 줄로 좁히기, 같은 줄 재클릭 = 해제, ESC 도 해제
    w.panel = "sessions"
    w._rebuild_rows()
    w.grab()
    hx, hy, hw, hh = w._hit["row:0"]
    center = QPoint(int(hx + hw / 2), int(hy + hh / 2))
    w._click(center)
    assert w.focus == ("project", "tokenmeter"), w.focus
    w._click(center)
    assert w.focus is None, "같은 줄을 다시 누르면 풀린다"

    from PyQt6.QtGui import QKeyEvent  # ESC 로도 풀린다

    w._click(center)
    w.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape,
                              Qt.KeyboardModifier.NoModifier))
    assert w.focus is None, "ESC 가 드릴다운을 풀지 못했다"

    w.panel = "days"
    w._rebuild_rows()
    w.update()
    w.grab()  # days 패널의 행 좌표를 다시 남긴다
    hx, hy, hw, hh = w._hit["row:1"]
    w._click(QPoint(int(hx + hw / 2), int(hy + hh / 2)))
    assert w.focus == ("day", "2026-08-10"), w.focus  # 'MM-DD' 에 연도를 되붙인다
    w._set_span("today")
    assert w.focus is None, "범위를 누르면 날짜 드릴다운에서 빠져나온다"

    # 접으면 드릴다운도 같이 풀리고 원래 크기로 돌아온다
    w.panel = "sessions"
    w._rebuild_rows()
    w.focus = ("project", "tokenmeter")
    w._toggle_expand()
    assert not w.expanded and w.focus is None and w._size() == narrow, w._size()

    # 미니 모드: 창이 작아지고 실제로 그려지며, 더블클릭으로 원래 크기로 돌아온다
    w._rebuild_rows()
    full = w._size()
    w._toggle_mini()
    assert w.mini and w._size()[0] < full[0] and w._size()[1] < full[1], (full, w._size())
    w.resize(*w._size())
    assert not w.grab().isNull(), "미니 모드 paintEvent"
    w._toggle_mini()
    assert not w.mini and w._size() == full, (full, w._size())

    # 첫 실행 힌트: 한 번 보여주고 나면 다시 뜨지 않는다
    w.hint_seen, w.hint_until = False, time.monotonic() + HINT_SEC
    w._end_hint()
    assert w.hint_seen and w.hint_until == 0.0
    w._end_hint()  # 두 번 불러도 안전하다

    # 크기: 100~200%에서만 움직여 24px 조작 타깃을 보존한다.
    w._set_scale(1.0)
    w._set_scale(w.scale * 1.1)
    assert abs(w.scale - 1.1) < 1e-9, w.scale
    w._set_scale(99.0)
    assert 1.0 <= w.scale <= 2.0  # 작은 화면이면 전체가 보이도록 자동 보정한다
    w._set_scale(0.01)
    assert w.scale == 1.0
    print("overlay.py 미터 자가 검증 통과")
    del app


if __name__ == "__main__":
    _demo()
