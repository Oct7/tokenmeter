"""프로바이더 한도(잔여). 측정(로컬 토큰)과 별개로, 자격 증명이 있으면 네트워크로 읽는다."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

TTL = 180.0
WARN, HOT = 0.70, 0.90
GetJson = Callable[..., Dict[str, Any]]

CLAUDE_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_URL = "https://chatgpt.com/backend-api/wham/usage"
GROK_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
SHORT = {"claude-code": "CC", "codex": "CDX", "grok": "GRK"}
TITLES = {"claude-code": "Claude Code", "codex": "Codex", "grok": "Grok"}


class QuotaError(Exception):
    pass


class QuotaSkip(Exception):
    pass


def quota_path() -> Path:
    from .config import DATA_DIR

    return DATA_DIR / "quota.json"


def load() -> Dict[str, Any]:
    try:
        data = json.loads(quota_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"updated_at": 0.0, "windows": [], "errors": {}}
    if not isinstance(data, dict):
        return {"updated_at": 0.0, "windows": [], "errors": {}}
    data.setdefault("windows", [])
    data.setdefault("errors", {})
    return data


def save(snap: Dict[str, Any]) -> None:
    from .config import ensure_dirs

    ensure_dirs()
    path = quota_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def public(snap: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "updated_at": snap.get("updated_at") or 0.0,
        "windows": list(snap.get("windows") or []),
        "errors": dict(snap.get("errors") or {}),
    }


def reset_caption(ts: Any, now: float) -> str:
    try:
        when = float(ts or 0)
    except (TypeError, ValueError):
        return ""
    if when <= 0:
        return ""
    sec = int(when - now)
    if sec <= 0:
        return "곧"
    if sec < 60:
        return f"{sec}초"
    if sec < 3600:
        return f"{sec // 60}분"
    if sec < 86400:
        return f"{sec // 3600}시간"
    return f"{sec // 86400}일"


def window_key(row: Dict[str, Any]) -> str:
    """설정에 저장할 한도 창의 exact key. label이 scoped 창을 구분한다."""
    return ":".join(str(row.get(name) or "") for name in ("source", "kind", "label"))


def can_represent(row: Dict[str, Any]) -> bool:
    """상단 대표 칩으로 실제 표시할 값이 있는 한도 창인가."""
    return bool(row.get("source")) and row.get("status") != "unavailable" and (
        row.get("used") is not None or row.get("remaining_usd") is not None
    )


def representative_windows(
    windows: List[Dict[str, Any]],
    preferences: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """프로바이더별 대표 창. CC는 기본 주간, 나머지는 기존 첫 창을 유지한다."""
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in windows:
        source = str(row.get("source") or "")
        if can_represent(row):
            grouped.setdefault(source, []).append(row)

    preferences = preferences if isinstance(preferences, dict) else {}
    picked: List[Dict[str, Any]] = []
    for source, rows in grouped.items():
        preference = str(preferences.get(source) or "")
        row = next((item for item in rows if window_key(item) == preference), None)
        if row is None and preference:
            row = next((item for item in rows if item.get("kind") == preference), None)
        if row is None and source == "claude-code":
            row = next((item for item in rows if item.get("kind") == "weekly"), None)
        picked.append(row or rows[0])
    return picked


def chips(
    windows: List[Dict[str, Any]],
    preferences: Optional[Dict[str, str]] = None,
) -> List[Tuple[str, str]]:
    picked = {str(row.get("source") or ""): row
              for row in representative_windows(windows, preferences)}
    out: List[Tuple[str, str]] = []
    for src in ("claude-code", "codex", "grok"):
        row = picked.get(src)
        if not row:
            continue
        tag = SHORT[src]
        used = row.get("used")
        if used is not None:
            text = f"{tag} {row.get('label') or '?'} · {float(used) * 100:.0f}% 사용"
        elif row.get("remaining_usd") is not None:
            text = f"{tag} ${float(row['remaining_usd']):.1f}"
        else:
            continue
        status = str(row.get("status") or "ok")
        out.append((text, "underused" if status == "ok" and is_underused(row) else status))
    return out


def pace_gap(row: Dict[str, Any], now: Optional[float] = None) -> Optional[float]:
    """기간 경과율 - 사용률을 반환한다. 양수면 계획보다 덜 썼다."""
    if str(row.get("status") or "").lower() in {"stale", "unavailable", "expired"}:
        return None
    if row.get("plan") not in (None, "", "subscription"):
        return None
    try:
        used = float(row["used"])
        period = float(row["period_seconds"])
    except (KeyError, TypeError, ValueError):
        return None
    now = time.time() if now is None else now
    reset_value = row.get("resets_at")
    reset = float(reset_value) if isinstance(reset_value, (int, float)) else _ts(reset_value, now)
    if (not math.isfinite(used) or not math.isfinite(period) or period <= 0
            or reset is None or not math.isfinite(reset) or reset <= now):
        return None
    elapsed = max(0.0, min(1.0, 1.0 - (reset - now) / period))
    return elapsed - used


def is_underused(row: Dict[str, Any], now: Optional[float] = None) -> bool:
    gap = pace_gap(row, now)
    return gap is not None and gap > 0


def panel_rows(windows: List[Dict[str, Any]], now: Optional[float] = None) -> List[Tuple[str, str, float, str, str]]:
    now = time.time() if now is None else now
    rows = []
    for row in windows:
        used = row.get("used")
        try:
            ratio = float(used) if used is not None else -1.0
        except (TypeError, ValueError):
            ratio = -1.0
        reset = reset_caption(row.get("resets_at"), now)
        if row.get("remaining_usd") is not None and row.get("kind") == "credits":
            cap = row.get("cap_usd")
            label = f"${float(row['remaining_usd']):.2f}" + (f" / ${float(cap):.0f}" if cap else "")
        else:
            label = str(row.get("label") or "")
        rows.append((
            str(row.get("title") or TITLES.get(str(row.get("source")), row.get("source") or "")),
            label,
            ratio,
            reset,
            str(row.get("status") or "ok"),
        ))
    return rows


def parse_claude(data: Dict[str, Any], now: float) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    limits = data.get("limits")
    if isinstance(limits, list) and limits:
        for item in limits:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "")
            if kind == "session":
                rows.append(_window("claude-code", "session", "5h", item.get("percent"),
                                    item.get("resets_at"), now, percent=True,
                                    period_seconds=5 * 3600))
            elif kind == "weekly_all":
                rows.append(_window("claude-code", "weekly", "주간", item.get("percent"),
                                    item.get("resets_at"), now, percent=True,
                                    period_seconds=7 * 86400))
            elif kind == "weekly_scoped":
                name = str((((item.get("scope") or {}).get("model") or {}).get("display_name"))
                           or "모델")
                rows.append(_window("claude-code", "weekly_scoped", f"{name} 주",
                                    item.get("percent"), item.get("resets_at"), now, percent=True,
                                    period_seconds=7 * 86400))
    else:
        mapping = (
            ("five_hour", "session", "5h"),
            ("seven_day", "weekly", "주간"),
            ("seven_day_sonnet", "weekly_scoped", "Sonnet 주"),
            ("seven_day_opus", "weekly_scoped", "Opus 주"),
        )
        for key, kind, label in mapping:
            node = data.get(key)
            if not isinstance(node, dict):
                continue
            rows.append(_window("claude-code", kind, label, node.get("utilization"),
                                node.get("resets_at"), now, percent=True,
                                period_seconds=5 * 3600 if kind == "session" else 7 * 86400))
    extra = data.get("extra_usage")
    if isinstance(extra, dict) and extra.get("is_enabled"):
        cap = _money(extra.get("monthly_limit"), cents=True)
        used = _money(extra.get("used_credits"), cents=True)
        if cap is not None:
            remain = max(0.0, cap - (used or 0.0))
            rows.append(_window("claude-code", "credits", "추가사용",
                                (used or 0.0) / cap if cap else None,
                                extra.get("resets_at"), now, percent=False,
                                remaining_usd=remain, cap_usd=cap))
    return [row for row in rows if row]


def parse_codex(data: Dict[str, Any], now: float) -> List[Dict[str, Any]]:
    limit = data.get("rate_limit") if isinstance(data.get("rate_limit"), dict) else data
    rows = []
    primary = limit.get("primary_window") if isinstance(limit, dict) else None
    secondary = limit.get("secondary_window") if isinstance(limit, dict) else None
    if isinstance(primary, dict):
        rows.append(_window("codex", "session", _codex_label(primary, "5h"),
                            _used_field(primary), _reset_field(primary), now, percent=True,
                            period_seconds=_codex_period(primary, 5 * 3600)))
    if isinstance(secondary, dict):
        rows.append(_window("codex", "weekly", _codex_label(secondary, "주간"),
                            _used_field(secondary), _reset_field(secondary), now, percent=True,
                            period_seconds=_codex_period(secondary, 7 * 86400)))
    extras = data.get("additional_rate_limits")
    if not isinstance(extras, list) and isinstance(limit, dict):
        extras = limit.get("additional_rate_limits")
    if isinstance(extras, list):
        for item in extras:
            if not isinstance(item, dict):
                continue
            nested = item.get("rate_limit") if isinstance(item.get("rate_limit"), dict) else item
            win = nested.get("primary_window") if isinstance(nested.get("primary_window"), dict) else nested
            name = str(item.get("limit_name") or item.get("title") or item.get("name") or "추가")
            rows.append(_window("codex", "weekly_scoped", name, _used_field(win),
                                _reset_field(win), now, percent=True,
                                period_seconds=_codex_period(win, 7 * 86400)))
    credits = data.get("credits")
    if isinstance(credits, dict) and not credits.get("unlimited"):
        try:
            bal = float(credits.get("balance") or 0)
        except (TypeError, ValueError):
            bal = 0.0
        if credits.get("has_credits") or bal > 0:
            rows.append(_window("codex", "credits", "크레딧", None, None, now,
                                remaining_usd=bal))
    return [row for row in rows if row]


def parse_grok(data: Dict[str, Any], now: float) -> List[Dict[str, Any]]:
    cfg = data.get("config") if isinstance(data.get("config"), dict) else data
    used = cfg.get("creditUsagePercent") if isinstance(cfg, dict) else None
    percent = used is not None
    reset = None
    start = None
    period: Dict[str, Any] = {}
    if isinstance(cfg, dict):
        if isinstance(cfg.get("currentPeriod"), dict):
            period = cfg["currentPeriod"]
            reset = period.get("end")
            start = period.get("start")
        reset = reset or cfg.get("billingPeriodEnd")
        start = start or cfg.get("billingPeriodStart")
    if used is None:
        limit = _cents_val(data.get("monthlyLimit"))
        if limit is None and isinstance(cfg, dict):
            limit = _cents_val(cfg.get("monthlyLimit"))
        spent = _cents_val(((data.get("usage") or {}) if isinstance(data.get("usage"), dict) else {}).get("totalUsed"))
        if spent is None:
            spent = _cents_val(data.get("onDemandUsed"))
            limit = limit or _cents_val(data.get("onDemandCap"))
        if limit and limit > 0 and spent is not None:
            used = spent / limit
            percent = False
    cycle = data.get("billingCycle")
    if isinstance(cycle, dict):
        reset = reset or cycle.get("billingPeriodEnd")
        start = start or cycle.get("billingPeriodStart")
    if used is None:
        return []
    ptype = str(period.get("type") or ((cycle or {}).get("type") if isinstance(cycle, dict) else "")).upper()
    if "WEEKLY" in ptype:
        label = "주간"
    elif "MONTHLY" in ptype:
        label = "월간"
    else:
        label = "크레딧"
        when = _ts(reset, now)
        if when:
            days = when - now
            if 5.5 * 86400 < days < 9 * 86400:
                label = "주간"
            elif days >= 9 * 86400:
                label = "월간"
    reset_ts, start_ts = _ts(reset, now), _ts(start, now)
    span = reset_ts - start_ts if reset_ts is not None and start_ts is not None else None
    if span is None or span <= 0:
        # ponytail: 시작 시각이 없는 월간 응답은 30일로 본다. 제공자가 start를 주면 위의 실기간이 이긴다.
        span = 7 * 86400 if label == "주간" else 30 * 86400 if label == "월간" else None
    return [_window("grok", "credits", label, used, reset, now, percent=percent,
                    period_seconds=span)]


def refresh(
    force: bool = False,
    now: Optional[float] = None,
    get_json: Optional[GetJson] = None,
    homes: Optional[Dict[str, Path]] = None,
) -> Dict[str, Any]:
    now = time.time() if now is None else now
    prev = load()
    age = now - float(prev.get("updated_at") or 0)
    if not force and 0 <= age < TTL:
        return prev
    getter = get_json or _get_json
    paths = homes or _homes()
    found: Dict[str, List[Dict[str, Any]]] = {}
    errors: Dict[str, str] = {}
    probes = (
        ("claude-code", lambda: _fetch_claude(now, getter, paths)),
        ("codex", lambda: _fetch_codex(now, getter, paths)),
        ("grok", lambda: _fetch_grok(now, getter, paths)),
    )
    for name, fn in probes:
        try:
            found[name] = fn()
        except QuotaSkip as exc:
            errors[name] = str(exc)
        except Exception as exc:
            errors[name] = str(exc) or type(exc).__name__
            old = [dict(row, status="stale") for row in prev.get("windows") or []
                   if row.get("source") == name]
            if old:
                found[name] = old
    windows = []
    for name in ("claude-code", "codex", "grok"):
        windows.extend(found.get(name) or [])
    snap = {"updated_at": now, "windows": windows, "errors": errors}
    save(snap)
    return snap


def due(snap: Optional[Dict[str, Any]] = None, now: Optional[float] = None) -> bool:
    snap = load() if snap is None else snap
    now = time.time() if now is None else now
    age = now - float(snap.get("updated_at") or 0)
    return age < 0 or age >= TTL


# ── 내부 ──────────────────────────────────────────────────────────────────


def _homes() -> Dict[str, Path]:
    home = Path.home()
    claude_root = Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser() if os.environ.get("CLAUDE_CONFIG_DIR") else home / ".claude"
    return {
        "claude": claude_root / ".credentials.json",
        "codex": home / ".codex" / "auth.json",
        "grok": home / ".grok" / "auth.json",
    }


def _window(
    source: str,
    kind: str,
    label: str,
    used: Any,
    reset: Any,
    now: float,
    remaining_usd: Optional[float] = None,
    cap_usd: Optional[float] = None,
    percent: bool = False,
    period_seconds: Any = None,
) -> Dict[str, Any]:
    ratio = _ratio(used, percent=percent)
    return {
        "source": source,
        "title": TITLES[source],
        "plan": "subscription",
        "kind": kind,
        "label": label,
        "used": ratio,
        "remaining_usd": remaining_usd,
        "cap_usd": cap_usd,
        "resets_at": _ts(reset, now),
        "period_seconds": _positive_seconds(period_seconds),
        "status": _status(ratio),
        "note": "",
        "fetched_at": now,
    }


def _status(used: Optional[float]) -> str:
    if used is None:
        return "ok"
    if used >= HOT:
        return "exhausted"
    if used >= WARN:
        return "warn"
    return "ok"


def _ratio(value: Any, percent: bool = False) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if percent:
        num /= 100.0
    elif num > 1.0:
        num /= 100.0
    return max(0.0, min(1.0, num))


def _codex_label(node: Dict[str, Any], fallback: str) -> str:
    sec = node.get("limit_window_seconds")
    try:
        span = int(sec)
    except (TypeError, ValueError):
        return fallback
    if span <= 6 * 3600:
        return "5h"
    if span <= 2 * 86400:
        return "일간"
    if span <= 10 * 86400:
        return "주간"
    return "월간"


def _codex_period(node: Dict[str, Any], fallback: int) -> Optional[float]:
    return _positive_seconds(node.get("limit_window_seconds")) or float(fallback)


def _positive_seconds(value: Any) -> Optional[float]:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def _ts(value: Any, now: float) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        num = float(value)
        if num > 1e12:
            return num / 1000.0
        if num > 1e9:
            return num
        return now + num
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _money(value: Any, cents: bool = False) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num / 100.0 if cents else num


def _cents_val(value: Any) -> Optional[float]:
    if isinstance(value, dict):
        value = value.get("val")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _used_field(node: Dict[str, Any]) -> Any:
    for key in ("used_percent", "usedPercent", "utilization", "percent"):
        if node.get(key) is not None:
            return node.get(key)
    return None


def _reset_field(node: Dict[str, Any]) -> Any:
    for key in ("resets_at", "reset_at", "resetAt", "resetsAt"):
        if node.get(key) is not None:
            return node.get(key)
    for key in ("reset_after_seconds", "resets_in_seconds", "resetAfterSeconds"):
        if node.get(key) is not None:
            return node.get(key)
    return None


def _get_json(url: str, headers: Dict[str, str], timeout: float = 8.0) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise QuotaError(f"{exc.code}") from exc
    except urllib.error.URLError as exc:
        raise QuotaError(str(exc.reason or exc)) from exc
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise QuotaError("invalid json") from exc
    if not isinstance(data, dict):
        raise QuotaError("unexpected payload")
    return data


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QuotaSkip(f"{path.name} 없음") from exc
    return data if isinstance(data, dict) else {}


def _claude_token(homes: Dict[str, Path]) -> str:
    path = Path(homes["claude"])
    if path.is_file():
        token = _extract_claude(_read_json(path))
        if token:
            return token
        raise QuotaSkip("claude 자격 없음")
    if path != _homes()["claude"]:
        raise QuotaSkip("claude 자격 없음")
    token = _keychain_claude()
    if token:
        return token
    raise QuotaSkip("claude 자격 없음")


def _extract_claude(data: Dict[str, Any]) -> str:
    oauth = data.get("claudeAiOauth")
    if isinstance(oauth, dict) and oauth.get("accessToken"):
        return str(oauth["accessToken"])
    if data.get("accessToken"):
        return str(data["accessToken"])
    return ""


def _keychain_claude() -> str:
    if sys.platform != "darwin":
        return ""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.returncode != 0 or not out.stdout.strip():
        return ""
    try:
        return _extract_claude(json.loads(out.stdout))
    except json.JSONDecodeError:
        return ""


def _codex_auth(homes: Dict[str, Path]) -> Tuple[str, str]:
    data = _read_json(Path(homes["codex"]))
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    token = str(tokens.get("access_token") or "")
    if not token:
        raise QuotaSkip("codex 자격 없음")
    return token, str(tokens.get("account_id") or "")


def _grok_token(homes: Dict[str, Path]) -> str:
    data = _read_json(Path(homes["grok"]))
    rec: Dict[str, Any] = {}
    for key, item in data.items():
        if not isinstance(item, dict) or not item.get("key"):
            continue
        rec = item
        if "auth.x.ai" in str(key):
            break
    if not rec.get("key"):
        raise QuotaSkip("grok 자격 없음")
    exp = _ts(rec.get("expires_at"), time.time())
    if exp is not None and exp < time.time():
        raise QuotaSkip("grok 로그인 만료")
    return str(rec["key"])


def _fetch_claude(now: float, get_json: GetJson, homes: Dict[str, Path]) -> List[Dict[str, Any]]:
    token = _claude_token(homes)
    data = get_json(CLAUDE_URL, {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
    })
    return parse_claude(data, now)


def _fetch_codex(now: float, get_json: GetJson, homes: Dict[str, Path]) -> List[Dict[str, Any]]:
    token, account = _codex_auth(homes)
    headers = {"Authorization": f"Bearer {token}"}
    if account:
        headers["ChatGPT-Account-Id"] = account
    return parse_codex(get_json(CODEX_URL, headers), now)


def _fetch_grok(now: float, get_json: GetJson, homes: Dict[str, Path]) -> List[Dict[str, Any]]:
    token = _grok_token(homes)
    return parse_grok(get_json(GROK_URL, {
        "Authorization": f"Bearer {token}",
        "x-xai-token-auth": "xai-grok-cli",
        "Accept": "application/json",
    }), now)
