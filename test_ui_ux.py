#!/usr/bin/env python3
"""TokenMeter UI/UX 회귀 검증 — 프레임워크 없이 assert 만 쓴다.

    QT_QPA_PLATFORM=offscreen uv run python test_ui_ux.py

사용자 설정과 화면은 건드리지 않고 Qt의 offscreen 백엔드에서 실제 창을 검증한다.
"""

# ruff: noqa: E402 — Qt 백엔드와 격리된 상태 경로를 import 전에 고정해야 한다.

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Iterator, List

_PROCESS_HOME = tempfile.TemporaryDirectory(prefix="tokenmeter-ui-test-home-")
os.environ["TOKENMETER_HOME"] = str(Path(_PROCESS_HOME.name) / "state")
os.environ["XDG_CONFIG_HOME"] = str(Path(_PROCESS_HOME.name) / "config")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PyQt6.QtCore import QEvent, QPoint, QPointF, QTimer, Qt
from PyQt6.QtGui import QFont, QFontMetricsF, QMouseEvent, QWheelEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMenu, QMessageBox, QStyle

from tokenmeter import overlay
from tokenmeter.config import Config
from tokenmeter.meter import Meter, TokenDelta, session_views
from tokenmeter.overlay import CHIP_H, FOOT_H, PAD, MeterWindow
from tokenmeter.rates import rate_slot
from tokenmeter.views import UNKNOWN_PROJECT, ctx_status_caption, project_key


class _Board:
    online = False

    def team(self, _status: Dict[str, Any]):
        return [], ""


class _Meter:
    read_only = False

    def __init__(self, status: Dict[str, Any] | None = None) -> None:
        self._status = status or {}
        self.reset_calls = 0

    def reload(self) -> None:
        pass

    def status(self) -> Dict[str, Any]:
        return self._status

    def reset_stats(self) -> None:
        self.reset_calls += 1


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


@contextlib.contextmanager
def _window(meter: Any = None) -> Iterator[MeterWindow]:
    """실제 MeterWindow를 띄우되 타이머와 사용자 prefs 쓰기는 막는다."""
    app = _app()
    load_prefs, save_prefs = overlay._load_prefs, overlay._save_prefs
    overlay._load_prefs = lambda: {}
    overlay._save_prefs = lambda _data: None
    window: MeterWindow | None = None
    try:
        window = MeterWindow(meter, _Board())
        window._timer.stop()
        window._poll.stop()
        window.show()
        app.processEvents()
        yield window
    finally:
        overlay._load_prefs, overlay._save_prefs = load_prefs, save_prefs
        if window is not None:
            window._timer.stop()
            window._poll.stop()
            window.close()
            window.deleteLater()
        app.processEvents()


def _sessions(live: bool = True) -> Dict[str, Any]:
    now = time.time()
    sessions = {
        "svc/check": {
            "project": "check", "started_at": now - 300, "last_seen": now,
            "totals": {"output_tokens": 1},
        },
        "svc/older": {
            "project": "older", "started_at": now - 200, "last_seen": now,
            "totals": {"output_tokens": 2},
        },
        "svc/newer": {
            "project": "newer", "started_at": now - 100, "last_seen": now,
            "totals": {"output_tokens": 3},
        },
    }
    rows = []
    if live:
        rows = [
            {"service": "svc", "session_id": "check", "attention": "check",
             "attention_at": now + 1, "updated_at": now},
            {"service": "svc", "session_id": "older", "attention": "working",
             "attention_at": now, "updated_at": now},
            {"service": "svc", "session_id": "newer", "attention": "working",
             "attention_at": now, "updated_at": now},
        ]
    return {"sessions": sessions, "live": rows, "live_count": len(rows)}


def test_session_order_ignores_live_rate_changes(_tmp: Path) -> None:
    """상태 → 라이브 → 시작시각 → key 순서는 순간 tok/s 변화로 뒤집히지 않는다."""
    with _window() as window:
        window.status = _sessions()
        window.session_filter = "all"
        expected = ["svc/check", "svc/newer", "svc/older"]

        window.rates = {"svc/check": 0.1, "svc/newer": 1.0, "svc/older": 999.0}
        first = [row[0] for row in window._session_rows()[0]]
        window.rates = {"svc/check": 999.0, "svc/newer": 900.0, "svc/older": 0.1}
        second = [row[0] for row in window._session_rows()[0]]

        assert first == expected and second == expected, (first, second)


def test_context_caption_distinguishes_zero_unknown_and_hot(_tmp: Path) -> None:
    assert ctx_status_caption(0.0, True) == "0%"
    assert ctx_status_caption(0.0, False) == "미상"
    assert ctx_status_caption(0.95, True) == "95% · 높음"


def test_subagent_output_is_not_presented_as_main_model_speed(_tmp: Path) -> None:
    meter = Meter(Config(services={}, settings={}))
    meter.reset_stats()
    meter.ingest(TokenDelta(
        output_tokens=100, model="main-model", vendor="openai", effort="high",
        service="svc", session="mixed",
    ))
    meter.ingest(TokenDelta(
        output_tokens=300, model="sub-model", vendor="anthropic", effort="low",
        service="svc", session="mixed", subagent=True,
    ))
    rec = meter.state["sessions"]["svc/mixed"]
    view = {row["key"]: row for row in session_views(meter.status())}["svc/mixed"]

    assert rec["totals"]["output_tokens"] == 400
    assert rec["sub_output_tokens"] == 300
    assert (rec["model"], rec["effort"]) == ("main-model", "high")
    assert (view["main_output_tokens"], view["sub_output_tokens"]) == (100, 300)
    assert all("sub-model" not in key for key in meter.state["rate"]["m"]), meter.state["rate"]

    with _window() as window:
        window.status = {"sessions": {"svc/mixed": rec}}
        window._track_rates()  # 과거 누적은 기준점만 잡는다
        rec["totals"]["output_tokens"] += 20
        rec["sub_output_tokens"] += 20
        window._track_rates()
        assert window.rates.get("svc/mixed", 0.0) == 0.0

        rec["totals"]["output_tokens"] += 50
        window._track_rates()
        assert window.rates["svc/mixed"] == 50 / overlay.RATE_TAU


def test_painted_actions_have_native_accessible_controls(_tmp: Path) -> None:
    with _window() as window:
        _app().processEvents()
        close = window._controls["close"]
        assert close.accessibleName() == "오버레이 숨기기 · 측정 계속"
        assert close.width() >= 24 and close.height() >= 24
        assert close.focusPolicy() == Qt.FocusPolicy.StrongFocus
        assert all(button.width() >= 24 and button.height() >= 24
                   for button in window._controls.values() if button.isVisible())
        assert "전체 출력 처리량" in window.accessibleDescription()
        assert "panel:board" not in window._controls or not window._controls["panel:board"].isVisible()


def test_tokenmeter_controls_are_visible_before_optional_search(_tmp: Path) -> None:
    """TokenMeter 계기판이 기본이고, 검색은 요청할 때만 나타난다."""
    with _window() as window:
        window.status = _sessions()
        window._rebuild_rows()
        window.grab()

        expected = {"scope", "mode:S", "mode:M", "mode:L", "panel:sessions",
                    "panel:projects", "panel:quota",
                    "filter:live", "filter:archive", "filter:all"}
        assert expected <= window._hit.keys(), window._hit.keys()
        assert "search" not in window._hit
        assert not window.palette_open and window._search_field.isHidden()

        # 속도·일별은 L(상세)에서만 나온다 — M 에 탭이 6개면 라벨이 붙어 못 읽는다
        assert "panel:rates" not in window._hit and "panel:days" not in window._hit
        window._set_mode("L")
        window.grab()
        assert {"panel:rates", "panel:days"} <= window._hit.keys()
        window._set_mode("M")

        window.hide()
        window._tray_click()
        assert window.isVisible() and not window.palette_open


def test_overlay_submenus_switch_without_hover_delay(_tmp: Path) -> None:
    class _CaptureMenu(QMenu):
        opened: List[QMenu] = []

        def exec(self, *_args: Any) -> None:
            self.opened.append(self)

    saved_menu = overlay.QMenu
    try:
        overlay.QMenu = _CaptureMenu
        with _window() as window:
            window._popup_menu()
            root = _CaptureMenu.opened[-1]
            menus = [root] + [action.menu() for action in root.actions() if action.menu()]
            assert len(menus) > 2, "실제 2단 옵션 메뉴를 검사해야 한다"
            assert all(
                menu.style().styleHint(QStyle.StyleHint.SH_Menu_SubMenuPopupDelay) == 0
                for menu in menus
            ), "즉시 전환 스타일이 실제 메뉴에 적용되지 않았다"
            root.popup(QPoint(100, 100))
            _app().processEvents()
            for submenu in menus[1:3]:
                QTest.mouseMove(root, root.actionGeometry(submenu.menuAction()).center())
                _app().processEvents()
                assert submenu.isVisible(), "다른 옵션의 다음 창이 hover 즉시 열리지 않았다"
            root.close()
    finally:
        overlay.QMenu = saved_menu


def test_classic_meter_visual_tokens_and_square_surface(_tmp: Path) -> None:
    """v0.3 계기판의 고대비 색과 평평한 사각 표면을 기본 디자인으로 고정한다."""
    dark = overlay.THEMES["dark"]
    assert dark["surface_glass"] == "#0D0E13"
    assert dark["text_primary"] == "#E8EAF2"
    assert dark["tint"] == "#2BD9E5"
    assert (dark["success"], dark["warning"], dark["destructive"]) == (
        "#3BE06A", "#FFC53D", "#FF5F6D",
    )
    assert overlay.HINT == "오늘/누적 · S/M/L · ⋯ 메뉴"
    assert overlay.METER_H == 128

    with _window() as window:
        image = window.grab().toImage()
        corner = image.pixelColor(image.width() - 1, image.height() - 1)
        assert (corner.red(), corner.green(), corner.blue(), corner.alpha()) == (13, 14, 19, 236)


def test_command_launcher_fuzzy_search_opens_session(_tmp: Path) -> None:
    with _window() as window:
        window.status = _sessions()
        window._rebuild_rows()

        window._open_palette()
        _app().processEvents()
        assert window.palette_open and window._search_field.isVisible()
        assert window._search_field.accessibleName() == "세션 또는 명령 검색"
        assert overlay.search_score("nwr", "newer") > 0

        window._search_field.setText("nwr")
        assert window.palette_results[0]["target"] == "session:svc/newer", window.palette_results
        assert window._activate_palette()
        assert not window.palette_open
        assert window.panel == "sessions" and window.open_key == "svc/newer"


def test_light_reduced_effects_keep_same_render_path(_tmp: Path) -> None:
    with _window() as window:
        window._set_theme("light")
        window.reduce_transparency = window.reduce_motion = True
        window.rate = 100.0
        window._advance(0.1)

        assert window.colors == overlay.THEMES["light"]
        assert window.gauge == overlay.gauge_target(window.rate, window.full_scale)
        assert window.peak == window.pulse == 0.0
        assert not window.grab().isNull()


def test_animation_timer_slows_when_surface_is_idle(_tmp: Path) -> None:
    with _window() as window:
        window.rate = window.gauge = window.pulse = 0.0
        window._tick()
        assert window._timer.interval() == 250

        window.rate = 10.0
        window._tick()
        assert window._timer.interval() == window._active_interval


def test_mini_meter_keeps_rate_unit_without_attention_shortcut(_tmp: Path) -> None:
    with _window() as window:
        window.status = _sessions()
        window.mini = True
        window.rate = 12_345.0
        window.gauge = 0.5
        window.resize(*window._size())
        image = window.grab().toImage()

        gauge_x = int(6 + overlay.MINI_RATE_W + 6)
        gauge = image.pixelColor(gauge_x, image.height() // 2)
        assert gauge.green() > gauge.red(), (gauge.red(), gauge.green(), gauge.blue())
        font = QFont(window._data_font)
        font.setBold(True)
        font.setPointSizeF(9.5)
        metrics = QFontMetricsF(font)
        for rate in (12_345, 999_999, 10**18):
            caption = overlay.mini_rate_caption(rate)
            assert caption.endswith("/s") and len(caption) <= 6, caption
            available = overlay.MINI_RATE_W - 8
            assert metrics.horizontalAdvance(caption) <= available, (
                caption, metrics.horizontalAdvance(caption), available,
            )
        assert "attention" not in window._hit


def test_manual_sync_does_not_block_gui_thread(_tmp: Path) -> None:
    class _OnlineBoard(_Board):
        online = True
        called = False

        def sync(self, _status: Dict[str, Any], force: bool = False) -> Dict[str, Any]:
            time.sleep(0.08)
            self.called = force
            return {"status": ""}

    with _window() as window:
        board = _OnlineBoard()
        window.board = board
        started = time.monotonic()
        window._sync_now()
        assert time.monotonic() - started < 0.04
        assert window._sync_busy

        deadline = time.monotonic() + 1.0
        while window._sync_busy and time.monotonic() < deadline:
            _app().processEvents()
            time.sleep(0.001)
        assert board.called and not window._sync_busy
        assert window._feedback == "동기화 완료"


def test_state_read_error_keeps_last_good_screen(_tmp: Path) -> None:
    class _BrokenMeter:
        read_only = True

        def reload(self) -> None:
            raise OSError("temporary read failure")

    with _window() as window:
        last_good = _sessions(live=True)
        window.status = last_good
        window.meter = _BrokenMeter()

        window._refresh_state()

        assert window.status is last_good
        assert window.state_error.startswith("상태 읽기 실패")


def test_close_button_hides_without_quitting_application(_tmp: Path) -> None:
    """×는 오버레이만 숨기고 측정 프로세스의 QApplication은 살려 둔다."""

    class _QuitGuard:
        called = False

        @classmethod
        def quit(cls) -> None:
            cls.called = True

    with _window() as window:
        real_application = overlay.QApplication
        try:
            overlay.QApplication = _QuitGuard
            window._hit = {"close": (0.0, 0.0, 24.0, 24.0)}
            assert window._click(QPoint(12, 12))
        finally:
            overlay.QApplication = real_application

        assert window.isHidden()
        assert not _QuitGuard.called, "×가 QApplication.quit()을 호출했다"


def _answer_dialog(window: MeterWindow, button: QMessageBox.StandardButton) -> bool:
    """동기/비동기 QMessageBox 모두 실제 표준 버튼으로 응답한다."""
    app = _app()
    answered: List[bool] = []
    timer = QTimer()
    timer.setInterval(1)

    def answer() -> None:
        dialogs = [
            widget for widget in app.topLevelWidgets()
            if isinstance(widget, QMessageBox) and widget.isVisible()
        ]
        if not dialogs:
            return
        timer.stop()
        dialog = dialogs[-1]
        target = dialog.button(button)
        if target is not None:
            target.click()
        else:
            dialog.done(button.value)
        answered.append(True)

    timer.timeout.connect(answer)
    timer.start()
    window._reset_stats()
    deadline = time.monotonic() + 0.1
    while timer.isActive() and time.monotonic() < deadline:
        app.processEvents()
    timer.stop()
    return bool(answered)


def test_reset_stats_waits_for_explicit_confirmation(_tmp: Path) -> None:
    meter = _Meter()
    with _window(meter) as window:
        assert _answer_dialog(window, QMessageBox.StandardButton.No)
        assert meter.reset_calls == 0, "취소했는데 통계가 초기화됐다"

        assert _answer_dialog(window, QMessageBox.StandardButton.Yes)
        assert meter.reset_calls == 1


def test_user_can_enter_model_prices_from_overlay(_tmp: Path) -> None:
    from tokenmeter import pricing

    class _Dialogs:
        @staticmethod
        def getItem(*_args: Any) -> tuple[str, bool]:
            return "gpt-5.6-sol", True

        @staticmethod
        def getText(*_args: Any) -> tuple[str, bool]:
            return "4, 0.4, 4, 24", True

    saved_dialog = overlay.QInputDialog
    saved_path = pricing.USER_PRICES
    saved_cache = pricing._OVER, pricing._OVER_MTIME
    try:
        overlay.QInputDialog = _Dialogs
        pricing.USER_PRICES = _tmp / "prices.json"
        with _window() as window:
            window.status = {"models": {"gpt-5.6-sol": {}}}
            window._edit_price()
            saved = json.loads(pricing.USER_PRICES.read_text(encoding="utf-8"))
            assert saved["gpt-5.6-sol"] == {
                "input": 4.0, "cache_read": 0.4, "cache_write": 4.0, "output": 24.0,
            }
            assert "다음 측정부터 적용" in window._feedback
    finally:
        overlay.QInputDialog = saved_dialog
        pricing.USER_PRICES = saved_path
        pricing._OVER, pricing._OVER_MTIME = saved_cache


def test_resize_clamps_window_inside_available_geometry(_tmp: Path) -> None:
    with _window() as window:
        area = _app().primaryScreen().availableGeometry()
        window.rows_on = False
        window.scale = 0.6
        window.resize(*window._size())
        window.move(area.right() - 4, area.bottom() - 4)

        window._set_scale(1.0)
        _app().processEvents()
        frame = window.frameGeometry()

        assert frame.left() >= area.left() and frame.top() >= area.top(), (frame, area)
        assert frame.right() <= area.right() and frame.bottom() <= area.bottom(), (frame, area)


def _double_click(window: MeterWindow, x: int, y: int) -> None:
    local = QPointF(x, y)
    global_pos = QPointF(window.mapToGlobal(QPoint(x, y)))
    event = QMouseEvent(
        QEvent.Type.MouseButtonDblClick,
        local,
        global_pos,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(window, event)


def test_double_click_does_not_change_panel_or_scope(_tmp: Path) -> None:
    with _window() as window:
        window.mini = False
        window.rows_on = True
        window.panel = "sessions"
        window.scope = "today"
        original = (window.panel, window.scope)

        _double_click(window, 5, 5)
        panel_y = int((overlay.PAD + overlay.METER_H + window._chip_h() + 5) * window.scale)
        _double_click(window, 5, panel_y)

        assert (window.panel, window.scope) == original


def test_quota_chip_resizes_even_when_panel_row_count_is_unchanged(_tmp: Path) -> None:
    with _window() as window:
        window.panel = "sessions"
        window.session_filter = "all"
        window.status = _sessions()
        window.quota = {"windows": [], "errors": {}}
        window._rebuild_rows()
        window.resize(*window._size())
        before_rows, before_height = window._row_count(), window.height()

        window.quota = {"windows": [{
            "source": "claude-code", "label": "5h", "used": 0.25, "status": "ok",
        }], "errors": {}}
        window._rebuild_rows()

        assert window._row_count() == before_rows
        assert window.height() == window._size()[1] == before_height + int(CHIP_H * window.scale)


def test_projects_show_only_measured_tokens_in_latest_activity_order(_tmp: Path) -> None:
    with _window() as window:
        window.status = {"projects": {
            "old": {"last_seen": 100.0, "totals": {"output_tokens": 20}},
            "shop/old": {"last_seen": 150.0, "totals": {"output_tokens": 5}},
            "new": {"last_seen": 200.0, "totals": {
                "input_tokens": 1, "cache_read": 2, "cache_write": 3, "output_tokens": 4,
            }},
            "cost-only": {"last_seen": 300.0, "totals": {"cost_usd": 99.0}},
        }}
        window.panel = "projects"
        rows, note = window._build_rows()

        assert rows == [("new", 10, 200.0), ("shop/old", 25, 150.0)]
        assert "프로젝트 2개" in note and "측정 35 토큰" in note
        window._rebuild_rows()
        assert not window.grab().isNull()


def test_wheel_resizes_window_except_over_a_list_row(_tmp: Path) -> None:
    with _window() as window:
        window.status = _sessions()
        window._rebuild_rows()
        window.grab()
        before = window.scale
        point = QPointF(4, 4)
        event = QWheelEvent(
            point, QPointF(window.mapToGlobal(QPoint(4, 4))), QPoint(), QPoint(0, 120),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.ScrollUpdate, False,
        )
        QApplication.sendEvent(window, event)
        assert window.scale > before

        window.scroll = 1
        row = window._hit["row:0"]
        row_point = QPointF(row[0] + 2, row[1] + 2)
        event = QWheelEvent(
            row_point, QPointF(window.mapToGlobal(row_point.toPoint())), QPoint(), QPoint(0, 120),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.ScrollUpdate, False,
        )
        scale = window.scale
        QApplication.sendEvent(window, event)
        assert window.scale == scale and window.scroll == 0


def test_quota_row_sets_and_persists_representative_window(_tmp: Path) -> None:
    with _window() as window:
        now = time.time()
        window.quota = {"updated_at": now, "errors": {}, "windows": [
            {"source": "claude-code", "title": "Claude Code", "plan": "subscription",
             "kind": "session", "label": "5h", "used": 0.20, "resets_at": now + 3600,
             "period_seconds": 5 * 3600, "status": "ok"},
            {"source": "claude-code", "title": "Claude Code", "plan": "subscription",
             "kind": "weekly", "label": "주간", "used": 0.30, "resets_at": now + 86400,
             "period_seconds": 7 * 86400, "status": "ok"},
            {"source": "codex", "title": "Codex", "plan": "subscription",
             "kind": "weekly", "label": "주간", "used": 0.10, "resets_at": now + 86400,
             "period_seconds": 7 * 86400, "status": "unavailable"},
        ]}
        window.panel = "quota"
        assert "주간" in window._quota_marks()[0][0], "CC 기본 대표는 주간이다"

        saved: List[Dict[str, Any]] = []
        original = overlay._save_prefs
        overlay._save_prefs = lambda data: saved.append(data)
        try:
            window._set_quota_representative(0)
        finally:
            overlay._save_prefs = original

        key = "claude-code:session:5h"
        assert window.quota_representatives == {"claude-code": key}
        assert "5h" in window._quota_marks()[0][0]
        assert saved and saved[-1]["quota_representatives"] == {"claude-code": key}
        window.grab()
        caption = window._controls["row:0"].accessibleName()
        assert all(text in caption for text in ("사용 20%", "페이스 여유", "리셋", "대표 구독"))
        assert "row:2" not in window._hit
        window._set_quota_representative(2)
        assert window.quota_representatives == {"claude-code": key}


def _metrics(window: MeterWindow, size: float, bold: bool = False,
             mono: bool = False) -> QFontMetricsF:
    """paintEvent 밖에서 `_f` 와 같은 폰트를 만든다 (`_f` 는 살아 있는 QPainter 를 쓴다)."""
    font = QFont(window._data_font if mono else window._ui_font)
    font.setBold(bold)
    font.setPointSizeF(max(6.0, size * window.scale))
    return QFontMetricsF(font)


def _quota_windows() -> Dict[str, Any]:
    now = time.time()
    return {"updated_at": now, "errors": {}, "windows": [
        {"source": "claude-code", "title": "Claude Code", "plan": "subscription",
         "kind": "weekly", "label": "Fable 주간", "used": 0.14, "resets_at": now + 86400,
         "period_seconds": 7 * 86400, "status": "ok"},
        {"source": "codex", "title": "Codex", "plan": "subscription",
         "kind": "weekly", "label": "GPT-5.3-high 주간", "used": 0.17,
         "resets_at": now + 86400, "period_seconds": 7 * 86400, "status": "ok"},
    ]}


def test_simple_mode_keeps_only_the_meter(_tmp: Path) -> None:
    """S 는 출력 속도와 게이지만 남긴다."""
    with _window() as window:
        window.status = _sessions()
        window.quota = _quota_windows()
        window._rebuild_rows()

        window._set_mode("M")
        medium = window.height()
        assert window._chip_h() == CHIP_H and window._quota_marks()

        window._set_mode("S")
        window.grab()
        assert window._mode() == "S"
        assert window._chip_h() == 0.0, "S 에는 한도 칩이 없다"
        assert window.height() < medium
        assert window.height() == int((PAD * 2 + overlay.METER_H_S + FOOT_H) * window.scale)
        assert "attention" not in window._hit
        assert not any(name.startswith("panel:") for name in window._hit)


def test_output_speed_bezel_fills_available_width(_tmp: Path) -> None:
    with _window() as window:
        window.status = _sessions()
        for mode in ("S", "M", "L"):
            window._set_mode(mode)
            image = window.grab().toImage()
            s = window.scale
            edge = image.pixelColor(
                image.width() - int(PAD * s) - 2,
                int((PAD + 27) * s) + 1,
            )
            assert edge.blue() > edge.red(), f"{mode} 출력 속도 베젤이 오른쪽 끝까지 안 찼다"


def test_mode_buttons_do_not_share_hit_areas(_tmp: Path) -> None:
    """MODE_BTN < MIN_HIT 이면 _sync_controls 가 히트박스를 키워 이웃과 겹친다."""
    assert overlay.MODE_BTN >= overlay.MIN_HIT
    with _window() as window:
        window.grab()
        _app().processEvents()
        names = ["mode:S", "mode:M", "mode:L", "menu", "close"]
        rects = [window._controls[name].geometry() for name in names]
        for (left_name, left), (right_name, right) in zip(
            zip(names, rects), zip(names[1:], rects[1:])
        ):
            assert left.right() < right.left(), f"{left_name} 와 {right_name} 가 겹친다"


def test_table_cells_never_hard_clip(_tmp: Path) -> None:
    """모든 표 셀은 칸을 넘으면 '…' 로 줄어든다 — 잘린 글자는 다른 값으로 읽힌다."""
    with _window() as window:
        window.status = _sessions()
        window.quota = _quota_windows()
        window.session_filter = "all"
        for mode in ("M", "L"):
            window._set_mode(mode)
            for panel in window._visible_panels():
                window.panel = panel
                window._rebuild_rows()
                assert not window.grab().isNull(), f"{mode}/{panel}"

        # 긴 라벨이 들어와도 elide 가 걸려 폭 안에서 끝난다
        metrics = _metrics(window, 8.5)
        long_label = "GPT-5.3-high 주간"
        shown = metrics.elidedText(long_label, Qt.TextElideMode.ElideRight, 40.0)
        assert shown != long_label and metrics.horizontalAdvance(shown) <= 40.0


def test_wide_session_table_fits_every_column(_tmp: Path) -> None:
    """L 세션 표 7칸이 실제 폭 안에 들어오는지 — 컨텍스트가 시각 칸을 먹으면 안 된다."""
    assert len(overlay.SESSION_COLS_WIDE) == len(overlay.SESSION_HEAD_WIDE) == 7
    assert len(overlay.SESSION_COLS) == len(overlay.SESSION_HEAD) == 5
    for columns in (overlay.SESSION_COLS, overlay.SESSION_COLS_WIDE):
        for (start, end, _right, _size), (next_start, *_rest) in zip(columns, columns[1:]):
            assert end <= next_start, "칸이 서로 겹친다"
        assert columns[0][0] == 0.0 and columns[-1][1] == 1.0

    with _window() as window:
        window._set_mode("L")
        window.status = _sessions()
        window.session_filter = "all"
        window._rebuild_rows()
        window.grab()
        # 칸 폭을 픽셀로 못 박으면 폰트가 다른 플랫폼(CI 의 리눅스)에서 깨진다 — 한글이
        # 섞인 '100% · 높음' 은 mono 폰트마다 폭이 크게 다르다. 어디서나 지켜야 하는 건
        # "하드 클립되지 않는다"와 "값이 달라 보이는 칸은 줄지 않는다" 두 가지다.
        # 값은 실제로 나올 수 있는 최댓값이다: 세션 누적은 억 단위까지 관측되고(`538.6M`),
        # 세션 tok/s 는 게이지 만배율(3000)을 넘지 않아 `3.0k/s` 가 상한이다.
        # 프로젝트·모델은 510px 에 7칸이라 어차피 줄어드는 칸이라 must_fit 이 아니다.
        cases = [
            (overlay.SESSION_COLS_WIDE, "L", {
                0: ("확인", 8.0, False, True), 1: ("acme/web-client", 9.0, False, False),
                2: ("gpt-5.6-sol · med", 8.0, True, False), 3: ("3.0k/s", 8.5, True, True),
                4: ("538.6M", 8.0, True, True), 5: ("100% · 높음", 8.0, True, False),
                6: ("08-16 00:11", 7.5, True, True),
            }),
            (overlay.SESSION_COLS, "M", {
                0: ("확인", 8.0, False, True), 1: ("acme/web-client", 9.0, False, False),
                2: ("3.0k/s", 8.5, True, True), 3: ("538.6M", 8.0, True, True),
                4: ("100% · 높음", 7.5, True, False),
            }),
        ]
        for columns, mode, worst in cases:
            window._set_mode(mode)
            width = window.width() - 2 * PAD * window.scale
            for index, (text, size, mono, must_fit) in worst.items():
                start, end, _right, _size = columns[index]
                room = (end - start) * width - overlay.CELL_PAD * window.scale
                metrics = _metrics(window, size, True, mono=mono)
                shown = metrics.elidedText(text, Qt.TextElideMode.ElideRight, room)
                assert shown, f"{mode}/{index} {text!r} 가 {room:.0f}px 에서 통째로 사라진다"
                assert metrics.horizontalAdvance(shown) <= room + 0.5, (
                    f"{mode}/{index} {text!r} 가 {room:.0f}px 를 넘쳐 잘린다"
                )
                if must_fit:  # 줄면 값이 달라 보이는 칸 — '…' 가 나오면 안 된다
                    assert shown == text, (
                        f"{mode}/{index} {text!r} 가 {room:.0f}px 에서 줄었다: {shown!r}"
                    )


def test_graph_peak_label_stays_inside_the_plot(_tmp: Path) -> None:
    """첫 막대가 최고점이면 라벨이 패딩 밖으로 나가 '$5.82' 가 '5.82' 로 잘렸다."""
    with _window() as window:
        left, width = 12.0, 300.0
        label_w = _metrics(window, 7, mono=True).horizontalAdvance("$5.82")
        for bar_x in (left, left + width - 10.0):
            placed = overlay._clamp(bar_x + (10.0 - label_w) / 2.0, left, left + width - label_w)
            assert left <= placed <= left + width - label_w, bar_x


def test_project_name_is_the_same_in_sessions_and_projects(_tmp: Path) -> None:
    """집계 키와 표시 이름이 같은 규칙을 쓴다 — 두 패널이 다른 이름을 보이면 안 된다."""
    now = time.time()
    cwd = "/Users/nilk/dev/oct7/token-pet"
    key = project_key(cwd)
    assert key == "oct7/token-pet"
    with _window() as window:
        window.status = {
            "sessions": {"svc/a": {"project": key, "cwd": cwd, "last_seen": now,
                                   "totals": {"output_tokens": 5}}},
            "projects": {key: {"last_seen": now, "totals": {"output_tokens": 5}},
                         "(unknown)": {"last_seen": now - 1, "totals": {"output_tokens": 3}}},
            "live": [], "live_count": 0,
        }
        window.session_filter = "all"
        window.panel = "sessions"
        session_rows, _note = window._build_rows()
        window.panel = "projects"
        project_rows, _note = window._build_rows()

        assert session_rows[0][2] == key
        assert project_rows[0][0] == key, project_rows
        assert project_rows[1][0] == UNKNOWN_PROJECT, "(unknown) 은 사람 말로 보여준다"


def test_live_session_uses_current_cwd_and_url_provider(_tmp: Path) -> None:
    """저장된 옛 프로젝트/프로토콜보다 현재 cwd/호출 URL이 화면의 진실이다."""
    now = time.time()
    with _window() as window:
        window.status = {
            "sessions": {"claude-code/s": {
                "project": "INF", "model": "deepseek-v4-flash", "vendor": "anthropic",
                "endpoint": "https://api.deepseek.com/anthropic", "last_seen": now,
                "totals": {"output_tokens": 1},
            }},
            "live": [{
                "service": "claude-code", "session_id": "s",
                "cwd": "/Users/nilk/dev/BROZ_Projects/INF/infmap",
                "attention": "working", "attention_at": now,
            }],
            "live_count": 1,
        }
        window.session_filter = "live"
        row = window._session_rows()[0][0]
        assert row[2] == "INF/infmap"
        assert row[6] == "api.deepseek.com"

        window.status["rate"] = {
            "h": rate_slot(now),
            "m": {"anthropic/deepseek-v4-flash": [100, 2.0, 1]},
        }
        assert window._rate_view().rows[0].vendor == "api.deepseek.com"


def test_saved_panel_falls_back_when_the_mode_hides_it(_tmp: Path) -> None:
    """L 에서 '일별' 을 보다 M 으로 접으면 탭 줄과 표가 어긋나면 안 된다."""
    with _window() as window:
        window.status = _sessions()
        window._set_mode("L")
        window._set_panel("days")
        assert window.panel == "days"

        window._set_mode("M")
        assert window.panel in window._visible_panels()
        assert window.panel == "sessions"

        # 반대로 M 에서 '일별' 을 고르면 막다른 길 대신 상세가 열린다
        window._set_panel("days")
        assert window.panel == "days" and window.expanded
        assert "days" in window._visible_panels()
        targets = {item["target"] for item in window._palette_catalog()}
        assert {"panel:rates", "panel:days"} <= targets


def main() -> int:
    tests = [(name, fn) for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    failed: List[str] = []
    print(f"TokenMeter UI/UX 회귀 검증 — {len(tests)}개\n")
    for name, test in tests:
        with tempfile.TemporaryDirectory(prefix="tokenmeter-ui-test-") as tmp:
            try:
                test(Path(tmp))
            except Exception:
                failed.append(name)
                print(f"  ✗ {name}")
                traceback.print_exc()
            else:
                print(f"  ✓ {name}")
    print()
    if failed:
        print(f"실패 {len(failed)}개: {', '.join(failed)}")
        return 1
    print(f"전부 통과 ({len(tests)}개)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
