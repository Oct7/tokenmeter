"""토큰 미터 + 선택형 자체 호스팅 랭킹 오버레이 (Presentation).

QPainter 로만 그린다. 새 의존성 없음 (PyQt6 + math + time).

  · 위쪽은 **미터기** — 세그먼트 게이지의 채움이 곧 **출력 토큰 생성 속도**(tok/s)다.
    유입이 끊기면 지수감쇠로 내려가 0 에서 멈춘다. 곁눈질만으로 에이전트가
    일하는 중인지 알 수 있다는 점이 이 도구의 존재 이유다.
  · 아래쪽은 **패널** — 세션 / 한도 / 속도 / 일별 / 관심현황. 탭으로 고른다.
  · 확인이 있으면 헤더가 비용 대신 그 세션을 가리킨다.
  · 입력/출력/캐시는 색으로 구분한다 (초록/시안/앰버).

GUI 를 못 띄우는 환경에서는 run_overlay() 가 예외 대신 False 를 돌려준다.
"""

from __future__ import annotations

import json
import math
import colorsys
import os
import re
import sys
import threading
import time
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .history import OTHER, SPAN_TITLES, SPANS, Series, load_hours, series, summary
from .leaderboard import Entry, Leaderboard
from .leaderboard import tokens_of as _tokens_of
from .meter import ATTENTION_LABELS, ATTENTION_ORDER, attention_counts, session_views
from .rates import RATE_SPAN_TITLES, RATE_SPANS, RateSeries, load_rates, rate_series, rate_summary
from .views import (
    SESSION_FILTER_TITLES, SESSION_FILTERS, check_reason, ctx_caption,
    filter_sessions, header_attention, health_note, money_caption,
    project_label, wait_caption,
)

try:  # PyQt6 가 없어도 import 자체는 성공해야 한다 (CLI 가 이 모듈을 import 한다)
    from PyQt6.QtCore import QPoint, QRectF, Qt, QTimer
    from PyQt6.QtGui import QAction, QColor, QFont, QFontMetricsF, QPainter, QPixmap
    from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget

    QT_ERROR = ""
except Exception as exc:  # pragma: no cover - 환경 의존
    QT_ERROR = f"{type(exc).__name__}: {exc}"
    QWidget = object  # type: ignore[assignment,misc]  # 클래스 정의만 가능하게 둔다
    QSystemTrayIcon = None  # type: ignore[assignment,misc]
    QPixmap = None  # type: ignore[assignment,misc]

# ── 상수 ──────────────────────────────────────────────────────────────────
BASE_W = 340              # 세션 줄이 5칸이라 300 에서는 이름이 계속 잘린다
METER_H = 93                # 미터 영역 높이 (scale 1.0 기준)
ROW_H = 19                  # 랭킹 한 줄 높이
FOOT_H = 18
PAD = 11
HEAD_H = 13                 # 아래 패널 머리글 높이
# tok/s = **출력 토큰 생성 속도**. 캐시 읽기는 이미 저장된 프롬프트의 재사용이라 어떤
# 속도로 '생성'된 적이 없다 — 실측에서 output 의 300배라 넣는 순간 미터는 캐시 히트율
# 그래프가 된다. 입력/캐시는 아래 ^v~ 줄에서 총량으로 본다.
RATE_TAU = 20.0             # tok/s 지수평균 시정수 (초)
# 게이지 만땅 기준 (출력 tok/s). 세션 하나가 대략 40 tok/s 이므로, 여기까지 차오르는
# 것은 정말 많이 돌 때뿐이다. 체감이 다르면 settings.overlay.full_scale 로 조정.
DEFAULT_FULL_SCALE = 3000.0
SEGMENTS = 28               # 게이지 세그먼트 개수
PREFS_NAME = "overlay.json"

# 아래 패널 — 더블클릭으로 순환한다
MENU_QSS = (
    "QMenu{background:#15161f;color:#e8eaf2;border:1px solid #2a2c3a;padding:4px;}"
    "QMenu::item{padding:5px 18px;}QMenu::item:selected{background:#2f3350;}"
)
PANELS = ("sessions", "quota", "rates", "days", "board")  # 첫 항목이 기본 패널
PANEL_TITLES = {"board": "관심", "days": "일별", "sessions": "세션", "rates": "속도", "quota": "한도"}
PANEL_ROWS = {"board": 5, "days": 7, "sessions": 10, "rates": 6, "quota": 8}
CHIP_H = 14                 # 한도 칩 한 줄 (자격 있을 때만 높이에 더한다)
MARK_W = {"board": 16.0, "days": 36.0}  # 등수(1자) vs 날짜(5자)
Row = Tuple[str, str, int, float, bool]  # (좌측 표식, 이름, 토큰, 비용, 강조)
# 세션 줄 — (내부키, 상태, 프로젝트, 모델, effort, tok/s, 벤더, 라이브, 열린시각, 누적토큰, ctx점유율, 서브몫)
SessionRow = Tuple[str, str, str, str, str, float, str, bool, str, int, float, float]
WHEEL_LINE = 60.0           # 세션 목록 한 줄을 넘기는 휠 델타 (한 칸=120 → 두 줄)
# 세션 줄의 칸 배치 — (시작, 끝, 오른쪽정렬, 글자크기). 창 폭 대비 비율이라
# 배율/폭이 바뀌어도 같이 움직인다. 5칸이라 폭이 빠듯해 넘치는 칸은 접는다.
# 좁은 모드의 마지막 칸은 **벤더 대신 ctx%** 다 — 벤더는 모델명으로 짐작되지만
# 컨텍스트가 얼마나 찼는지는 다른 데서 볼 방법이 없다.
SESSION_COLS = ((0.00, 0.36, False, 8.5), (0.36, 0.58, False, 7.5),
                (0.58, 0.80, True, 8.5), (0.80, 1.00, True, 8.0))
# 확장 모드는 기본 네 칸 뒤에 기존의 세부 정보를 붙인다.
SESSION_COLS_WIDE = ((0.00, 0.22, False, 8.5), (0.22, 0.38, False, 7.5),
                     (0.38, 0.52, True, 8.5), (0.52, 0.62, True, 8.0),
                     (0.62, 0.74, False, 8.0), (0.74, 0.82, False, 7.5),
                     (0.82, 0.90, True, 8.0), (0.90, 1.00, False, 7.5))
CELL_PAD = 4.0
# 상태 글자는 색 띠가 대신한다. 좁은 창에서 잘리지 않게 칸을 비운다.
SESSION_HEAD = ("프로젝트", "모델", "tok/s", "ctx")
SESSION_HEAD_WIDE = SESSION_HEAD + ("시각", "강도", "서브", "벤더")
DETAIL_H = 34.0
CARD_H = 28.0
COLHEAD_H = 11              # 칸 이름 한 줄 높이
CTX_WARN, CTX_HOT = 0.70, 0.90  # 컨텍스트 점유 경고선 (앰버 / 빨강)
SUB_HOT = 0.50                  # 하위 에이전트가 세션 비용의 절반을 넘긴 지점

# 미니 모드 — 게이지 한 줄만. 회의·녹화 중 화면을 비우려고 쓴다
MINI_W, MINI_H = 0.62, 30.0
# 우상단 크기 버튼 — S(미터기만) · M(패널까지) · L(히스토리까지), 그리고 창 닫기
MODES = ("S", "M", "L")
MODE_BTN = 15.0
# 첫 실행 힌트 — 조작이 전부 더블클릭/우클릭에 숨어 있어서, 한 번은 말해줘야 한다
HINT = "탭으로 전환 · 오늘 클릭 · 우클릭 메뉴 · S/M/L 크기"
HINT_SEC = 20.0

# ── 확장(히스토리) 모드 ──
EXPAND_W = 1.5              # 폭 배수 (340 → 510). 24막대 그래프엔 이만해도 충분하다
EXPAND_ROWS = 2             # 목록 줄 수 배수
GRAPH_H = 104               # 그래프 영역 높이 (scale 1.0 기준)
GRAPH_TOP = 8               # 구분선과 '시간별 사용량' 사이 여백
GRAPH_GAP = 1.6             # 막대 사이 간격
BTN_W, BTN_H = 30.0, 13.0   # 범위 버튼 [오늘][7일][30일]
RATE_BTN_W = 36.0           # [1시간][4시간][1일][7일]
EFFORTS = {"minimal": "min", "low": "low", "medium": "med", "high": "high",
           "xhigh": "xhi", "max": "max"}

# 토큰 종류별 색 (요구사항: in/out/cache 를 눈으로 구분)
KIND_COLORS: Dict[str, str] = {
    "input": "#3BE06A",        # 초록
    "output": "#2BD9E5",       # 시안
    "cache_read": "#6B7280",   # 회색
    "cache_write": "#FFC53D",  # 앰버
}
BG = "#0D0E13"
FG = "#E8EAF2"
DIM = "#8B93A7"
LINE = "#232634"
GOLD = "#FFC53D"
STATE_COLORS = {"check": "#FF5F6D", "working": GOLD, "waiting": DIM, "done": "#555B6C"}


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


def project_color(name: str) -> str:
    """프로젝트 이름 → 고정 색. 팔레트를 손으로 관리하지 않으려고 해시로 뽑는다.

    채도/명도를 묶어두면 배경(#0D0E13) 위에서 어느 색이든 읽히고, 서로도 구분된다.
    """
    if name == OTHER:
        return "#4A5061"
    h = (zlib.crc32(name.encode("utf-8")) % 360) / 360.0
    r, g, b = colorsys.hls_to_rgb(h, 0.62, 0.55)
    return f"#{int(r * 255):02X}{int(g * 255):02X}{int(b * 255):02X}"


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
        return "#FF5F6D"
    return GOLD if ratio >= CTX_WARN else DIM


def seg_color(pos: float) -> str:
    """세그먼트 위치별 색 — VU 미터처럼 위로 갈수록 뜨거워진다."""
    if pos < 0.55:
        return "#3BE06A"
    if pos < 0.8:
        return "#FFC53D"
    return "#FF5F6D"


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

    def __init__(self, meter: Any = None, board: Any = None) -> None:
        super().__init__()
        self.meter = meter

        # ── 설정 / 사용자 환경설정 ──
        fps, cfg_scale, cfg_pos = 60, 1.0, [40, 80]
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
        self._last_output: Optional[int] = None
        self.rates: Dict[str, float] = {}       # 세션 키 → tok/s (미터와 같은 지수평균)
        self._seen_out: Dict[str, int] = {}     # 세션 키 → 마지막으로 본 누적 출력

        prefs = _load_prefs()
        self.scale = _clamp(_float(prefs.get("scale"), cfg_scale), 0.6, 2.0)
        self.rows_on = bool(prefs.get("rows", True))
        self.on_top = bool(prefs.get("on_top", True))
        self.scope = prefs.get("scope") if prefs.get("scope") in ("today", "total") else "today"
        self.panel = prefs.get("panel") if prefs.get("panel") in PANELS else PANELS[0]
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
        self._tray = None
        self.quota: Dict[str, Any] = {"windows": [], "errors": {}}
        self._quota_busy = False
        pos = prefs.get("pos") if isinstance(prefs.get("pos"), list) else cfg_pos

        # ── 창 ──
        self.setWindowTitle("TokenMeter")
        self._apply_flags()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(*self._size())
        home = QApplication.primaryScreen().availableGeometry()
        rects = [
            (g.x(), g.y(), g.width(), g.height())
            for g in (s.availableGeometry() for s in QApplication.screens())
        ]
        self.move(*visible_pos(pos, rects, (home.x() + 40, home.y() + 80)))

        # ── 애니메이션 / 계측 상태 ──
        self._t0 = time.monotonic()
        self._last_frame = self._t0
        self.t = 0.0
        self.rate = 0.0           # 출력 토큰 생성 속도의 지수평균 (tok/s)
        self.gauge = 0.0          # 게이지 표시값 (target 을 관성으로 따라간다)
        self.peak = 0.0           # 피크 홀드 마커
        self.pulse = 0.0          # 유입 순간 번쩍임 0~1

        self._font = QFont()
        self._font.setStyleHint(QFont.StyleHint.Monospace)
        self._drag: Optional["QPoint"] = None

        self._refresh_state()
        self._setup_tray()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(max(8, int(1000 / max(1, fps))))
        self._poll = QTimer(self)
        self._poll.timeout.connect(self._refresh_state)
        self._poll.start(200)

    # ── 창 관리 ──────────────────────────────────────────────────────────
    def _row_count(self) -> int:
        """보여줄 줄 수. 데이터가 적을 때 빈 칸을 띄우지 않는다."""
        if not self.rows_on:
            return 0
        return min(self._cap(self.panel), max(1, len(self.rows)))

    def _cap(self, panel: str) -> int:
        """패널이 담는 줄 수. 확장 모드는 폭도 높이도 남으니 배로 보여준다.

        세션만은 접든 펴든 10줄로 고정한다 — 그 아래는 휠로 굴려 본다.
        전체 개수는 머리글의 '세션 N개 기록' 이 알려준다.
        """
        n = PANEL_ROWS.get(panel, 5)
        return n if (not self.expanded or panel == "sessions") else n * EXPAND_ROWS

    def _size(self) -> Tuple[int, int]:
        if self.mini:
            return int(BASE_W * MINI_W * self.scale), int(MINI_H * self.scale)
        rows = self._row_count()
        extra = COLHEAD_H * 2 if self.panel == "sessions" else (
            COLHEAD_H * (1 if self.expanded else 2) if self.panel == "rates" else (
                COLHEAD_H if self.panel == "quota" else 0
            )
        )
        head = HEAD_H + extra
        h = PAD * 2 + METER_H + self._chip_h() + (head + rows * ROW_H + 8 if rows else 0) + FOOT_H
        w = BASE_W * (EXPAND_W if self.expanded else 1.0)
        if self.expanded:
            h += GRAPH_H
        if self.card:
            h += CARD_H
        if self.open_key and self.panel == "sessions":
            h += DETAIL_H
        return int(w * self.scale), int(h * self.scale)

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
            }
        )

    # ── 상태 갱신 (200ms) ────────────────────────────────────────────────
    def _refresh_state(self) -> None:
        st: Dict[str, Any] = {}
        try:
            if self.meter is not None:
                self.meter.reload()
                st = self.meter.status() or {}
        except Exception:
            st = {}
        self.status = st if isinstance(st, dict) else {}
        self._maybe_quota()
        self._track_ended()
        self._track_rates()
        self._rebuild_rows()
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
            # '초당 생성된 출력 토큰' 으로 수렴하고, 유입이 끊기면 절벽 없이 0 으로 간다.
            self.rate += gained / RATE_TAU
            self.pulse = 1.0

    def _track_rates(self) -> None:
        """세션별 출력 tok/s. 미터 바늘과 같은 방식(임펄스 + 지수감쇠)으로 잰다.

        어느 에이전트가 지금 실제로 돌고 있는지는 '마지막 활동 시각' 으로는 안 보인다
        (막 끝난 세션도 방금 활동했다). 세션마다 속도가 있어야 구분된다.
        """
        book = self._sessions()
        for key, rec in book.items():
            if not isinstance(rec, dict):
                continue
            totals = rec.get("totals")
            cur = _int((totals or {}).get("output_tokens")) if isinstance(totals, dict) else 0
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
        cost = money_caption(self._approx(), rec.get("cost_usd"))
        self.card = (time.monotonic() + 5.0, f"{name} 종료 · {cost}")

    def _counts(self) -> Dict[str, int]:
        try:
            return attention_counts(self.status)
        except Exception:
            return {"check": 0, "working": 0, "waiting": 0, "risk": 0}

    def _check_project(self) -> str:
        for rec in session_views(self.status):
            if rec.get("attention") == "check":
                return project_label(rec.get("project"), rec.get("cwd"))
        return ""

    # ── 아래 패널 (관심현황 / 일별 히스토리 / 최근 세션 / 속도) ──────────
    def _rebuild_rows(self) -> None:
        before = self._row_count()
        try:
            self.rows, self.note = self._build_rows()
        except Exception:
            self.rows, self.note = [], ""
        if self._row_count() != before:  # 줄 수가 바뀌면 창이 따라 자란다
            self.resize(*self._size())

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
            rows.append((day[5:] or day, "진행 중" if day == today else "",
                         _tokens_of(totals), _float(totals.get("cost_usd")), day == today))
        if not rows:
            return [], "히스토리 없음 — 하루가 지나면 쌓입니다"
        spent = sum(r[3] for r in rows)
        return rows, f"최근 {len(rows)}일 합계 {_money(spent)}"

    def _session_rows(self) -> Tuple[List[SessionRow], str]:
        """세션 목록 — 상태를 먼저, 그 안에서 속도와 마지막 활동 순으로 표시한다."""
        views = filter_sessions(session_views(self.status), self.session_filter)
        views.sort(key=lambda row: (
            ATTENTION_ORDER[row["attention"]],
            -self.rates.get(row["key"], 0.0),
            -_float(row["last_seen"]),
        ))
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
        note = f"한도 {len(rows)}개" + (f" · 경고 {warn}" if warn else "")
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
            self.resize(*self._size())
        self._advance(dt)
        self.update()

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
              color: str = FG, right: bool = False, alpha: int = 255) -> None:
        self._painter.setPen(_c(color, alpha))
        align = Qt.AlignmentFlag.AlignRight if right else Qt.AlignmentFlag.AlignLeft
        self._painter.drawText(QRectF(x, y, w, h), align | Qt.AlignmentFlag.AlignVCenter, s)

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        p = QPainter(self)
        self._painter = p
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        try:
            s = self.scale
            p.fillRect(QRectF(0, 0, self.width(), self.height()), _c(BG, 236))
            p.fillRect(QRectF(0, 0, self.width(), 1.0 * s), _c("#FFFFFF", 22))
            self._hit = {}
            if self.mini:
                self._draw_mini(6 * s, 0, self.width() - 12 * s)
                return
            y = self._draw_meter(PAD * s, PAD * s, self.width() - PAD * 2 * s)
            y = self._draw_chips(PAD * s, y, self.width() - PAD * 2 * s)
            if self.card:
                y = self._draw_card(PAD * s, y, self.width() - PAD * 2 * s)
            if self.expanded:
                y = self._draw_graph(PAD * s, y, self.width() - PAD * 2 * s)
            if self.rows_on:
                y = self._draw_rows(PAD * s, y, self.width() - PAD * 2 * s)
            self._f(7.5)
            hint = self.hint_until > 0.0
            sick = health_note(self.status, time.time())
            foot = HINT if hint else (sick or self.note)
            color = GOLD if hint else (STATE_COLORS["check"] if sick else DIM)
            self._text(PAD * s, self.height() - FOOT_H * s, self.width() - PAD * 2 * s,
                       FOOT_H * s, foot, color)
        finally:
            p.end()
            self._painter = None  # type: ignore[assignment]

    def _draw_meter(self, x: float, y: float, w: float) -> float:
        s, p = self.scale, self._painter
        tot = self._scoped()

        # 1행: 제목(확인이 있으면 그 세션) + 오늘/누적 + 크기 버튼
        counts = self._counts()
        attention = header_attention(counts, self._check_project())
        self._f(7.5, True)
        title = attention or "TOKEN METER"
        self._text(x, y, w, 13 * s, title, STATE_COLORS["check"] if attention else DIM)
        bar = MODE_BTN * s * (len(MODES) + 1)  # [S][M][L] + [×]
        label = "오늘" if self.scope == "today" else "누적"
        lw = 28 * s
        self._text(x, y, w - bar - 4 * s, 13 * s, label, GOLD, right=True)
        self._hit["scope"] = (x + w - bar - lw, y, lw, 13 * s)
        self._draw_modes(x + w - bar, y)

        # 2행: 지금 속도 + 비용 (구독이면 환산)
        y += 15 * s
        rate = f"{self.rate:.1f}" if 0 < self.rate < 10 else _fmt(self.rate)
        self._f(19, True)
        self._text(x, y, w, 26 * s, rate, FG if self.rate > 0 else DIM)
        self._f(9.5, True)
        rate_w = 13 * s * (len(rate) + 1)
        self._text(x + rate_w, y, w - rate_w, 26 * s, "출력 tok/s", DIM)
        self._f(12, True)
        money = money_caption(self._approx(), tot.get("cost_usd"))
        self._text(x, y, w, 26 * s, money, GOLD, right=True)

        # 3행: 세그먼트 게이지
        y += 29 * s
        gw = w / SEGMENTS
        bar_h = 9 * s
        lit = self.gauge * SEGMENTS
        for i in range(SEGMENTS):
            pos = i / (SEGMENTS - 1.0)
            on = i < lit
            col = _c(seg_color(pos), 255 if on else 26)
            if on and i >= lit - 1.0:  # 선두 세그먼트는 유입 순간 번쩍인다
                col = _c("#FFFFFF", int(120 + 135 * self.pulse))
            p.fillRect(QRectF(x + i * gw, y, gw - 1.6 * s, bar_h), col)
        if self.peak > 0.01:  # 피크 홀드 — 방금 얼마나 빨랐는지 잔상으로 남긴다
            px = x + _clamp(self.peak, 0.0, 1.0) * w - 1.5 * s
            p.fillRect(QRectF(px, y - 2 * s, 1.6 * s, bar_h + 4 * s), _c(FG, 150))

        # 4행: 입력 / 출력 / 캐시
        y += bar_h + 6 * s
        self._f(8.5, True)
        cache = _int(tot.get("cache_read")) + _int(tot.get("cache_write"))
        cw = w / 3.0
        for i, (mark, val, kind) in enumerate((
            ("^", _fmt(tot.get("input_tokens")), "input"),
            ("v", _fmt(tot.get("output_tokens")), "output"),
            ("~", _fmt(cache), "cache_write"),
        )):
            self._text(x + cw * i, y, cw, 15 * s, f"{mark} {val}", KIND_COLORS[kind])
        # 캐시로 아낀 돈 — 캐시 총량 옆이라야 '이 숫자 덕분에' 가 읽힌다
        saved = _float(tot.get("cache_saved_usd"))
        if saved >= 0.005:
            self._f(8, True)
            self._text(x, y, w, 15 * s, f"절감 {_money_short(saved)}",
                       KIND_COLORS["input"], right=True, alpha=190)

        # '지금 도는 세션' 한 줄은 없다 — 바로 아래 세션 목록의 첫 줄과 같은 말이었다
        return y + 17 * s

    def _draw_chips(self, x: float, y: float, w: float) -> float:
        marks = self._quota_marks()
        if not marks:
            return y
        s = self.scale
        cw = w / len(marks)
        self._f(7.5, True)
        for i, (text, status) in enumerate(marks):
            color = STATE_COLORS["check"] if status == "exhausted" else (
                GOLD if status in ("warn", "stale") else DIM
            )
            self._text(x + i * cw, y, cw, CHIP_H * s, text, color)
        return y + CHIP_H * s

    def _draw_modes(self, x: float, y: float) -> None:
        """[S][M][L][×] — 크기 세 단계와 창 닫기. 그린 자리를 _hit 에 남긴다."""
        s, p = self.scale, self._painter
        bw, h, cur = MODE_BTN * s, 13 * s, self._mode()
        self._f(7.5, True)
        for i, name in enumerate(MODES):
            bx = x + i * bw
            on = name == cur
            p.fillRect(QRectF(bx, y, bw - 2 * s, h), _c(GOLD if on else FG, 30 if on else 12))
            self._text(bx + 4.5 * s, y, bw, h, name, GOLD if on else DIM)
            self._hit[f"mode:{name}"] = (bx, y, bw - 2 * s, h)
        bx = x + len(MODES) * bw
        self._f(9, True)
        self._text(bx + 3.5 * s, y, bw, h, "×", DIM)
        self._hit["close"] = (bx, y, bw - 2 * s, h)

    def _draw_mini(self, x: float, y: float, w: float) -> None:
        """미니 모드 — 확인 배지 · 속도 · 게이지. 곁눈질에 필요한 최소치만 남긴다."""
        s, p = self.scale, self._painter
        h = float(self.height())
        tot = self._scoped()
        checks = self._counts().get("check", 0)
        if checks:
            pulse = 0.45 + 0.55 * (0.5 + 0.5 * math.sin(self.t * 4.0))
            badge = f"확인 {checks}"
            self._f(8.5, True)
            self._text(x, y, 52 * s, h, badge, STATE_COLORS["check"], alpha=int(140 + 115 * pulse))
            gx0 = 56 * s
        else:
            rate = f"{self.rate:.1f}" if 0 < self.rate < 10 else _fmt(self.rate)
            self._f(9, True)
            self._text(x, y, 46 * s, h, f"{rate}/s", FG if self.rate > 0 else DIM)
            gx0 = 50 * s
        self._f(8.5, True)
        money = money_caption(self._approx(), tot.get("cost_usd"))
        self._text(x, y, w, h, money, GOLD, right=True)

        segs = SEGMENTS // 2
        gx, gw = x + gx0, max(10.0, w - gx0 - 72 * s)
        bar_h, by = 6 * s, y + (h - 6 * s) / 2.0
        lit = self.gauge * segs
        for i in range(segs):
            on = i < lit
            col = _c(seg_color(i / (segs - 1.0)), 255 if on else 26)
            if on and i >= lit - 1.0:
                col = _c("#FFFFFF", int(120 + 135 * self.pulse))
            p.fillRect(QRectF(gx + i * gw / segs, by, gw / segs - 1.6 * s, bar_h), col)

    def _draw_card(self, x: float, y: float, w: float) -> float:
        """세션이 막 끝났을 때 한 줄. 클릭하면 사라진다."""
        if not self.card:
            return y
        s, p = self.scale, self._painter
        h = (CARD_H - 6) * s
        p.fillRect(QRectF(x, y, w, h), _c(GOLD, 28))
        self._f(8, True)
        self._text(x + 6 * s, y, w - 12 * s, h, self.card[1], GOLD)
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
        p.fillRect(QRectF(x, y - 4 * s, w, 1.0), _c(LINE))
        y += GRAPH_TOP * s
        if self.panel == "rates":
            return self._draw_rate_graph(x, y, w)

        data = self._series()

        # 머리글 — 무엇을 보고 있는지 + 범위 버튼
        self._f(7.5, True)
        kind, value = self.focus or ("", "")
        title = {"project": f"◀ {value}", "day": f"◀ {value}"}.get(kind) or "시간별 사용량"
        self._text(x, y, w * 0.5, HEAD_H * s, title, GOLD if kind else DIM)
        self._f(7.5)
        self._text(x + w * 0.5, y, w * 0.5 - BTN_W * 3 * s - 6 * s, HEAD_H * s,
                   summary(data), DIM, right=True)
        self._draw_spans(x + w - BTN_W * 3 * s, y, disabled=kind == "day")
        y += HEAD_H * s + 3 * s

        # 막대 — 프로젝트별로 아래부터 쌓는다
        bh = GRAPH_H * s - GRAPH_TOP * s - HEAD_H * s - 14 * s
        if data.peak <= 0:
            self._f(8)
            self._text(x, y, w, bh, "아직 쌓인 시간이 없습니다 — 한 시간이 지나면 그려집니다",
                       DIM, alpha=150)
            return y + bh + 11 * s
        bw = w / max(1, len(data.bars))
        tallest = max(range(len(data.bars)), key=lambda i: data.bars[i].total)
        for i, bar in enumerate(data.bars):
            bx = x + i * bw
            if bar.total <= 0:  # 빈 칸도 바닥선을 남겨야 축이 이어져 보인다
                p.fillRect(QRectF(bx, y + bh - 1.0 * s, bw - GRAPH_GAP * s, 1.0 * s), _c(LINE))
                continue
            cy = y + bh
            for name, part_value in bar.parts:
                part = bh * (part_value / data.peak)
                cy -= part
                p.fillRect(QRectF(bx, cy, bw - GRAPH_GAP * s, part), _c(project_color(name)))
            if i == tallest:  # 최고점에만 숫자를 붙인다 — 칸마다 쓰면 뭉개진다
                self._f(7)
                self._text(bx - bw, cy - 10 * s, bw * 3, 10 * s, _money_short(bar.total), FG)

        # 축 라벨 — 칸마다 쓰면 뭉개진다. 4칸에 하나씩만.
        y += bh + 1 * s
        self._f(6.5)
        step = max(1, len(data.bars) // 8)
        for i, bar in enumerate(data.bars):
            if i % step == 0:
                self._text(x + i * bw, y, bw * 2, 9 * s, bar.label, DIM, alpha=140)
        return y + 10 * s

    def _draw_rate_graph(self, x: float, y: float, w: float) -> float:
        s, p = self.scale, self._painter
        data = self._rate_view()
        self._f(7.5, True)
        self._text(x, y, w * 0.42, HEAD_H * s, "작업 중 tok/s", DIM)
        self._f(7.0)
        self._text(x + w * 0.42, y, w * 0.18, HEAD_H * s, rate_summary(data), DIM, right=True)
        self._draw_rate_spans(x + w - RATE_BTN_W * 4 * s, y)
        y += HEAD_H * s + 3 * s
        bh = GRAPH_H * s - GRAPH_TOP * s - HEAD_H * s - 14 * s
        if data.peak <= 0:
            self._f(8)
            self._text(x, y, w, bh, "작업이 이어지면 속도가 쌓입니다", DIM, alpha=150)
            return y + bh + 11 * s
        bw = w / max(1, len(data.bars))
        tallest = max(range(len(data.bars)), key=lambda i: data.bars[i].rate)
        for i, bar in enumerate(data.bars):
            bx = x + i * bw
            if bar.rate <= 0:
                p.fillRect(QRectF(bx, y + bh - 1.0 * s, bw - GRAPH_GAP * s, 1.0 * s), _c(LINE))
                continue
            part = bh * (bar.rate / data.peak)
            p.fillRect(QRectF(bx, y + bh - part, bw - GRAPH_GAP * s, part),
                       _c(KIND_COLORS["output"], 200))
            if i == tallest:
                self._f(7)
                self._text(bx - bw, y + bh - part - 10 * s, bw * 3, 10 * s,
                           f"{bar.rate:.0f}", FG)
        y += bh + 1 * s
        self._f(6.5)
        step = max(1, len(data.bars) // 8)
        for i, bar in enumerate(data.bars):
            if i % step == 0:
                self._text(x + i * bw, y, bw * 2, 9 * s, bar.label, DIM, alpha=140)
        return y + 10 * s

    def _draw_rate_spans(self, x: float, y: float) -> None:
        s = self.scale
        self._f(7.0, True)
        for i, name in enumerate(RATE_SPANS):
            bx = x + i * RATE_BTN_W * s
            on = name == self.rate_span
            self._painter.fillRect(QRectF(bx, y, RATE_BTN_W * s - 2 * s, BTN_H * s),
                                   _c(GOLD if on else FG, 30 if on else 12))
            self._text(bx, y, RATE_BTN_W * s - 2 * s, BTN_H * s, RATE_SPAN_TITLES[name],
                       GOLD if on else DIM)
            self._hit[f"rate:{name}"] = (bx, y, RATE_BTN_W * s - 2 * s, BTN_H * s)

    def _draw_spans(self, x: float, y: float, disabled: bool = False) -> None:
        """[오늘][7일][30일]. 그린 자리를 _hit 에 남겨 클릭이 같은 좌표를 쓰게 한다."""
        s, p = self.scale, self._painter
        self._f(7.0, True)
        for i, name in enumerate(SPANS):
            bx = x + i * BTN_W * s
            rect = QRectF(bx, y, BTN_W * s - 2 * s, BTN_H * s)
            on = (not disabled) and name == self.span
            p.fillRect(rect, _c(GOLD if on else FG, 30 if on else 12))
            self._text(bx, y, BTN_W * s - 2 * s, BTN_H * s, SPAN_TITLES[name],
                       GOLD if on else DIM, alpha=90 if disabled else 255)
            self._hit[f"span:{name}"] = (bx, y, BTN_W * s - 2 * s, BTN_H * s)

    def _draw_rows(self, x: float, y: float, w: float) -> float:
        """세션·속도·관심·일별. 탭이 전환 버튼이다."""
        s, p = self.scale, self._painter
        p.fillRect(QRectF(x, y - 4 * s, w, 1.0), _c(LINE))
        tab_w = w / len(PANELS)
        self._f(7.5, True)
        for i, name in enumerate(PANELS):
            bx = x + i * tab_w
            on = name == self.panel
            self._text(bx, y, tab_w, HEAD_H * s, PANEL_TITLES[name],
                       GOLD if on else DIM)
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
                       _c(GOLD if hot else FG, 20 if hot else 10))
            self._hit[f"row:{i}"] = (x, ry, w, h)
            if hot:
                p.fillRect(QRectF(x, ry, 2.0 * s, h), _c(GOLD))
            self._f(8, True)
            self._text(x + 6 * s, ry, mw * s, h, mark, GOLD if i == 0 else DIM)
            self._f(8.5, hot)
            self._text(x + (10 + mw) * s, ry, w * 0.38, h, label, FG if hot else "#C6CCDC")
            self._f(8)
            self._text(x, ry, w - 58 * s, h, _fmt(tokens), DIM, right=True)
            self._f(8.5, True)
            self._text(x, ry, w - 6 * s, h, _money_short(cost),
                       GOLD if hot else "#C6CCDC", right=True)
        return y + len(rows) * ROW_H * s + 6 * s

    def _draw_session_filters(self, x: float, y: float, w: float) -> float:
        s = self.scale
        self._f(6.5, True)
        fw = 40 * s
        for i, name in enumerate(SESSION_FILTERS):
            bx = x + i * fw
            on = name == self.session_filter
            self._painter.fillRect(QRectF(bx, y, fw - 3 * s, COLHEAD_H * s),
                                   _c(GOLD if on else FG, 28 if on else 10))
            self._text(bx, y, fw - 3 * s, COLHEAD_H * s, SESSION_FILTER_TITLES[name],
                       GOLD if on else DIM)
            self._hit[f"filter:{name}"] = (bx, y, fw - 3 * s, COLHEAD_H * s)
        return y + COLHEAD_H * s + 2 * s

    def _draw_rate_rows(self, x: float, y: float, w: float, rows: List[Any]) -> float:
        s, p = self.scale, self._painter
        if not self.expanded:
            self._draw_rate_spans(x + w - RATE_BTN_W * 4 * s, y)
            y += COLHEAD_H * s + 2 * s
        self._f(6.5, True)
        self._text(x, y, w * 0.36, COLHEAD_H * s, "프로바이더", DIM, alpha=125)
        self._text(x + w * 0.36, y, w * 0.28, COLHEAD_H * s, "모델", DIM, alpha=125)
        self._text(x, y, w - 6 * s, COLHEAD_H * s, "tok/s", DIM, right=True, alpha=125)
        y += COLHEAD_H * s
        top = max([row[3] for row in rows] or [0.0]) or 1.0
        for i, (vendor, model, tokens, rate, share) in enumerate(rows):
            ry, h = y + i * ROW_H * s, (ROW_H - 3) * s
            p.fillRect(QRectF(x, ry, w * _clamp(rate / top, 0.0, 1.0), h),
                       _c(KIND_COLORS["output"], 22))
            self._f(8, True)
            self._text(x + 6 * s, ry, w * 0.34, h, vendor, FG)
            self._f(8)
            self._text(x + w * 0.36, ry, w * 0.28, h, model, "#C6CCDC")
            self._text(x, ry, w - 58 * s, h, _fmt(tokens), DIM, right=True)
            self._f(8.5, True)
            self._text(x, ry, w - 6 * s, h, f"{rate:.1f}", KIND_COLORS["output"], right=True)
        if not rows:
            self._f(8)
            self._text(x, y, w, ROW_H * s, "아직 작업 속도 기록이 없습니다", DIM, alpha=150)
        return y + max(1, len(rows)) * ROW_H * s + 6 * s

    def _draw_quota_rows(self, x: float, y: float, w: float, rows: List[Any]) -> float:
        s, p = self.scale, self._painter
        self._f(6.5, True)
        self._text(x, y, w * 0.34, COLHEAD_H * s, "서비스", DIM, alpha=125)
        self._text(x + w * 0.34, y, w * 0.24, COLHEAD_H * s, "창", DIM, alpha=125)
        self._text(x, y, w - 56 * s, COLHEAD_H * s, "사용", DIM, right=True, alpha=125)
        self._text(x, y, w - 6 * s, COLHEAD_H * s, "리셋", DIM, right=True, alpha=125)
        y += COLHEAD_H * s
        for i, (title, label, used, reset, status) in enumerate(rows):
            ry, h = y + i * ROW_H * s, (ROW_H - 3) * s
            if used >= 0:
                hot = status in ("warn", "exhausted")
                p.fillRect(QRectF(x, ry, w * _clamp(used, 0.0, 1.0), h),
                           _c(STATE_COLORS["check"] if status == "exhausted" else GOLD,
                              22 if hot else 12))
            color = STATE_COLORS["check"] if status == "exhausted" else (
                GOLD if status == "warn" else FG
            )
            self._f(8, True)
            self._text(x + 6 * s, ry, w * 0.32, h, title, color)
            self._f(8)
            self._text(x + w * 0.34, ry, w * 0.24, h, label, "#C6CCDC")
            pct = f"{used * 100:.0f}%" if used >= 0 else "-"
            self._text(x, ry, w - 56 * s, h, pct, DIM, right=True)
            self._text(x, ry, w - 6 * s, h, reset or "-", DIM, right=True)
        if not rows:
            self._f(8)
            self._text(x, y, w, ROW_H * s, "한도 없음", DIM, alpha=150)
        return y + max(1, len(rows)) * ROW_H * s + 6 * s

    def _draw_team_rows(self, x: float, y: float, w: float, rows: List[Any]) -> float:
        s, p = self.scale, self._painter
        for i, (handle, check, working, waiting, cost, me) in enumerate(rows):
            ry, h = y + i * ROW_H * s, (ROW_H - 3) * s
            if me:
                p.fillRect(QRectF(x, ry, 2.0 * s, h), _c(GOLD))
            self._f(8.5, me)
            self._text(x + 6 * s, ry, w * 0.34, h, handle, FG if me else "#C6CCDC")
            self._f(8, True)
            mark = f"확인 {check}" if check else f"작업 {working}"
            color = STATE_COLORS["check"] if check else (GOLD if working else DIM)
            self._text(x + w * 0.36, ry, w * 0.34, h, mark, color)
            self._f(8)
            self._text(x, ry, w - 6 * s, h, money_caption(False, cost),
                       GOLD if me else DIM, right=True)
        return y + max(1, len(rows)) * ROW_H * s + 6 * s

    def _draw_session_detail(self, x: float, y: float, w: float) -> float:
        rec = next((row for row in session_views(self.status) if row["key"] == self.open_key), None)
        if rec is None:
            return y
        s, p = self.scale, self._painter
        h = (DETAIL_H - 4) * s
        p.fillRect(QRectF(x, y, w, h), _c(FG, 12))
        why = check_reason(rec.get("event"))
        waited = wait_caption(time.time(), rec.get("attention_at") or rec.get("last_seen"))
        bits = [part for part in (
            why, f"확인 {waited}" if rec.get("attention") == "check" and waited else "",
            money_caption(self._approx(), rec.get("cost_usd")),
            short_model(rec.get("model")),
        ) if part]
        self._f(7.5, True)
        self._text(x + 6 * s, y, w - 12 * s, h * 0.5, " · ".join(bits) or "세션 상세", FG)
        self._f(7, True)
        bw = 56 * s
        self._text(x + 6 * s, y + h * 0.5, bw, h * 0.5, "영수증", GOLD)
        self._hit["act:copy"] = (x + 6 * s, y + h * 0.5, bw, h * 0.5)
        self._text(x + 8 * s + bw, y + h * 0.5, 70 * s, h * 0.5, "이 프로젝트", GOLD)
        self._hit["act:project"] = (x + 8 * s + bw, y + h * 0.5, 70 * s, h * 0.5)
        return y + DETAIL_H * s

    def _draw_session_head(self, x: float, y: float, w: float) -> float:
        """세션 칸 이름. 아래 값과 같은 좌표·정렬이라야 어느 칸을 가리키는지 읽힌다."""
        s = self.scale
        wide = self.expanded
        self._f(6.5, True)
        for name, (a, b, right, _size) in zip(
            SESSION_HEAD_WIDE if wide else SESSION_HEAD,
            SESSION_COLS_WIDE if wide else SESSION_COLS,
        ):
            self._text(x + a * w + CELL_PAD * s, y, (b - a) * w - CELL_PAD * s,
                       COLHEAD_H * s, name, DIM, right=right, alpha=125)
        return y + COLHEAD_H * s

    def _draw_session_rows(self, x: float, y: float, w: float, rows: List[SessionRow]) -> None:
        """상태 | 프로젝트 | 모델 | tok/s | ctx. 확장 모드는 세부 정보를 뒤에 붙인다."""
        s, p = self.scale, self._painter
        top = max([r[5] for r in rows] or [0.0]) or 1.0
        wide = self.expanded
        focused = self.focus[1] if (self.focus or ("", ""))[0] == "project" else None
        for i, (_key, attention, project, model, effort, rate, vendor, live, opened, tokens, ctx, sub) in enumerate(rows):
            ry = y + i * ROW_H * s
            h = (ROW_H - 3) * s
            # 속도 비례 막대 = 지금 누가 토큰을 태우는지. 다 멈춰 있으면 아무 막대도 없다
            if rate >= 0.5:
                p.fillRect(QRectF(x, ry, w * _clamp(rate / top, 0.0, 1.0), h),
                           _c(KIND_COLORS["output"], 30))
            if project == focused:  # 그래프가 지금 보고 있는 줄
                p.fillRect(QRectF(x, ry, w, h), _c(GOLD, 22))
            p.fillRect(QRectF(x, ry, 2.0 * s, h), _c(STATE_COLORS[attention]))
            if ctx > 0:
                p.fillRect(QRectF(x + w * 0.80, ry + h - 2.0 * s, w * 0.18 * _clamp(ctx, 0, 1), 2.0 * s),
                           _c(ctx_color(ctx)))
            self._hit[f"row:{i}"] = (x, ry, w, h)
            cells = [
                (project, FG if live else "#C6CCDC", live),
                (model, "#C6CCDC", False),
                (_rate(rate) or _fmt(tokens), KIND_COLORS["output"] if rate >= 0.5 else DIM, True),
                (ctx_caption(ctx, True) if ctx > 0 else ("창?" if live else ""),
                 ctx_color(ctx) if ctx > 0 else DIM, ctx >= CTX_WARN),
            ]
            if wide:  # 폭이 남을 때만 기존 세부 정보를 뒤에 붙인다
                cells.extend([
                    (opened, DIM, False),
                    (effort, KIND_COLORS["cache_write"], False),
                    (f"{sub * 100:.0f}%" if sub > 0 else "", GOLD if sub >= SUB_HOT else DIM,
                     sub >= SUB_HOT),
                    (vendor, DIM, False),
                ])
            for (text, color, bold), (a, b, right, size) in zip(
                cells, SESSION_COLS_WIDE if wide else SESSION_COLS
            ):
                if not text:
                    continue
                self._f(size, bold)
                cx, cw = x + a * w + CELL_PAD * s, (b - a) * w - CELL_PAD * s
                fm = QFontMetricsF(self._font)
                self._text(cx, ry, cw, h,
                           fm.elidedText(text, Qt.TextElideMode.ElideRight, cw),
                           color, right=right)

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
        target = self._hit_test(pos)
        if target.startswith("mode:"):
            self._set_mode(target.split(":", 1)[1])
            return True
        if target == "close":
            self._quit()
            return True
        if target == "scope":
            self._toggle_scope()
            return True
        if target == "card":
            self.card = None
            self.resize(*self._size())
            return True
        if target.startswith("panel:"):
            self._set_panel(target.split(":", 1)[1])
            return True
        if target.startswith("filter:"):
            self._set_filter(target.split(":", 1)[1])
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
                if not self.expanded:
                    self._toggle_expand()
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
            self.resize(*self._size())
            self.update()
            return
        if self.panel == "days":
            day = str(rows[index][0])  # 'MM-DD' 로 잘려 있다 — 연도를 되붙인다
            node = self.status.get("today")
            year = (str(node.get("date")) if isinstance(node, dict) else "")[:4] or "2026"
            focus = ("day", day if len(day) == 10 else f"{year}-{day}")
            self.focus = None if self.focus == focus else focus
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
        self.session_filter = name if name in SESSION_FILTERS else "live"
        self.scroll = 0
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
            self.note = "영수증을 복사했습니다"
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
        self.resize(*self._size())
        self._store_prefs()
        self.update()

    def _toggle_expand(self) -> None:
        """히스토리(그래프) 여닫이. 아래 목록(rows_on)은 건드리지 않는다 — 따로 접고 편다."""
        self.expanded = not self.expanded
        if not self.expanded:
            self.focus = None
        self._rebuild_rows()
        self.resize(*self._size())
        self._store_prefs()

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape and (self.focus or self.open_key):
            self.focus = None
            self.open_key = ""
            self.resize(*self._size())
            self.update()

    def mouseMoveEvent(self, event: Any) -> None:  # noqa: N802
        if self._drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, event: Any) -> None:  # noqa: N802
        if self._drag is not None:
            self._drag = None
            self._store_prefs()

    def mouseDoubleClickEvent(self, event: Any) -> None:  # noqa: N802
        # 종료가 아니다 — 실수로 닫히면 안 되므로. 누른 곳이 곧 바꾸려는 대상이다.
        if self.mini:
            self._toggle_mini()  # 미니에서 나가는 길이 메뉴뿐이면 갇힌 것처럼 느껴진다
        elif self.rows_on and event.position().y() > (PAD + METER_H + self._chip_h()) * self.scale:
            self._cycle_panel()
        else:
            self._toggle_scope()

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
        menu = QMenu(self)
        menu.setStyleSheet(MENU_QSS)
        self._end_hint()
        read_only = self.meter is None or getattr(self.meter, "read_only", False)

        if self.mini:  # 미니에서는 패널·그래프 항목이 아무 일도 안 한다 — 아예 안 보인다
            for label, fn in (("미니 모드 해제", self._toggle_mini),
                              ("항상 위 끄기" if self.on_top else "항상 위 켜기", self._toggle_top),
                              ("종료", self._quit)):
                self._act(menu, label, fn)
            menu.exec(event.globalPos())
            return

        # 패널·크기는 더블클릭/휠로도 되지만, 그건 아는 사람만 쓴다. 이름이 붙은
        # 항목이 있어야 처음 여는 사람이 히스토리에 닿는다.
        panels = menu.addMenu("패널 보기")
        panels.setStyleSheet(MENU_QSS)
        for name in PANELS:
            mark = "● " if name == self.panel else "   "
            self._act(panels, mark + PANEL_TITLES[name], lambda _=False, n=name: self._set_panel(n))

        sizes = menu.addMenu(f"창 크기 ({self.scale * 100:.0f}%)")
        sizes.setStyleSheet(MENU_QSS)
        for label, factor in (("크게  +10%", 1.1), ("작게  −10%", 1 / 1.1)):
            self._act(sizes, label, lambda _=False, f=factor: self._set_scale(self.scale * f))
        self._act(sizes, "기본 크기 (100%)", lambda: self._set_scale(1.0))

        for label, fn, enabled in [
            ("히스토리 접기" if self.expanded else "히스토리 펼치기", self._toggle_expand, True),
            ("보던 구간 해제", lambda: (setattr(self, "focus", None), self.update()),
             self.focus is not None),
            ("누적 보기" if self.scope == "today" else "오늘 보기", self._toggle_scope, True),
            ("패널 접기" if self.rows_on else "패널 펼치기", self._toggle_rows, True),
            ("미니 모드 (한 줄만)", self._toggle_mini, True),
            ("항상 위 끄기" if self.on_top else "항상 위 켜기", self._toggle_top, True),
            ("종료 요약 끄기" if self.end_card else "종료 요약 켜기", self._toggle_end_card, True),
            ("지금 동기화", self._sync_now, self.board.online),
            ("통계 초기화", self._reset_stats, not read_only),  # 데몬만 상태를 쓴다
            ("종료", self._quit, True),
        ]:
            self._act(menu, label, fn, enabled)
        menu.exec(event.globalPos())

    def _set_scale(self, value: float) -> None:
        self.scale = _clamp(value, 0.6, 2.0)
        self.resize(*self._size())
        self._store_prefs()

    def _set_panel(self, name: str) -> None:
        self.panel = name if name in PANELS else PANELS[0]
        self.scroll = 0
        self.open_key = ""
        self.rows_on = True  # 패널을 골랐다 = 보겠다는 뜻. 접힌 채로 두면 아무 일도 안 난다
        self._rebuild_rows()
        self.resize(*self._size())  # 패널마다 줄 수가 다르다
        self._store_prefs()

    def _toggle_end_card(self) -> None:
        self.end_card = not self.end_card
        self._store_prefs()

    def _toggle_scope(self) -> None:
        self.scope = "total" if self.scope == "today" else "today"
        self._refresh_state()
        self._store_prefs()

    def _next_panel(self) -> str:
        return PANELS[(PANELS.index(self.panel) + 1) % len(PANELS)] \
            if self.panel in PANELS else PANELS[0]

    def _cycle_panel(self) -> None:
        self._set_panel(self._next_panel())

    def _toggle_rows(self) -> None:
        self.rows_on = not self.rows_on
        self.resize(*self._size())
        self._store_prefs()

    def _toggle_mini(self) -> None:
        """미니 ↔ 기본. 접었던 상태(패널·히스토리)는 그대로 두고 돌아온다."""
        self.mini = not self.mini
        self.resize(*self._size())
        self._store_prefs()

    def _toggle_top(self) -> None:
        self.on_top = not self.on_top
        self._apply_flags()
        self.show()
        self._store_prefs()

    def _sync_now(self) -> None:
        try:
            self.board.sync(self.status, force=True)
        except Exception:
            pass  # sync 자체가 예외를 삼키지만, 설정이 깨져도 창이 죽으면 안 된다
        self._refresh_state()

    def _reset_stats(self) -> None:
        if self.meter is None or getattr(self.meter, "read_only", False):
            return  # 읽기 전용이면 저장이 안 된다 → 초기화된 척하게 두면 안 됨
        try:
            self.meter.reset_stats()
        except Exception:
            pass
        self._last_output = None
        self.rate = 0.0
        self._refresh_state()

    def _setup_tray(self) -> None:
        if QSystemTrayIcon is None:
            return
        try:
            if not QSystemTrayIcon.isSystemTrayAvailable():
                return
            self._tray = QSystemTrayIcon(self)
            self._tray.activated.connect(self._tray_click)
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
            pix.fill(_c(BG))
            painter = QPainter(pix)
            painter.fillRect(0, 0, 18, 18, _c(STATE_COLORS["check"] if n else DIM))
            painter.setPen(_c(BG))
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
        self.close()
        try:
            QApplication.quit()
        except Exception:
            pass


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
    assert seg_color(0.0) != seg_color(0.9), "게이지 끝이 같은 색이면 미터로 안 읽힌다"

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
    assert [r[0] for r in rows] == ["08-11", "08-10", "08-09"], rows
    assert rows[0][4] and rows[0][3] == 1.0 and not rows[1][4], rows
    assert "6.00" in note, note  # 1 + 3 + 2

    # 세션 목록: 프로젝트 · 모델 · tok/s · ctx. 상태 글자는 색 띠가 대신한다.
    w.panel = "sessions"
    w.session_filter = "all"
    w.status["sessions"]["claude-code/s1"]["sub_cost"] = 1.0  # 4.0 중 1.0 = 25%
    rows, note = w._build_rows()
    opened = time.strftime("%m-%d %H:%M", time.localtime(1786377600.0))
    assert SESSION_HEAD == ("프로젝트", "모델", "tok/s", "ctx")
    assert [ATTENTION_LABELS[row[1]] for row in rows] == ["확인", "작업", "대기", "종료"], rows
    assert rows[0] == ("claude-code/s1", "check", "tokenmeter", "opus-5", "xhi", 0.0,
                       "anthropic", True, opened, 40, 0.75, 0.25), rows[0]
    assert rows[1] == ("claude-code/s2", "working", "other", "gpt-5.6", "", 0.0,
                       "openai", True, "", 4, 0.0, 0.0), rows[1]
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
    assert ctx_color(0.1) == DIM and ctx_color(0.75) == GOLD and ctx_color(0.95) != ctx_color(0.75)

    # 구독분이 섞여 있으면 금액에 '≈' 가 붙는다 (청구서가 아니라 환산가)
    assert not w._approx()
    w.status["plans"] = {"subscription": {"totals": {"output_tokens": 10, "cost_usd": 1.0}}}
    assert w._approx()
    del w.status["plans"]
    # 멈춘 세션 줄은 속도 칸에 총 사용량이 대신 들어간다 (빈칸으로 두면 기록이 사라진다)
    assert _rate(0.0) == "" and (_rate(0.0) or _fmt(rows[0][9])) == "40"

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
                head = HEAD_H + (COLHEAD_H if panel == "sessions" else 0)  # 칸 이름 한 줄
                need = (PAD * 2 + METER_H + (head + n * ROW_H + 8 if n else 0) + FOOT_H) * scale
                assert wh >= need - 1.0, (scale, panel, rows_on, wh, need)
                assert not w.grab().isNull(), "paintEvent 가 죽지 않는지 실제로 한 번 그려본다"

    # 메뉴로 패널을 직접 고를 수 있다 (순환만으로는 원하는 패널에 못 닿는다)
    for name in PANELS:
        w._set_panel(name)
        assert w.panel == name
    w._set_panel("없는패널")
    assert w.panel == PANELS[0], "모르는 이름이면 기본 패널로"
    w.rows_on = False  # 접어둔 상태에서 패널을 고르면 펼쳐진다 (아니면 메뉴가 먹통으로 보인다)
    w._set_panel("days")
    assert w.rows_on and w.panel == "days"

    # ── 확장(히스토리) 모드 ──
    w.scale, w.rows_on, w.panel = 1.0, True, "sessions"
    w._rebuild_rows()
    narrow = w._size()
    w._toggle_expand()
    wide = w._size()
    assert w.expanded and wide[0] > narrow[0] and wide[1] > narrow[1], (narrow, wide)
    assert wide[0] == int(BASE_W * EXPAND_W), wide
    assert w._cap("board") == PANEL_ROWS["board"] * EXPAND_ROWS
    # 세션만은 접든 펴든 10줄 — 나머지는 스크롤로 본다
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

    # 그래프가 실제로 그려지고, 그리면서 클릭 영역을 남긴다
    w.resize(*wide)
    assert not w.grab().isNull(), "확장 모드 paintEvent"
    assert "mode:S" in w._hit and "close" in w._hit and "span:7d" in w._hit, w._hit.keys()
    assert "scope" in w._hit and "panel:sessions" in w._hit and "panel:rates" in w._hit, w._hit.keys()
    assert "filter:live" in w._hit, w._hit.keys()

    # 크기 버튼: 누른 자리가 곧 그 단계다 (× 는 종료라 좌표만 확인하고 안 누른다)
    for name, (rows_on, expanded) in (("S", (False, False)), ("M", (True, False)),
                                      ("L", (True, True))):
        hx, hy, hw, hh = w._hit[f"mode:{name}"]
        assert w._click(QPoint(int(hx + hw / 2), int(hy + hh / 2)))
        assert (w.rows_on, w.expanded) == (rows_on, expanded), (name, w.rows_on, w.expanded)
        assert w._mode() == name, (name, w._mode())
        w.resize(*w._size())
        w.grab()  # 단계마다 다시 그려 버튼 좌표를 갱신한다 (폭이 바뀐다)
    cx, _, cw, _ = w._hit["close"]
    assert not any(cx < hx + hw and hx < cx + cw for k, (hx, _, hw, _) in w._hit.items()
                   if k.startswith("mode:")), "× 가 크기 버튼과 겹치면 오클릭으로 종료된다"

    # 범위 버튼: 그린 자리를 누르면 그 범위가 된다
    for name in SPANS:
        hx, hy, hw, hh = w._hit[f"span:{name}"]
        assert w._click(QPoint(int(hx + hw / 2), int(hy + hh / 2)))
        assert w.span == name, (name, w.span)

    # 행 클릭 = 그 줄로 좁히기, 같은 줄 재클릭 = 해제, ESC 도 해제
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

    # 프로젝트 색은 이름마다 고정이고 서로 다르다 (막대를 눈으로 구분하는 근거)
    assert project_color("a") == project_color("a") != project_color("b")
    assert project_color(OTHER) == project_color(OTHER)

    # 크기: 클램프 안에서만 움직이고, 트랙패드 한 번에 끝까지 튀지 않는다
    w._set_scale(1.0)
    w._set_scale(w.scale * 1.1)
    assert abs(w.scale - 1.1) < 1e-9, w.scale
    w._set_scale(99.0)
    assert w.scale == 2.0
    w._set_scale(0.01)
    assert w.scale == 0.6
    swipe = 1.08 ** (20 * 3 / 120.0)  # 트랙패드 관성 스크롤: 작은 델타 20 회
    assert swipe < 1.5, f"한 번 쓸어서 {swipe:.2f}배 — 조절이 안 된다"
    assert 1.08 ** (120 / 120.0) > 1.07, "휠 한 칸은 눈에 띄게 움직여야 한다"
    print("overlay.py 미터 자가 검증 통과")
    del app


if __name__ == "__main__":
    _demo()
