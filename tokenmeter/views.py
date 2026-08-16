"""오버레이가 그릴 문구·필터 (Application).

Qt 를 모른다. 상태 dict 와 숫자만 받아 화면에 쓸 문자열을 만든다.
"""

from __future__ import annotations

from pathlib import Path
import time
from typing import Any, Dict, List

CHECK_REASONS = {
    "PermissionRequest": "권한",
    "permission.asked": "권한",
    "permission.v2.asked": "권한",
    "question.asked": "질문",
    "question.v2.asked": "질문",
    "Stop": "중지",
    "session.idle": "중지",
    "Notification": "알림",
}

SESSION_FILTERS = ("live", "archive", "all")
SESSION_FILTER_TITLES = {"live": "LIVE", "archive": "ARCHIVE", "all": "ALL"}
SESSION_ARCHIVE_SECONDS = 3600.0
UNKNOWN_PROJECT = "폴더 미상"
LEGACY_UNKNOWN = "(unknown)"


def check_reason(event: Any) -> str:
    return CHECK_REASONS.get(str(event or ""), "")


def project_key(cwd: Any) -> str:
    """에이전트를 실행한 폴더 → 프로젝트 키 '상위/leaf'.

    leaf 이름만 쓰면 서로 다른 저장소의 같은 폴더명(`api`, `web`)이 한 프로젝트로
    합쳐진다. 집계(state)와 표시(project_label)가 같은 규칙을 써야 세션 목록과
    프로젝트 패널의 이름이 어긋나지 않는다.
    """
    text = str(cwd or "").strip()
    if not text:
        return ""
    try:
        path = Path(text)
    except (TypeError, ValueError):
        return ""
    leaf = path.name
    if not leaf:
        return ""
    parent = path.parent.name
    return f"{parent}/{leaf}" if parent and parent not in {".", path.anchor} else leaf


def project_label(project: Any, cwd: Any = "") -> str:
    """화면에 쓸 프로젝트 이름. cwd 를 알면 그걸로, 모르면 저장된 키를 그대로 쓴다."""
    name = project_key(cwd) or str(project or "").strip()
    return UNKNOWN_PROJECT if name in ("", LEGACY_UNKNOWN) else name


def money_caption(approx: bool, amount: Any) -> str:
    try:
        value = float(amount or 0)
    except (TypeError, ValueError):
        value = 0.0
    if value != value or abs(value) == float("inf"):
        value = 0.0
    text = f"${value:,.2f}"
    return f"환산 {text}" if approx else text


def cost_caption(approx: bool, amount: Any) -> str:
    """오버레이 상단용 비용 문구. 구독분은 청구액으로 오해하지 않게 명시한다."""
    text = money_caption(False, amount)
    return f"API 환산 {text}" if approx else f"비용 {text}"


def ctx_caption(ratio: float, has_window: bool) -> str:
    if not has_window:
        return "창?"
    if ratio >= 0.90:
        return "높음"
    return f"{max(0.0, ratio) * 100:.0f}%"


def ctx_status_caption(ratio: float, has_window: bool) -> str:
    """정확한 점유율과 미상 상태를 모두 보존하는 세션 표 문구."""
    if not has_window:
        return "미상"
    pct = f"{max(0.0, ratio) * 100:.0f}%"
    return f"{pct} · 높음" if ratio >= 0.90 else pct


def health_note(status: Dict[str, Any], now: float) -> str:
    live = int(status.get("live_count") or 0)
    sessions = status.get("sessions")
    has_sessions = isinstance(sessions, dict) and bool(sessions)
    if live <= 0 and not has_sessions:
        return "첫 세션 대기 중 · 에이전트를 재시작하세요"
    try:
        updated = float(status.get("updated_at") or 0)
    except (TypeError, ValueError):
        updated = 0.0
    if live > 0 and updated > 0 and now - updated > 120:
        return "측정이 멈춤 · tokenmeter doctor"
    return ""


def header_attention(counts: Dict[str, Any], project: str) -> str:
    try:
        n = int(counts.get("check") or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return ""
    name = str(project or "").strip()
    return f"확인 {n} · {name}" if name else f"확인 {n}"


def filter_sessions(
    rows: List[Dict[str, Any]], mode: str, now: float | None = None,
) -> List[Dict[str, Any]]:
    """세션을 최근 활동 1시간 기준 LIVE / ARCHIVE 두 그룹으로 나눈다."""
    if mode == "all":
        return list(rows)
    now = time.time() if now is None else now

    def recent(row: Dict[str, Any]) -> bool:
        if row.get("live"):
            return True  # 장시간 실행 중인 실제 라이브 세션은 활동 간격과 무관하게 LIVE다.
        try:
            activity = float(row.get("activity_at") or row.get("last_seen") or 0)
        except (TypeError, ValueError):
            activity = 0.0
        return activity > 0 and now - activity < SESSION_ARCHIVE_SECONDS

    if mode == "archive":
        return [row for row in rows if not recent(row)]
    if mode == "live":
        return [row for row in rows if recent(row)]
    return list(rows)


def wait_caption(now: float, since: Any) -> str:
    try:
        start = float(since or 0)
    except (TypeError, ValueError):
        return ""
    if start <= 0:
        return ""
    sec = max(0, int(now - start))
    if sec < 60:
        return f"{sec}초"
    return f"{sec // 60}분 {sec % 60}초"
