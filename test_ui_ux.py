#!/usr/bin/env python3
"""TokenMeter UI/UX 회귀 검증 — 프레임워크 없이 assert 만 쓴다.

    QT_QPA_PLATFORM=offscreen uv run python test_ui_ux.py

사용자 설정과 화면은 건드리지 않고 Qt의 offscreen 백엔드에서 실제 창을 검증한다.
"""

# ruff: noqa: E402 — Qt 백엔드와 격리된 상태 경로를 import 전에 고정해야 한다.

from __future__ import annotations

import contextlib
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
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication, QMessageBox

from tokenmeter import overlay
from tokenmeter.config import Config
from tokenmeter.meter import Meter, TokenDelta, session_views
from tokenmeter.overlay import CARD_H, CHIP_H, MeterWindow
from tokenmeter.views import ctx_status_caption


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
        window._live_keys = {"svc/check", "svc/older", "svc/newer"}
        window.meter = _BrokenMeter()

        window._refresh_state()

        assert window.status is last_good
        assert window.state_error.startswith("상태 읽기 실패")
        assert window.card is None, "일시적인 읽기 실패를 세션 종료로 오인했다"


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


def test_end_card_resizes_even_when_session_row_count_is_unchanged(_tmp: Path) -> None:
    with _window() as window:
        window.panel = "sessions"
        window.session_filter = "all"
        window.status = _sessions(live=True)
        window._track_ended()
        window._rebuild_rows()
        window.resize(*window._size())
        before_rows, before_height = window._row_count(), window.height()

        window.status = _sessions(live=False)
        window._track_ended()
        window._rebuild_rows()

        assert window.card is not None
        assert window._row_count() == before_rows
        assert window.height() == window._size()[1] == before_height + int(CARD_H * window.scale)


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
