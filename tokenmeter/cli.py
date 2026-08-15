"""TokenMeter CLI (Presentation).

훅이 `sys.executable -m tokenmeter.cli daemon` 으로 데몬을 띄우기 때문에, 이 모듈이
무거운 서드파티에 의존하면 연동 전체가 조용히 죽는다. 그래서 출력까지 전부
표준 라이브러리(argparse + print)로만 만든다.

  install / uninstall  훅 설치·해제 (멱등)
  services / doctor    설정 점검 — 새 서비스를 붙일 때 여기서 검증한다
  daemon               워처 + 오버레이 (훅이 자동으로 띄우는 본체)
  status / start / stop / watch / overlay / reset
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import signal
import sys
import threading
import time
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from . import installer
from .adapter import check_adapter, init_adapter
from .config import (
    LOG_FILE,
    PID_FILE,
    TOKEN_FIELDS,
    USER_PRICES,
    Config,
    ServiceSpec,
    dig,
    ensure_dirs,
    load_config,
    load_toggle,
    save_toggle,
)
from .endpoints import classify
from .leaderboard import Leaderboard
from .meter import _float, Meter, session_views, sessions_today, tokens_of
from .pricing import PRICES, has_override, known, overrides, prices_for, set_price, unset_price
from .watcher import MultiWatcher, ServiceReader

PROG = "tokenmeter"
IDLE_TICK_SEC = 5.0  # 데몬 유휴 감시 주기
DOCTOR_JSON_FILES = 40  # format=json 은 파일 1개 = 레코드 1개라 표본을 넓게 잡는다


# ── 출력 헬퍼 ──────────────────────────────────────────────────────────────


def _w(text: Any) -> int:
    """한글이 섞인 표를 맞추기 위한 표시 폭(전각 = 2칸)."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in str(text))


def _pad(text: Any, width: int, right: bool = False) -> str:
    gap = " " * max(0, width - _w(text))
    return (gap + str(text)) if right else (str(text) + gap)


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]], right: Sequence[int] = ()) -> None:
    widths = [
        max([_w(h)] + [_w(r[i]) for r in rows]) for i, h in enumerate(headers)
    ]
    print("  " + "  ".join(_pad(h, w) for h, w in zip(headers, widths)))
    print("  " + "  ".join("─" * w for w in widths))
    for row in rows:
        cells = (_pad(c, w, i in right) for i, (c, w) in enumerate(zip(row, widths)))
        print(("  " + "  ".join(cells)).rstrip())


def _n(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _num(value: Any) -> str:
    return f"{_n(value):,}"


def _usd(value: Any) -> str:
    try:
        return f"${float(value):.4f}"
    except (TypeError, ValueError):
        return "$0.0000"


def _usd_scoped(totals: Dict[str, Any], approx: bool) -> str:
    """구독분이 섞여 있으면 '≈' — 이 금액은 청구서가 아니라 API 환산가다."""
    return ("≈" if approx else "") + _usd(totals.get("cost_usd"))


def _is_approx(state: Dict[str, Any]) -> bool:
    node = (state.get("plans") or {}).get("subscription")
    return bool(node) and tokens_of((node or {}).get("totals")) > 0


def _unknown_models(state: Dict[str, Any]) -> List[str]:
    """가격표에 없어 default 단가로 계산 중인 모델들."""
    return [name for name in (state.get("models") or {}) if not known(name)]


def _when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def _short(path: Any) -> str:
    """홈 디렉토리를 ~ 로 줄여 표에 넣는다."""
    text = str(path)
    home = str(Path.home())
    return "~" + text[len(home) :] if text.startswith(home) else text


STREAM_TOTALS = ("input_tokens", "cache_read", "cache_write", "output_tokens", "cost_usd", "calls")
PUBLIC_TOTALS = STREAM_TOTALS + ("cache_saved_usd",)


def totals_delta(before: Dict[str, Any], after: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    diff = {key: float(after.get(key) or 0) - float(before.get(key) or 0) for key in STREAM_TOTALS}
    if any(value < 0 for value in diff.values()):
        return None
    return {key: int(value) if key != "cost_usd" else round(value, 10)
            for key, value in diff.items() if value > 0}


def _public_totals(node: Any) -> Dict[str, Any]:
    raw = node if isinstance(node, dict) else {}
    return {
        key: round(_float(raw.get(key)), 10) if key in ("cost_usd", "cache_saved_usd")
        else int(_float(raw.get(key)))
        for key in PUBLIC_TOTALS
    }


def _public_group(node: Any, model: bool = False) -> Dict[str, Any]:
    raw = node if isinstance(node, dict) else {}
    out: Dict[str, Any] = {}
    for name, value in raw.items():
        if not isinstance(value, dict):
            continue
        entry: Dict[str, Any] = {"totals": _public_totals(value.get("totals"))}
        if "sessions" in value:
            entry["sessions"] = int(_float(value.get("sessions")))
        if "last_seen" in value:
            entry["last_seen"] = _float(value.get("last_seen"))
        if model and value.get("vendor"):
            entry["vendor"] = str(value["vendor"])
        out[str(name)] = entry
    return out


def _public_endpoints(node: Any) -> Dict[str, Any]:
    raw = node if isinstance(node, dict) else {}
    out: Dict[str, Any] = {}
    for endpoint, value in raw.items():
        if not isinstance(value, dict):
            continue
        label = classify(str(endpoint))
        entry = out.setdefault(label, {"totals": _public_totals({}), "sessions": 0, "last_seen": 0.0})
        totals = _public_totals(value.get("totals"))
        for key, amount in totals.items():
            entry["totals"][key] = (round(entry["totals"][key] + amount, 10)
                                    if key in ("cost_usd", "cache_saved_usd")
                                    else entry["totals"][key] + amount)
        entry["sessions"] += int(_float(value.get("sessions")))
        entry["last_seen"] = max(entry["last_seen"], _float(value.get("last_seen")))
    return out


def _public_days(node: Any) -> Dict[str, Any]:
    raw = node if isinstance(node, dict) else {}
    out: Dict[str, Any] = {}
    for name, value in raw.items():
        label = str(name)
        try:
            if date.fromisoformat(label).isoformat() != label:
                continue
        except ValueError:
            continue
        out[label] = _public_totals(value)
    return out


def public_snapshot(state: Dict[str, Any], record_type: str = "snapshot") -> Dict[str, Any]:
    sessions = [{
        "service": row["service"], "project": row["project"], "model": row["model"],
        "attention": row["attention"], "started_at": row["started_at"],
        "last_seen": row["last_seen"], "attention_at": row["attention_at"],
        "ctx": row["ctx"], "ctx_window": row["ctx_win"],
    } for row in session_views(state) if row["live"]]
    today = state.get("today") if isinstance(state.get("today"), dict) else {}
    total = state.get("total") if isinstance(state.get("total"), dict) else {}
    return {
        "schema_version": 1, "type": record_type, "timestamp": time.time(),
        "updated_at": _float(state.get("updated_at")),
        "today": {"date": str(today.get("date") or ""), "totals": _public_totals(today.get("totals"))},
        "total": {"started_at": _float(total.get("started_at")), "last_seen": _float(total.get("last_seen")),
                  "sessions": int(_float(total.get("sessions"))), "totals": _public_totals(total.get("totals"))},
        "days": _public_days(state.get("days")),
        "projects": _public_group(state.get("projects")), "services": _public_group(state.get("services")),
        "models": _public_group(state.get("models"), model=True), "vendors": _public_group(state.get("vendors")),
        "plans": _public_group(state.get("plans")), "endpoints": _public_endpoints(state.get("endpoints")),
        "sessions": sessions,
    }


MONEY_LABELS = {"api": "예상 사용액", "subscription": "API 환산 가치"}


def receipt_data(state: Dict[str, Any], key: Optional[str] = None) -> Optional[Dict[str, Any]]:
    sessions = state.get("sessions") if isinstance(state.get("sessions"), dict) else {}
    if key:
        rec = sessions.get(key)
        if not isinstance(rec, dict):
            return None
    else:
        rows = [row for row in sessions.values() if isinstance(row, dict)]
        if not rows:
            return None
        rec = max(rows, key=lambda row: _float(row.get("last_seen")))
    totals = _public_totals(rec.get("totals"))
    cost = max(0.0, _float(totals.get("cost_usd")))
    window = max(0.0, _float(rec.get("ctx_win")))
    started_at, last_seen = _float(rec.get("started_at")), _float(rec.get("last_seen"))
    plan = rec.get("plan") if isinstance(rec.get("plan"), str) else "unknown"
    return {
        "schema_version": 1, "type": "receipt",
        "project": rec.get("project") if isinstance(rec.get("project"), str) and rec.get("project") else "(unknown)",
        "service": rec.get("service") if isinstance(rec.get("service"), str) else "",
        "model": rec.get("model") if isinstance(rec.get("model"), str) else "",
        "effort": rec.get("effort") if isinstance(rec.get("effort"), str) else "",
        "plan": plan, "started_at": started_at, "last_seen": last_seen,
        "duration_seconds": max(0.0, last_seen - started_at),
        "totals": totals, "money_label": MONEY_LABELS.get(plan, "API 환산가"),
        "amount_usd": cost,
        "ctx_percent": round(100 * max(0.0, _float(rec.get("ctx"))) / window, 1) if window else None,
        "subagent_percent": round(100 * max(0.0, _float(rec.get("sub_cost"))) / cost, 1) if cost else 0.0,
    }


def format_receipt(data: Dict[str, Any], format_name: str) -> str:
    totals = data["totals"]
    minutes = int(data["duration_seconds"] // 60)
    identity = " · ".join(str(data[key]) for key in ("project", "service", "model") if data.get(key))
    amount = f"{data['money_label']} ${data['amount_usd']:.2f}"
    context = "-" if data["ctx_percent"] is None else f"{data['ctx_percent']:g}%"
    lines = [
        "TokenMeter 영수증", identity,
        f"{minutes}분 · 입력 {int(totals.get('input_tokens') or 0):,} · "
        f"캐시 읽기 {int(totals.get('cache_read') or 0):,} · "
        f"캐시 쓰기 {int(totals.get('cache_write') or 0):,} · "
        f"출력 {int(totals.get('output_tokens') or 0):,} · {int(totals.get('calls') or 0)} 호출",
        f"{amount} · 캐시 절감 ${float(totals.get('cache_saved_usd') or 0):.2f}",
        f"ctx {context} · 서브에이전트 {data['subagent_percent']:g}%",
    ]
    if format_name == "json":
        return json.dumps(data, ensure_ascii=False)
    if format_name == "markdown":
        return "\n".join(["### TokenMeter 영수증", f"- {identity}", f"- {lines[2]}",
                            f"- {lines[3]}", f"- {lines[4]}"])
    return "\n".join(lines)


# ── 데몬 상태 ──────────────────────────────────────────────────────────────


def _daemon_pid() -> int:
    """살아 있는 데몬의 pid. 없으면 0. (판정은 훅과 공유 — pid 재사용 오판 방지)"""
    from .hook import daemon_pid  # 표준 라이브러리만 쓰는 모듈이라 import 가 싸다

    return daemon_pid()


# ── 서비스 조회 ────────────────────────────────────────────────────────────


def _specs(config: Config, names: Optional[Sequence[str]]) -> List[ServiceSpec]:
    if not names:
        return list(config.services.values())
    out: List[ServiceSpec] = []
    for name in names:
        spec = config.get(name)
        if spec is None:
            print(f"⚠ 알 수 없는 서비스: {name}")
            continue
        out.append(spec)
    return out


def _scan_logs(spec: ServiceSpec, limit: int = 1) -> Tuple[List[Path], float, int]:
    """최근 로그 파일 limit 개 → (경로 목록, 가장 최근 mtime, 전체 개수).

    ponytail: roots 를 매번 전수 스캔한다(O(파일 수)). CLI 한 번 실행 비용이라
              충분하지만, 파일이 수만 개가 되면 디렉토리 mtime 캐시가 필요하다.
    """
    found: List[Tuple[float, Path]] = []
    count = 0
    for path in ServiceReader(spec, lambda _d: None).files():
        count += 1
        try:
            found.append((path.stat().st_mtime, path))
        except OSError:
            continue
    found.sort(key=lambda item: item[0], reverse=True)
    return [p for _m, p in found[:limit]], (found[0][0] if found else 0.0), count


def _iter_records(spec: ServiceSpec, path: Path, limit: int = 20000) -> Iterator[Any]:
    """doctor 용 원본 레코드 스트림 (파싱 실패 줄은 건너뛴다)."""
    if spec.format == "json":
        try:
            yield json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, json.JSONDecodeError):
            return
        return
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            for i, line in enumerate(fh):
                if i >= limit:
                    return
                text = line.strip()
                if not text:
                    continue
                try:
                    yield json.loads(text)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


# ── 명령: services ─────────────────────────────────────────────────────────


def _has_overlay() -> bool:
    """PyQt6 가 이 인터프리터에 있나. **import 하지 않는다** — 훅에 박히는 것도
    데몬이 쓰는 것도 결국 sys.executable 이라, 여기서 없으면 오버레이는 안 뜬다."""
    return importlib.util.find_spec("PyQt6") is not None


def _services_table(config: Config) -> None:
    installed = {row["name"]: row for row in installer.status(config)}
    rows: List[List[str]] = []
    for spec in config.services.values():
        row = installed.get(spec.name, {})
        roots = spec.existing_roots()
        _paths, mtime, count = _scan_logs(spec) if roots else ([], 0.0, 0)
        target = str(row.get("target", "none"))
        hook = "-" if target == "none" else f"{target} · {_short(row.get('path', ''))}"
        state = {"skip": "불필요", "missing": "미설치", "stale": "갱신 필요", "ok": "설치됨"}[
            installer.install_state(spec)
        ]
        rows.append(
            [
                spec.name,
                "예" if spec.enabled else "아니오",
                f"{len(roots)}/{len(spec.roots)}개" if spec.roots else "없음",
                f"{count:,}개" if roots else "-",
                _when(mtime),
                hook,
                state,
            ]
        )
    _table(
        ["서비스", "활성", "로그 경로", "로그 파일", "최근 로그", "훅 대상", "훅 설치"],
        rows,
        right=(3,),
    )
    # 설정 파일 경로(위 '훅 대상')가 아니라 **엔트리에 박히는 커맨드**를 보여준다.
    # venv 를 지웠을 때 정작 확인해야 하는 값이 이쪽이다.
    print()
    print(f"  훅 커맨드 : {installer.hook_command('<서비스>', '<이벤트>')}")
    stale = [s.label for s in config.services.values() if installer.install_state(s) == "stale"]
    if stale:
        print(f"  ⚠ 훅이 낡았습니다 ({', '.join(stale)}) — 옛 경로를 부르거나 이벤트가 빠져 있습니다.")
        print("    `install` 을 다시 돌리면 제자리에서 교체됩니다.")


def cmd_services(args: argparse.Namespace) -> int:
    """서비스별 활성/로그/훅 현황."""
    config = load_config()
    if not config.services:
        print("⚠ 패키지 services.yaml 에 서비스가 없습니다.")
        return 1
    print("TokenMeter 서비스")
    _services_table(config)
    print()
    print("  설정을 고친 뒤에는 `tokenmeter doctor <서비스>` 로 파싱을 검증하세요.")
    return 0


# ── 명령: doctor ───────────────────────────────────────────────────────────


def _doctor_one(spec: ServiceSpec) -> None:
    print(f"[{spec.label}] {spec.name}  (format={spec.format}, mode={spec.mode}, key={spec.key})")
    roots = spec.existing_roots()
    if not roots:
        print("   ⚠ 존재하는 로그 경로가 없습니다: " + ", ".join(_short(r) for r in spec.roots))
        return
    # json 포맷은 파일 하나가 레코드 하나라 표본을 넓게 잡아야 진단이 된다
    paths, mtime, count = _scan_logs(spec, 1 if spec.format == "jsonl" else DOCTOR_JSON_FILES)
    if not paths:
        print(f"   ⚠ patterns {spec.patterns} 에 맞는 로그 파일이 없습니다 (roots: "
              + ", ".join(_short(r) for r in roots) + ")")
        return
    extra = f"  외 최근 {len(paths) - 1}개" if len(paths) > 1 else ""
    print(f"   로그 파일 : {_short(paths[0])}{extra}")
    print(f"   최근 수정 : {_when(mtime)}   (감시 대상 {count:,}개 파일)")

    total = 0
    matched = 0
    sums = {f: 0 for f in TOKEN_FIELDS}
    misses = {f: 0 for f in TOKEN_FIELDS}
    ctx: Dict[str, str] = {}
    bad_match: Dict[str, Any] = {}
    for path in paths:
        for obj in _iter_records(spec, path):
            total += 1
            for name, dot_path in spec.context.items():
                value = dig(obj, dot_path)
                if value not in (None, ""):
                    ctx[name] = str(value)
            ok = True
            for dot_path, want in spec.match.items():
                got = dig(obj, dot_path)
                if str(got) != str(want):
                    bad_match.setdefault(dot_path, got)
                    ok = False
            if not ok:
                continue
            matched += 1
            for field in TOKEN_FIELDS:
                dot_path = spec.fields.get(field)
                if not dot_path:
                    continue
                value = dig(obj, dot_path)
                if value is None:
                    misses[field] += 1
                else:
                    sums[field] += _n(value)

    print(f"   레코드    : 전체 {total:,}개 / match {spec.match or '{}'} 일치 {matched:,}개")
    if not matched:
        hint = ", ".join(f"{k}={v!r}" for k, v in list(bad_match.items())[:3]) or "레코드 없음"
        print(f"   ⚠ match 에 걸리는 레코드가 없습니다 (관측된 값: {hint})")

    rows: List[List[str]] = []
    for field in TOKEN_FIELDS:
        dot_path = spec.fields.get(field)
        if not dot_path:
            rows.append([field, "(미설정)", "-", "-"])
            continue
        if matched and misses[field] == matched:
            note = "⚠ 전부 None — dot-path 확인"
        elif misses[field]:
            note = f"{misses[field]:,}건 None"
        else:
            note = "OK"
        rows.append([field, dot_path, f"{sums[field]:,}", note])
    _table(["필드", "dot-path", "합계", "비고"], rows, right=(2,))

    for name, dot_path in spec.context.items():
        value = ctx.get(name)
        mark = value if value else "⚠ 못 찾음 — dot-path 확인"
        print(f"   context.{name:<6}: {dot_path} → {mark}")
    if spec.input_includes_cache:
        print("   input_includes_cache=true → input 에서 cache_read 를 뺀 값이 반영됩니다")

    reader = ServiceReader(spec, lambda _d: None)
    deltas = [d for path in paths for d in reader.read_file(path, emit=False)]
    tokens = sum(d.total for d in deltas)
    usd = sum(d.cost() for d in deltas)
    models = sorted({d.model for d in deltas})
    projects = sorted({d.project or "(unknown)" for d in deltas})
    sessions = sorted({d.session for d in deltas if d.session})
    vendors = sorted({d.vendor for d in deltas if d.vendor})
    print(f"   추출 델타 : {len(deltas):,}건 · {tokens:,} 토큰 · {_usd(usd)}")
    print(f"   감지 모델 : {', '.join(models) or '-'}   (기본값 {spec.default_model})")
    print(f"   감지 프로젝트: {', '.join(projects) or '-'}")
    # 아래 셋이 비면 벤더·세션 비교 집계에서 이 서비스만 통째로 빠진다
    if sessions:
        print(f"   감지 세션 : {len(sessions)}개  (예: {sessions[0][:24]})")
    else:
        print(f"   감지 세션 : ⚠ 없음 — context.session dot-path 확인 "
              f"({spec.context.get('session') or '미설정'})")
    print(f"   감지 벤더 : {', '.join(vendors) or '⚠ 없음'}"
          f"   (기본값 {spec.vendor or '모델명에서 추론'})")
    plan = reader.plan
    how = "명시" if spec.plan else ("프로브" if spec.plan_probe else "판정 불가")
    print(f"   요금제    : {plan} ({how})" + ("  ⚠ plan 또는 plan_probe 를 설정하세요"
                                              if plan == "unknown" else ""))
    # 엔드포인트는 벤더마다 다를 수 있다 (opencode 처럼 프로바이더별 게이트웨이)
    public = Leaderboard(load_config()).public
    seen = {reader.endpoint_for(d.session, d.vendor) for d in deltas} or {
        reader.endpoint_for("", spec.vendor)
    }
    for url in sorted(u for u in seen if u):
        print(f"   엔드포인트: {url}   → 업로드 라벨 '{classify(url, public)}'")
    if not any(seen):
        print("   엔드포인트: ⚠ 판정 불가 — endpoint 또는 endpoint_probe 를 설정하세요")
    if spec.mode == "cumulative":
        print("   ※ 누적 모드라 파일을 처음 읽는 지금은 누적치 전체가 델타로 계산됩니다.")
        print("     데몬은 기동 시 prime() 으로 baseline 을 잡으므로 이후 증가분만 먹습니다.")


def cmd_doctor(args: argparse.Namespace) -> int:
    """서비스 설정이 실제 로그를 제대로 파싱하는지 검증한다."""
    config = load_config()
    specs = _specs(config, args.service)
    if not specs:
        print("⚠ 검사할 서비스가 없습니다.")
        return 1
    print("TokenMeter 설정 진단\n")
    for i, spec in enumerate(specs):
        if i:
            print()
        _doctor_one(spec)
    return 0


# ── 명령: install / uninstall ──────────────────────────────────────────────


def _activation_lines() -> List[str]:
    return [
        "1. 사용 중인 Claude Code, Codex, OpenCode를 완전히 다시 여세요.",
        "2. 새 프롬프트를 한 번 실행하세요.",
        "3. 오버레이가 뜨거나 `tokenmeter status`에 첫 측정이 보이는지 확인하세요.",
        "4. 보이지 않으면 `tokenmeter doctor`를 실행하세요.",
    ]


def _print_activation() -> None:
    print("  첫 측정 시작하기")
    for line in _activation_lines():
        print("  " + line)


def cmd_install(args: argparse.Namespace) -> int:
    """훅을 설치한다 (멱등). 다른 훅 설정은 건드리지 않는다."""
    config = load_config()
    for line in installer.install(config, args.service, dry_run=args.dry_run):
        print("  " + line)
    print()
    _services_table(config)
    if not args.dry_run:
        print()
        _print_activation()
        if not _has_overlay():
            print()
            print("  ⚠ PyQt6 가 없어 오버레이(미터 창)는 뜨지 않습니다. 측정과 CLI 는 그대로 동작합니다.")
            print("    uv tool install --force git+https://github.com/Oct7/tokenmeter.git")
    return 0


def _kill_daemon() -> bool:
    """돌고 있는 데몬을 내린다. 껐는데 계속 도는 것만큼 헷갈리는 게 없다."""
    pid = _daemon_pid()
    if not pid:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def _toggle_measure(on: bool, services: Optional[List[str]]) -> int:
    config = load_config()
    data = load_toggle()
    if services:
        unknown = [s for s in services if s not in config.services]
        if unknown:
            print(f"  모르는 서비스: {', '.join(unknown)}")
            print(f"  가능한 값: {', '.join(config.services)}")
            return 1
        book = data.setdefault("services", {})
        for name in services:
            book[name] = on
        label = ", ".join(services)
    else:
        data["enabled"] = on
        label = "전체"
    save_toggle(data)

    print(f"  {label} 측정을 {'켰습니다' if on else '껐습니다'}.")
    if not on:
        # 전체를 껐을 때만 내린다. 서비스 하나를 뺀 것뿐이면 나머지가 아직 살아 있다.
        if not services and _kill_daemon():
            print("  실행 중이던 데몬을 종료했습니다.")
        print("  훅은 설정에 그대로 있습니다 — 다시 켜면 즉시 동작합니다.")
    else:
        print("  다음 세션 이벤트에서 데몬이 자동으로 뜹니다.")
    return 0


def cmd_on(args: argparse.Namespace) -> int:
    """측정을 켠다 (전체 또는 특정 서비스)."""
    return _toggle_measure(True, args.service)


def cmd_off(args: argparse.Namespace) -> int:
    """측정을 끈다. 훅은 남겨두고 무력화하므로 재설치가 필요 없다."""
    return _toggle_measure(False, args.service)


def cmd_meter(args: argparse.Namespace) -> int:
    """세션이 시작될 때 미터 창을 자동으로 띄울지 정한다."""
    data = load_toggle()
    if args.state is None:
        print(f"  미터 자동 표시: {'켜짐' if data.get('overlay', True) is not False else '꺼짐'}")
        return 0
    on = args.state == "on"
    data["overlay"] = on
    save_toggle(data)
    print(f"  세션 시작 시 미터 창을 {'띄웁니다' if on else '띄우지 않습니다'}.")
    if not on:
        print("  측정은 그대로 돌아갑니다 — 수치는 `status` 로 봅니다.")
        if _daemon_pid():
            print("  지금 떠 있는 창은 다음 데몬 기동부터 반영됩니다 (`off` → `on` 이면 즉시).")
    elif not _has_overlay():
        print("  ⚠ PyQt6 가 없어 실제로는 뜨지 않습니다.")
        print("    uv tool install --force git+https://github.com/Oct7/tokenmeter.git")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    """선택형 자동 업데이트 설정 또는 즉시 업데이트."""
    data = load_toggle()
    if args.state is None:
        print(f"  자동 업데이트: {'켜짐' if data.get('auto_update') is True else '꺼짐'}")
        return 0
    if args.state in ("on", "off"):
        data["auto_update"] = args.state == "on"
        save_toggle(data)
        print(f"  자동 업데이트를 {'켰습니다' if args.state == 'on' else '껐습니다'}.")
        if args.state == "on":
            print("  데몬 시작 시 하루 한 번 GitHub 정식 릴리스를 확인합니다.")
        return 0
    updated, message = installer.update_package(force=True)
    print("  " + message)
    return 0 if updated is not None else 1


def cmd_uninstall(args: argparse.Namespace) -> int:
    """우리 훅 엔트리만 제거한다."""
    config = load_config()
    for line in installer.uninstall(config, args.service):
        print("  " + line)
    print()
    _services_table(config)
    return 0


# ── 명령: status ───────────────────────────────────────────────────────────


def _totals_row(label: str, totals: Dict[str, Any]) -> List[str]:
    return [
        label,
        _num(totals.get("input_tokens")),
        _num(totals.get("cache_read")),
        _num(totals.get("cache_write")),
        _num(totals.get("output_tokens")),
        _num(totals.get("calls")),
        _usd(totals.get("cost_usd")),
    ]


def _days_table(state: Dict[str, Any], limit: int = 14) -> None:
    """날짜별 사용량, 최근부터. 진행 중인 오늘도 같은 표에 놓아야 추세가 보인다."""
    days = state.get("days")
    merged: Dict[str, Any] = dict(days) if isinstance(days, dict) else {}
    node = state.get("today") or {}
    today = str(node.get("date") or "")
    if today:
        merged[today] = node.get("totals") or {}
    if not merged:
        return
    print()
    _table(
        ["일별 히스토리", "토큰", "호출", "비용", ""],
        [
            [
                day,
                _num(tokens_of(merged[day])),
                _num((merged[day] or {}).get("calls")),
                _usd((merged[day] or {}).get("cost_usd")),
                "◀ 진행 중" if day == today else "",
            ]
            for day in sorted(merged, reverse=True)[:limit]
        ],
        right=(1, 2, 3),
    )


def _group_table(title: str, items: Dict[str, Any], limit: int = 8) -> None:
    """축 하나를 비용 순으로 — 토큰·호출·세션을 한 표에 놓아야 비교가 된다."""
    if not items:
        return
    ranked = sorted(
        items.items(),
        key=lambda kv: float((kv[1].get("totals") or {}).get("cost_usd", 0.0)),
        reverse=True,
    )[:limit]
    print()
    _table(
        [title, "토큰", "호출", "세션", "비용"],
        [
            [
                name,
                _num(tokens_of(node.get("totals"))),
                _num((node.get("totals") or {}).get("calls")),
                _num(node.get("sessions")),
                _usd((node.get("totals") or {}).get("cost_usd")),
            ]
            for name, node in ranked
        ],
        right=(1, 2, 3, 4),
    )


def _board_table(config: Config, state: Dict[str, Any], scope: str, sync: bool) -> None:
    """선택형 자체 호스팅 랭킹. endpoint가 없으면 로컬 한 줄만 보인다."""
    board = Leaderboard(config)
    if sync:
        if not board.online:
            print("  ⚠ settings.leaderboard.endpoint 가 비어 있어 동기화할 곳이 없습니다.")
        else:
            board.sync(state, force=True)
    entries, note = board.board(state, scope)
    print()
    mode = "자체 호스팅" if board.online else "로컬"
    print(f"  랭킹 ({mode} · {'오늘' if scope == 'today' else '누적'})")
    _table(
        ["#", "핸들", "토큰", "비용", ""],
        [
            [str(i + 1), e.handle, _num(e.tokens), _usd(e.cost_usd), "◀ 나" if e.me else ""]
            for i, e in enumerate(entries[:10])
        ],
        right=(2, 3),
    )
    print(f"  {note}")


def cmd_quota(args: argparse.Namespace) -> int:
    """프로바이더 한도(잔여). 자격 증명이 있으면 네트워크로 읽는다."""
    from .quota import chips, load, public, refresh, reset_caption

    snap = load() if args.cached else refresh(force=True)
    if args.json:
        print(json.dumps(public(snap), ensure_ascii=False))
        return 0
    windows = list(snap.get("windows") or [])
    print("TokenMeter 한도")
    if not windows and not snap.get("errors"):
        print("  읽을 자격 증명이 없습니다 (Claude/Codex/Grok 로그인)")
        return 0
    now = time.time()
    rows = []
    for row in windows:
        used = row.get("used")
        if used is not None:
            pct = f"{float(used) * 100:.0f}%"
        elif row.get("remaining_usd") is not None:
            pct = f"${float(row['remaining_usd']):.2f}"
        else:
            pct = "-"
        rows.append([
            row.get("title") or row.get("source") or "-",
            row.get("label") or "-",
            pct,
            reset_caption(row.get("resets_at"), now) or "-",
            row.get("status") or "-",
        ])
    if rows:
        _table(["서비스", "창", "사용", "리셋", "상태"], rows)
    marks = chips(windows)
    if marks:
        print()
        print("  " + "   ".join(text for text, _status in marks))
    errors = snap.get("errors") or {}
    if errors:
        print()
        for name, msg in errors.items():
            print(f"  {name}: {msg}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """토큰/랭킹/라이브 세션 현황."""
    config = load_config()
    state = Meter(config, read_only=True).status()
    if args.json:
        print(json.dumps(public_snapshot(state), ensure_ascii=False))
        return 0
    total = state.get("total", {}).get("totals", {})
    today = state.get("today", {}).get("totals", {})
    pid = _daemon_pid()

    print("TokenMeter 상태")
    if not config.enabled:
        print("  ⚠ 측정 꺼짐 (`tokenmeter on` 으로 재개) — 훅은 그대로 있습니다.")
    off = [s.name for s in config.services.values() if not s.enabled]
    if config.enabled and off:
        print(f"  ⚠ 제외된 서비스: {', '.join(off)}  (`tokenmeter on --service <이름>`)")
    print(f"  데몬   : {'실행 중 (pid ' + str(pid) + ')' if pid else '꺼짐'}"
          f"   ·   라이브 세션 {state.get('live_count', 0)}개")
    if not config.overlay_auto:
        print("  오버레이: 자동 표시 꺼짐 (`tokenmeter meter on`)")
    elif _has_overlay():
        print(f"  오버레이: 사용 가능{'' if pid else ' (데몬이 뜨면 함께 표시됩니다)'}")
    else:
        # 데몬은 멀쩡히 도는데 창만 안 뜨는 상태 — 여기서 말해주지 않으면
        # daemon.log 를 직접 열어보기 전까지 이유를 알 방법이 없다.
        print("  오버레이: PyQt6 없음 — 창이 뜨지 않습니다 (측정은 정상)")
        print("            uv tool install --force git+https://github.com/Oct7/tokenmeter.git")
    print(f"  로그   : {LOG_FILE}")
    if tokens_of(total) == 0 and not state.get("live"):
        print()
        print("  첫 세션 대기 중")
        _print_activation()
        return 0
    approx = _is_approx(state)
    print(f"  오늘   : {_num(tokens_of(today))} 토큰 · {_num(today.get('calls'))} 호출 · "
          f"{sessions_today(state)} 세션 · {_usd_scoped(today, approx)}")
    print(f"  누적   : {_num(tokens_of(total))} 토큰 · {_num(total.get('calls'))} 호출 · "
          f"{_num(state.get('total', {}).get('sessions'))} 세션 · {_usd_scoped(total, approx)}")
    saved = float(total.get("cache_saved_usd") or 0.0)
    if saved > 0:
        print(f"  캐시   : 누적 {_usd(saved)} 절감 (오늘 {_usd(today.get('cache_saved_usd'))})")
    if approx:
        print("  ≈      : 구독 사용분이 섞여 있습니다 — 금액은 실제 청구액이 아니라 API 환산가입니다.")
    unknown = _unknown_models(state)
    if unknown:
        print(f"  ⚠ 가격표에 없는 모델: {', '.join(unknown[:5])}"
              f"{' 외 ' + str(len(unknown) - 5) + '개' if len(unknown) > 5 else ''}")
        print(f"    default 단가로 추정 중입니다 — `{PROG} price set <모델> --input .. --output ..`")
    print(f"  갱신   : {_when(state.get('updated_at', 0.0))}")
    print()

    _table(
        ["구간", "입력", "캐시 읽기", "캐시 쓰기", "출력", "호출", "비용"],
        [
            _totals_row("누적", total),
            _totals_row(f"오늘 ({state.get('today', {}).get('date', '-')})", today),
            _totals_row("이번 세션", state.get("session", {}).get("totals", {})),
        ],
        right=(1, 2, 3, 4, 5, 6),
    )

    _days_table(state)

    for group, title in (
        ("vendors", "API 벤더"),
        ("endpoints", "엔드포인트"),
        ("plans", "요금제"),
        ("models", "모델"),
        ("services", "클라이언트"),
        ("projects", "프로젝트"),
    ):
        _group_table(title, state.get(group) or {})

    _board_table(config, state, args.scope, args.sync)

    live = state.get("live") or []
    print()
    if not live:
        print("  라이브 세션 없음 (대기 중)")
    else:
        _table(
            ["서비스", "프로젝트", "세션", "이벤트", "갱신"],
            [
                [
                    s.get("service", "-"),
                    s.get("project") or "-",
                    str(s.get("session_id", ""))[:16],
                    s.get("event", "-"),
                    _when(s.get("updated_at", 0.0)),
                ]
                for s in live
            ],
        )
    return 0


def cmd_team(args: argparse.Namespace) -> int:
    """자체 호스팅 팀의 익명 관심 집계."""
    config = load_config()
    state = Meter(config, read_only=True).status()
    board = Leaderboard(config)
    if args.sync and board.online:
        board.sync(state, force=True)
    entries, _note = board.team(state)
    if args.json:
        print(json.dumps({
            "schema_version": 1, "type": "team", "timestamp": time.time(),
            "members": [{
                "handle": entry.handle, "check": entry.check, "working": entry.working,
                "waiting": entry.waiting, "risk": entry.risk, "cost_usd": entry.cost_usd,
                "me": entry.me,
            } for entry in entries],
        }, ensure_ascii=False))
        return 0
    _table(
        ["핸들", "확인", "작업", "대기", "위험", "오늘"],
        [[entry.handle, _num(entry.check), _num(entry.working), _num(entry.waiting),
          _num(entry.risk), _usd(entry.cost_usd)] for entry in entries],
        right=(1, 2, 3, 4, 5),
    )
    return 0


def cmd_receipt(args: argparse.Namespace) -> int:
    """최근 저장 세션의 읽기 전용 영수증."""
    state = Meter(load_config(), read_only=True).state
    data = receipt_data(state)
    if data is None:
        print("영수증을 만들 세션이 없습니다.")
        return 1
    print(format_receipt(data, args.format))
    return 0


# ── 명령: price ────────────────────────────────────────────────────────────


PRICE_FIELDS = (
    ("input", "--input", "입력 1M 토큰당 USD"),
    ("cache_read", "--cache-read", "캐시 읽기 1M 토큰당 USD"),
    ("cache_write", "--cache-write", "캐시 쓰기 1M 토큰당 USD"),
    ("output", "--output", "출력 1M 토큰당 USD"),
    # argparse 가 help 문자열에 % 서식을 적용한다 — 퍼센트 기호를 그냥 쓰면 터진다
    ("window", "--window", "컨텍스트 창 토큰 수 (ctx 비율의 분모)"),
)


def _price_row(model: str, seen: bool) -> List[str]:
    p = prices_for(model)
    source = "사용자" if has_override(model) else ("기본" if known(model) else "⚠ 추정(default)")
    return [
        model,
        f"{p['input']:g}", f"{p['cache_read']:g}", f"{p['cache_write']:g}", f"{p['output']:g}",
        _num(p.get("window")) if p.get("window") else "-",
        source + ("" if seen else "  (미사용)"),
    ]


def cmd_price(args: argparse.Namespace) -> int:
    """모델 단가 조회 / 직접 지정. 가격표에 없는 모델은 여기서 못 박는다."""
    values = {field: getattr(args, field, None) for field, _flag, _help in PRICE_FIELDS}
    values = {k: v for k, v in values.items() if v is not None}

    if args.action == "set":
        if not args.model or not values:
            print(f"  사용법: {PROG} price set <모델> --input 3 --output 15 [--window 200000]")
            return 1
        entry = set_price(args.model, values)
        print(f"  {args.model} 단가를 저장했습니다: "
              + ", ".join(f"{k}={v:g}" for k, v in sorted(entry.items())))
        print(f"  파일: {_short(USER_PRICES)}  (데몬 재시작 없이 다음 계산부터 반영)")
        print("  ※ 이미 쌓인 비용은 그때의 단가로 계산돼 있습니다 — 소급되지 않습니다.")
        return 0
    if args.action == "unset":
        if not args.model:
            print(f"  사용법: {PROG} price unset <모델>")
            return 1
        print(f"  {args.model} 오버라이드를 지웠습니다." if unset_price(args.model)
              else f"  {args.model} 에 지정된 오버라이드가 없습니다.")
        return 0

    # 조회 — 실제로 써 본 모델 + 오버라이드를 함께 보여준다
    state = Meter(load_config(), read_only=True).state
    used = list(state.get("models") or {})
    rows = [_price_row(m, True) for m in sorted(used)]
    rows += [_price_row(m, False) for m in sorted(overrides()) if m not in used]
    if not rows:
        rows = [_price_row(m, False) for m in sorted(PRICES) if m != "default"]
    print("모델 단가 (USD / 1M 토큰)")
    _table(["모델", "입력", "캐시읽기", "캐시쓰기", "출력", "컨텍스트", "출처"],
           rows, right=(1, 2, 3, 4, 5))
    unknown = _unknown_models(state)
    if unknown:
        print()
        print(f"  ⚠ 가격표에 없어 default 단가로 계산 중: {', '.join(unknown)}")
        print(f"    {PROG} price set {unknown[0]} --input 3 --output 15 --window 200000")
    return 0


# ── 명령: start / stop ─────────────────────────────────────────────────────


def cmd_start(args: argparse.Namespace) -> int:
    """수동으로 라이브 세션을 등록하고 데몬을 띄운다."""
    from .hook import ensure_daemon

    config = load_config()
    meter = Meter(config, read_only=True)  # 라이브 파일만 다루므로 state 는 안 건드린다
    cwd = os.getcwd()
    path = meter.add_live(
        service=args.service,
        session_id=args.session_id,
        project=args.project or Path(cwd).name,
        cwd=cwd,
        model=args.model,
        event="manual",
    )
    print(f"라이브 세션 등록: {_short(path)}")
    if ensure_daemon():
        print("데몬을 새로 띄웠습니다.")
    else:
        pid = _daemon_pid()
        print(f"데몬 이미 실행 중 (pid {pid})" if pid else "데몬을 띄우지 않았습니다 (수동 실행: tokenmeter daemon)")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    """라이브 세션을 해제한다 (session-id 를 생략하면 해당 서비스 전부)."""
    config = load_config()
    meter = Meter(config, read_only=True)
    targets = [
        s
        for s in meter.live_sessions()
        if (not args.service or s.get("service") == args.service)
        and (not args.session_id or s.get("session_id") == args.session_id)
    ]
    if not targets:
        print("해제할 라이브 세션이 없습니다.")
        return 0
    # archive 는 writer 만 허용된다. state 저장은 하지 않고 히스토리 스냅샷만 남긴다.
    meter.read_only = False
    for s in targets:
        service, sid = s.get("service", ""), s.get("session_id", "")
        meter.archive(service, sid)
        meter.remove_live(service, sid)
        print(f"해제: {service} / {sid}")
    meter.read_only = True
    return 0


# ── 명령: watch / overlay / reset ──────────────────────────────────────────


def cmd_watch(args: argparse.Namespace) -> int:
    """로그 감시만 실행 (오버레이 없음)."""
    if args.jsonl:
        meter = Meter(load_config(), read_only=True)
        state = meter.status()
        previous_totals = (state.get("total") or {}).get("totals") or {}
        previous_updated_at = state.get("updated_at", 0.0)
        previous_attention = tuple(sorted(
            (row["service"], row["project"], row["started_at"], row["attention"], row["attention_at"])
            for row in session_views(state)
        ))

        def emit(record: Dict[str, Any]) -> None:
            sys.stdout.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()

        emit(public_snapshot(state))
        try:
            while True:
                time.sleep(0.5)
                meter.reload()
                state = meter.status()
                totals = (state.get("total") or {}).get("totals") or {}
                updated_at = state.get("updated_at", 0.0)
                attention = tuple(sorted(
                    (row["service"], row["project"], row["started_at"], row["attention"], row["attention_at"])
                    for row in session_views(state)
                ))
                reset = False
                if updated_at != previous_updated_at:
                    delta = totals_delta(previous_totals, totals)
                    if delta is None:
                        emit(public_snapshot(state))
                        reset = True
                    elif delta:
                        record = public_snapshot(state, "delta")
                        record["delta"] = delta
                        emit(record)
                if attention != previous_attention and not reset:
                    emit(public_snapshot(state, "attention"))
                previous_totals = totals
                previous_updated_at = updated_at
                previous_attention = attention
        except KeyboardInterrupt:
            return 0
    pid = _daemon_pid()
    if pid:
        # state.json 은 통째로 덮어쓰기라 writer 가 둘이면 나중에 쓴 쪽이 상대 누적치를 지운다
        print(f"✗ 데몬(pid {pid})이 이미 상태 파일을 쓰고 있습니다 — 먼저 멈추세요: kill {pid}")
        return 1
    config = load_config()
    meter = Meter(config)
    watcher = MultiWatcher(meter, config)
    watcher.start(args.service or None)
    if not watcher.readers:
        print("⚠ 감시할 서비스가 없습니다 (로그 경로 확인: tokenmeter services)")
        watcher.stop()
        return 1
    print(f"감시 중: {', '.join(watcher.readers)}   (Ctrl+C 로 종료)")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        watcher.stop()
    print("감시 종료")
    return 0


def cmd_overlay(args: argparse.Namespace) -> int:
    """오버레이만 실행 (읽기 전용 — 토큰은 데몬이 먹인다)."""
    from .overlay import run_overlay

    meter = Meter(load_config(), read_only=True)
    return 0 if run_overlay(meter) else 1


def cmd_reset(args: argparse.Namespace) -> int:
    """누적 통계를 초기화한다 (누적/오늘/세션/프로젝트/서비스/모델)."""
    if not args.yes:
        print("정말 초기화하려면 `--yes` 를 붙이세요.")
        return 1
    pid = _daemon_pid()
    if pid:
        # 데몬은 자기 메모리를 그대로 다시 저장하므로 초기화가 곧바로 되돌려진다
        print(f"✗ 데몬(pid {pid})이 실행 중이라 초기화가 곧 덮어써집니다 — 먼저 멈추세요: kill {pid}")
        return 1
    meter = Meter(load_config())
    meter.reset_stats()
    print("통계를 초기화했습니다. (이미 서버에 올라간 랭킹은 다음 동기화 때 덮어써집니다)")
    return 0


def cmd_adapter(args: argparse.Namespace) -> int:
    """개인 값을 남기지 않는 새 서비스 어댑터 초안을 만든다/검사한다."""
    if args.adapter_action == "init":
        if (not args.name or args.name in {".", ".."} or "/" in args.name
                or "\\" in args.name or Path(args.name).is_absolute()):
            print("✗ 서비스 이름은 경로 구분자가 없는 한 단어여야 합니다")
            return 1
        ok, message = init_adapter(args.name, Path(args.log), Path.cwd() / f"{args.name}-adapter")
        print(("✓ " if ok else "✗ ") + message)
        return 0 if ok else 1
    ok, messages = check_adapter(Path(args.path))
    for message in messages:
        print(message)
    return 0 if ok else 1


# ── 명령: daemon ───────────────────────────────────────────────────────────


def notify(title: str, message: str) -> bool:
    """데스크톱 알림 한 번. 띄울 수단이 없으면 조용히 False (측정은 계속돼야 한다)."""
    import subprocess

    if sys.platform == "darwin":
        # 프로젝트 이름에 따옴표/역슬래시가 있으면 AppleScript 문자열이 깨진다 → 먼저 턴다
        def _safe(text: str) -> str:
            return "".join(c for c in text if c not in '"\\\n')

        cmd = ["osascript", "-e",
               f'display notification "{_safe(message)}" with title "{_safe(title)}"']
    elif sys.platform.startswith("linux"):
        cmd = ["notify-send", title, message]
    else:
        return False
    try:
        subprocess.run(cmd, check=False, timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def attention_notice_key(row: Dict[str, Any]) -> Tuple[str, float]:
    if row.get("attention") != "check":
        return "", 0.0
    return str(row.get("key") or ""), _float(row.get("attention_at"))


def _idle_loop(meter: Meter, config: Config, stop: threading.Event,
               board: Optional[Leaderboard] = None) -> None:
    """라이브 세션을 지켜보다 유휴 시간이 넘으면 돌아온다. 랭킹 동기화도 여기서 태운다."""
    ttl = float(config.setting("live_ttl_hours", 6))
    idle_limit = float(config.setting("idle_exit_minutes", 30)) * 60.0
    idle_since: Optional[float] = None
    attention_setting = config.setting("attention_notify", None)
    notify_attention = (config.setting("idle_notify_seconds", 0) != 0
                        if attention_setting is None else bool(attention_setting))
    notified_at: Dict[str, float] = {}
    while not stop.is_set():
        meter.prune_live(ttl)  # 종료 훅을 못 받고 남은 파일 정리
        live_now = meter.live_sessions()
        status = dict(meter.state)
        status["live"] = live_now
        status["live_count"] = len(live_now)
        if board is not None:  # sync 가 sync_seconds 로 스스로 스로틀한다
            board.sync(status)
        if notify_attention:
            for row in session_views(status):
                key, attention_at = attention_notice_key(row)
                if key and attention_at > notified_at.get(key, 0.0):
                    notified_at[key] = attention_at
                    notify("TokenMeter", f"{row['project']} · 확인 필요")
        if live_now:
            idle_since = None
        elif idle_since is None:
            idle_since = time.time()
        elif idle_limit > 0 and time.time() - idle_since >= idle_limit:
            print(f"[TokenMeter] 라이브 세션이 {idle_limit / 60:.0f}분간 없어 데몬을 종료합니다.")
            return
        stop.wait(IDLE_TICK_SEC)


def cmd_daemon(args: argparse.Namespace) -> int:
    """워처(백그라운드 스레드) + 오버레이(메인 스레드). 훅이 자동으로 띄운다."""
    pid = _daemon_pid()
    if pid:
        print(f"[TokenMeter] 데몬이 이미 실행 중입니다 (pid {pid}) — 중복 실행하지 않습니다.")
        return 0

    if not load_config().enabled:
        print("[TokenMeter] 측정이 꺼져 있어(`tokenmeter off`) 데몬을 띄우지 않습니다.")
        return 0

    updated, message = installer.update_package()
    if message:
        print(f"[TokenMeter] {message}")
    if updated:
        argv = [sys.executable, "-m", "tokenmeter.cli", "daemon"]
        if args.no_overlay:
            argv.append("--no-overlay")
        try:
            os.execv(sys.executable, argv)
        except OSError as exc:
            print(f"[TokenMeter] 새 버전 재실행 실패, 현재 프로세스로 계속합니다: {exc}")

    ensure_dirs()
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    # 훅의 기동 락(data/daemon.lock)은 성공 경로에서 아무도 지우지 않는다.
    # pid 를 쓴 뒤에 지워야 경쟁하는 훅이 락 → pid 순으로 반드시 막힌다.
    from .hook import LOCK_FILE

    LOCK_FILE.unlink(missing_ok=True)
    config = load_config()
    meter = Meter(config)
    watcher = MultiWatcher(meter, config)
    board = Leaderboard(config)
    stop = threading.Event()

    def _shutdown(_signum: int, _frame: Any) -> None:
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _shutdown)
        except (ValueError, OSError):
            pass  # 메인 스레드가 아니면 무시

    def _cleanup() -> None:
        watcher.stop()
        try:
            if PID_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
                PID_FILE.unlink()
        except (OSError, ValueError):
            pass

    watcher.start()
    print(f"[TokenMeter] 데몬 시작 pid={os.getpid()} 서비스=[{', '.join(watcher.readers) or '없음'}]")
    print("[TokenMeter] 이미 쌓인 로그는 세지 않습니다 — 지금부터 늘어나는 분만 반영됩니다.")
    show_overlay = not args.no_overlay and config.overlay_auto
    if not config.overlay_auto:
        print("[TokenMeter] `meter off` 상태라 창을 띄우지 않습니다 (측정은 계속).")
    elif not _has_overlay():
        print("[TokenMeter] PyQt6 가 없어 오버레이 없이 감시만 합니다.")
    try:
        if show_overlay:
            from .overlay import run_overlay

            def _guard() -> None:
                _idle_loop(meter, config, stop, board)
                _cleanup()
                # ponytail: Qt 이벤트 루프를 바깥에서 깨울 방법이 없어 하드 종료한다.
                #           오버레이에 종료 시그널 훅이 생기면 그쪽으로 바꿀 것.
                os._exit(0)

            threading.Thread(target=_guard, name="tokenmeter-idle", daemon=True).start()
            if run_overlay(meter, app_exec=True, board=board):
                return 0  # 오버레이 창을 닫으면 데몬도 끝
            print("[TokenMeter] 오버레이 없이 감시만 계속합니다.")
        _idle_loop(meter, config, stop, board)
    finally:
        stop.set()
        _cleanup()
    return 0


# ── 파서 ───────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tokenmeter",
        description="TokenMeter — 에이전트 토큰 자동 측정 + 미터/랭킹 오버레이",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, func: Any, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(func=func)
        return p

    p = add("install", cmd_install, "에이전트에 훅을 설치한다 (멱등)")
    p.add_argument("--service", action="append", help="서비스 이름 (여러 번 지정 가능)")
    p.add_argument("--dry-run", action="store_true", help="무엇이 바뀌는지만 보여준다")

    p = add("uninstall", cmd_uninstall, "설치한 훅을 제거한다")
    p.add_argument("--service", action="append", help="서비스 이름 (여러 번 지정 가능)")

    p = add("on", cmd_on, "측정을 켠다 (--service 로 특정 서비스만)")
    p.add_argument("--service", action="append", help="서비스 이름 (여러 번 지정 가능)")

    p = add("off", cmd_off, "측정을 끈다 — 훅은 남기고 무력화한다 (재설치 불필요)")
    p.add_argument("--service", action="append", help="서비스 이름 (여러 번 지정 가능)")

    p = add("meter", cmd_meter, "세션 시작 시 미터 창을 띄울지 (인자 없으면 현재 값)")
    p.add_argument("state", nargs="?", choices=["on", "off"], help="on | off")

    p = add("update", cmd_update, "정식 릴리스 자동 업데이트 설정 / 즉시 업데이트")
    p.add_argument("state", nargs="?", choices=["on", "off", "now"], help="on | off | now")

    add("services", cmd_services, "서비스별 활성/로그/훅 현황")

    p = add("doctor", cmd_doctor, "서비스 설정이 실제 로그를 파싱하는지 검증한다")
    p.add_argument("service", nargs="*", help="검사할 서비스 (생략하면 전부)")

    p = add("quota", cmd_quota, "프로바이더 한도(잔여 5h/주간/크레딧)")
    p.add_argument("--json", action="store_true", help="한도를 JSON으로 출력")
    p.add_argument("--cached", action="store_true", help="네트워크 없이 캐시만 읽는다")

    p = add("status", cmd_status, "토큰/랭킹/라이브 세션 현황")
    p.add_argument("--scope", choices=("today", "total"), default="today", help="랭킹 기준 구간")
    p.add_argument("--sync", action="store_true", help="랭킹을 지금 업로드·조회한다")
    p.add_argument("--json", action="store_true", help="공개 상태를 JSON으로 출력")

    p = add("team", cmd_team, "자체 호스팅 팀 관심 현황")
    p.add_argument("--sync", action="store_true", help="팀 현황을 지금 업로드·조회한다")
    p.add_argument("--json", action="store_true", help="팀 현황을 JSON으로 출력")

    p = add("receipt", cmd_receipt, "최근 세션 영수증")
    p.add_argument("--format", choices=("text", "markdown", "json"), default="text",
                   help="출력 형식 (기본: text)")

    p = add("price", cmd_price, "모델 단가 조회 / 직접 지정 (가격표에 없는 모델)")
    p.add_argument("action", nargs="?", choices=("set", "unset"), help="생략하면 조회")
    p.add_argument("model", nargs="?", help="로그에 찍히는 모델 이름 그대로 (예: claude-opus-5[1m])")
    for field, flag, help_text in PRICE_FIELDS:
        p.add_argument(flag, dest=field, type=float, help=help_text)

    p = add("daemon", cmd_daemon, "워처 + 오버레이 실행 (훅이 자동으로 띄운다)")
    p.add_argument("--no-overlay", action="store_true", help="오버레이 없이 감시만")

    p = add("start", cmd_start, "수동으로 라이브 세션을 등록하고 데몬을 띄운다")
    p.add_argument("--service", default="manual", help="서비스 이름 (기본: manual)")
    p.add_argument("--session-id", default="", help="세션 id (기본: 자동)")
    p.add_argument("--project", default="", help="프로젝트 이름 (기본: 현재 디렉토리)")
    p.add_argument("--model", default="", help="모델 이름")

    p = add("stop", cmd_stop, "라이브 세션을 해제한다")
    p.add_argument("--service", default="", help="서비스 이름 (생략: 전부)")
    p.add_argument("--session-id", default="", help="세션 id (생략: 해당 서비스 전부)")

    p = add("watch", cmd_watch, "로그 감시만 실행 (오버레이 없음)")
    p.add_argument("--service", action="append", help="감시할 서비스 (여러 번 지정 가능)")
    p.add_argument("--jsonl", action="store_true", help="읽기 전용 상태 스트림을 JSONL로 출력")

    add("overlay", cmd_overlay, "오버레이만 실행 (읽기 전용)")

    p = add("reset", cmd_reset, "누적 통계 초기화")
    p.add_argument("--yes", action="store_true", help="확인 없이 초기화")

    p = add("adapter", cmd_adapter, "개인 정보를 남기지 않는 서비스 어댑터 초안")
    adapter_sub = p.add_subparsers(dest="adapter_action", required=True)
    init = adapter_sub.add_parser("init", help="로그에서 어댑터 초안을 만든다")
    init.add_argument("name", help="서비스 이름")
    init.add_argument("--log", required=True, help="JSON/JSONL 로그 파일 또는 디렉터리")
    check = adapter_sub.add_parser("check", help="어댑터 초안 구조를 검사한다")
    check.add_argument("path", help="service.yaml과 fixture.json이 있는 디렉터리")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        # 데몬/watch 는 오래 돈다. 파이프로 리다이렉트돼도 로그가 실시간으로 보이게.
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, OSError):
        pass
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.func(args) or 0)
    except BrokenPipeError:
        # `status | head` 처럼 읽는 쪽이 먼저 닫는 건 정상이다. 인터프리터가 종료할 때
        # stdout 을 다시 flush 하다 또 터지므로 devnull 로 갈아끼우고 조용히 나간다.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


if __name__ == "__main__":
    sys.exit(main())
