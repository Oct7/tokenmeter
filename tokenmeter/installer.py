"""훅 설치/해제 (Infrastructure).

사용자의 `~/.claude/settings.json` 에는 다른 훅·플러그인·설정이 가득하다.
따라서 이 모듈의 유일한 원칙은 **우리 엔트리만 건드린다** 이다.

- 기존 키/순서/값은 그대로 두고 우리 커맨드(MARKER 포함)만 추가·교체·제거한다.
- 첫 수정 전에 `<path>.bak-tokenmeter` 백업을 남긴다 (이미 있으면 덮어쓰지 않음).
- 두 번 설치해도 엔트리는 하나 (멱등).
- JSON 파싱에 실패하면 **아무것도 쓰지 않고** 건너뛴다. 망가진 설정을 덮어쓰는 게 최악이다.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import __version__
from .config import ROOT, Config, ServiceSpec, load_toggle, save_toggle

HOOK_SCRIPT = Path(__file__).resolve().parent / "hook.py"  # 항상 나와 같은 디렉토리
MARKER = f'"{HOOK_SCRIPT}"'  # 우리 엔트리 식별자 (부분 경로가 아니라 절대 경로 전체)
# 패키지가 src/ 였던 시절에 설치된 엔트리. 인식하지 못하면 install 이 새 엔트리를
# 덧붙이고, 죽은 경로를 가리키는 옛 엔트리가 매 세션 실패로 남는다.
# ponytail: 이행용 — 옛 설치본이 사라졌다고 판단되면 이 줄만 지우면 된다.
LEGACY_MARKERS = (
    f'"{ROOT / "src" / "hook.py"}"',
    f'"{ROOT / "tokenpet" / "hook.py"}"',
    '/tokenpet/hook.py"',
    '\\tokenpet\\hook.py"',
)
PLUGIN_MARKER = "tokenmeter:generated"  # 우리가 만든 플러그인 파일인지 확인하는 마커
LEGACY_PLUGIN_MARKER = "tokenpet:generated"
BACKUP_SUFFIX = ".bak-tokenmeter"
HOOK_TIMEOUT = 5
UPDATE_INTERVAL = 86400
RELEASE_URL = "https://api.github.com/repos/Oct7/tokenmeter/releases/latest"
REPOSITORY = "git+https://github.com/Oct7/tokenmeter.git"
_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

# OpenCode 1.18.x 플러그인 (ESM, named export 하는 async 팩토리).
# __PY__ / __HOOK__ / __SERVICE__ 는 설치 시점의 절대 경로로 치환된다.
_PLUGIN_TEMPLATE = """// TokenMeter 이 생성함 — 지우면 연동 해제됩니다.
// __MARKER__
import { spawn } from "node:child_process"

const PY = __PY__
const HOOK = __HOOK__

export const TokenMeter = async ({ directory }) => {
  const fire = (event, sessionID) => {
    try {
      spawn(PY, [HOOK, __SERVICE__, event, sessionID || ""], {
        detached: true,
        stdio: "ignore",
        env: { ...process.env, TOKENMETER_CWD: directory || process.cwd() },
      }).unref()
    } catch {}
  }
  const EVENTS = new Set([
    "session.created", "session.deleted", "session.idle",
    "permission.asked", "permission.v2.asked", "permission.replied", "permission.v2.replied",
    "question.asked", "question.v2.asked", "question.replied", "question.v2.replied",
  ])
  return {
    event: async ({ event }) => {
      const type = event?.type
      if (!EVENTS.has(type)) return
      const mapped = type === "session.created" ? "SessionStart"
        : type === "session.deleted" ? "SessionEnd" : type
      fire(mapped, event?.properties?.sessionID)
    },
  }
}
"""


def hook_command(service: str, event: str) -> str:
    """설정에 박아 넣을 훅 커맨드 한 줄."""
    return f'"{sys.executable}" "{HOOK_SCRIPT}" {service} {event}'


def plugin_source(service: str) -> str:
    """OpenCode 플러그인 .js 본문 (경로는 JSON 이스케이프해서 문자열 리터럴로)."""
    return (
        _PLUGIN_TEMPLATE.replace("__MARKER__", PLUGIN_MARKER)
        .replace("__PY__", json.dumps(sys.executable))
        .replace("__HOOK__", json.dumps(str(HOOK_SCRIPT)))
        .replace("__SERVICE__", json.dumps(service))
    )


# ── JSON 파일 입출력 ────────────────────────────────────────────────────────


def _load_json(path: Path) -> Dict[str, Any]:
    """없으면 {}. 깨져 있으면 예외를 그대로 올려 호출부가 '건드리지 않음' 처리하게 한다."""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("최상위가 object 가 아님")
    return data


def _backup(path: Path) -> None:
    """우리가 쓰기 직전 상태를 남긴다. 매번 갱신 — 첫 설치 때의 스냅샷을 몇 달 뒤에
    복원하면 그 사이 편집이 통째로 날아가므로 오래된 백업이 오히려 위험하다."""
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + BACKUP_SUFFIX))


def _save_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tokenmeter-tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if path.exists():
        os.chmod(tmp, path.stat().st_mode & 0o7777)  # 원본 권한 유지 (secrets 가 든 파일이다)
    os.replace(tmp, path)


def _is_ours(entry: Any, service: str, event: str) -> bool:
    """우리 엔트리인지. 절대 경로 훅 스크립트 + 정확한 `<service> <event>` 꼬리까지 봐야
    같은 파일명을 쓰는 남의 훅이나 다른 서비스의 엔트리를 우리 것으로 오인하지 않는다."""
    if not isinstance(entry, dict):
        return False
    cmd = str(entry.get("command", ""))
    if not cmd.endswith(f" {service} {event}"):
        return False
    return MARKER in cmd or any(m in cmd for m in LEGACY_MARKERS)


# ── claude_json 타겟 (Claude Code settings.json / Codex hooks.json 동일 스키마) ──


def _edit_claude_json(data: Dict[str, Any], service: str, events: List[str], remove: bool) -> bool:
    """우리 엔트리만 추가/교체/제거. 실제로 바뀌었으면 True."""
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        if remove:
            return False
        hooks = {}
        data["hooks"] = hooks

    # 제거할 땐 설정에 적힌 이벤트뿐 아니라 파일 전체를 훑는다
    # (events 가 바뀌기 전에 설치된 잔재까지 걷어내기 위해).
    targets = list(hooks.keys()) if remove else list(events)

    changed = False
    for event in targets:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            if remove:
                continue
            groups = []
            hooks[event] = groups

        wanted = hook_command(service, event)
        found = False
        for group in list(groups):
            entries = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(entries, list):
                continue
            ours = [e for e in entries if _is_ours(e, service, event)]
            if not ours:
                continue
            found = True
            if remove:
                for entry in ours:
                    entries.remove(entry)
                changed = True
                if not entries:
                    # 우리가 비운 그룹만 제거한다 (원래 비어 있던 남의 그룹은 손대지 않음)
                    groups.remove(group)
            else:
                for entry in ours:
                    if entry.get("command") != wanted:
                        entry["command"] = wanted
                        changed = True

        if remove:
            # 우리가 비운 이벤트만 정리한다. 원래 비어 있던 남의 이벤트는 그대로 둔다.
            if found and not groups and event in hooks:
                hooks.pop(event)
                changed = True
        elif not found:
            groups.append(
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": wanted, "timeout": HOOK_TIMEOUT}],
                }
            )
            changed = True
    return changed


def _apply_claude_json(spec: ServiceSpec, remove: bool, dry_run: bool) -> str:
    path = spec.install.path
    events = list(spec.install.events)
    try:
        data = _load_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return f"{spec.label}: {path} 를 읽을 수 없어 건너뜁니다 ({exc}) — 파일은 그대로 둡니다"

    changed = _edit_claude_json(data, spec.name, events, remove=remove)
    verb = "해제" if remove else "설치"
    if dry_run:
        return f"{spec.label}: (dry-run) {path} → {verb} {'필요' if changed else '불필요 (이미 반영됨)'}"
    if not changed:
        return f"{spec.label}: 이미 {verb}된 상태 → {path}"
    try:
        _backup(path)
        _save_json(path, data)
    except OSError as exc:  # 한 서비스의 쓰기 실패로 나머지 설치까지 중단되면 안 된다
        return f"{spec.label}: {path} 에 쓸 수 없어 건너뜁니다 ({exc}) — 파일은 그대로 둡니다"
    detail = f" [{', '.join(events)}]" if not remove and events else ""
    return f"{spec.label}: {verb} 완료 → {path}{detail}"


# ── opencode_plugin 타겟 ────────────────────────────────────────────────────


def _legacy_plugin(path: Path) -> Optional[Path]:
    return path.with_name("tokenpet.js") if path.name == "tokenmeter.js" else None


def _is_generated_plugin(text: str) -> bool:
    return PLUGIN_MARKER in text or LEGACY_PLUGIN_MARKER in text


def _apply_opencode_plugin(spec: ServiceSpec, remove: bool, dry_run: bool) -> str:
    path = spec.install.path
    legacy = _legacy_plugin(path)
    legacy_generated = False
    if legacy is not None and legacy.exists():
        try:
            legacy_generated = _is_generated_plugin(legacy.read_text(encoding="utf-8"))
        except OSError:
            pass
    if remove:
        if not path.exists():
            if legacy_generated and not dry_run:
                legacy.unlink(missing_ok=True)
            return f"{spec.label}: 이미 해제된 상태 → {path}"
        try:
            if not _is_generated_plugin(path.read_text(encoding="utf-8")):
                return f"{spec.label}: {path} 는 TokenMeter 이 만든 파일이 아니라 남겨둡니다"
        except OSError as exc:
            return f"{spec.label}: {path} 를 읽을 수 없어 건너뜁니다 ({exc})"
        if dry_run:
            return f"{spec.label}: (dry-run) {path} 삭제 예정"
        path.unlink()
        if legacy_generated:
            legacy.unlink(missing_ok=True)
        return f"{spec.label}: 해제 완료 → {path} 삭제"

    source = plugin_source(spec.name)
    if path.exists():
        # 삭제할 때와 같은 기준으로 남의 파일을 지킨다 (덮어쓰기도 데이터 유실이다)
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError as exc:
            return f"{spec.label}: {path} 를 읽을 수 없어 건너뜁니다 ({exc})"
        if existing == source:
            if legacy_generated and not dry_run:
                legacy.unlink(missing_ok=True)
            return f"{spec.label}: 이미 설치된 상태 → {path}"
        if not _is_generated_plugin(existing):
            return f"{spec.label}: {path} 는 TokenMeter 이 만든 파일이 아니라 남겨둡니다"
    if dry_run:
        return f"{spec.label}: (dry-run) {path} 에 플러그인 생성 예정"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _backup(path)
        path.write_text(source, encoding="utf-8")
        if legacy_generated:
            legacy.unlink(missing_ok=True)
    except OSError as exc:
        return f"{spec.label}: {path} 에 쓸 수 없어 건너뜁니다 ({exc}) — 파일은 그대로 둡니다"
    return f"{spec.label}: 설치 완료 → {path}"


# ── 패키지 자동 업데이트 ────────────────────────────────────────────────────


def _version(value: str) -> Optional[Tuple[int, int, int]]:
    match = _VERSION.fullmatch(value.strip())
    return tuple(map(int, match.groups())) if match else None


def _latest_release() -> Tuple[str, Tuple[int, int, int]]:
    request = urllib.request.Request(
        RELEASE_URL,
        headers={"Accept": "application/vnd.github+json", "User-Agent": f"TokenMeter/{__version__}"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        data = json.loads(response.read(65536))
    tag = str(data.get("tag_name") or "") if isinstance(data, dict) else ""
    parsed = _version(tag)
    if parsed is None:
        raise ValueError(f"지원하지 않는 릴리스 태그: {tag or '(없음)'}")
    return ".".join(map(str, parsed)), parsed


def _update_command(version: str) -> Optional[List[str]]:
    """현재 가상환경을 만든 도구로 해당 정식 릴리스만 설치한다."""
    prefix = Path(sys.prefix).resolve()
    spec = f"{REPOSITORY}@v{version}"
    pipx = shutil.which("pipx")
    if pipx and (prefix / "pipx_metadata.json").exists():
        return [pipx, "install", "--force", spec]

    uv = shutil.which("uv")
    if not uv:
        return None
    try:
        found = subprocess.run(
            [uv, "tool", "dir"], capture_output=True, text=True, timeout=5, check=False,
        )
        tool_dir = Path(found.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        return None
    if found.returncode == 0 and (prefix == tool_dir or tool_dir in prefix.parents):
        return [uv, "tool", "install", "--force", spec]
    return None


def update_package(force: bool = False, now: Optional[float] = None) -> Tuple[Optional[bool], str]:
    """새 정식 릴리스를 설치한다. True=갱신, False=변경 없음, None=실패."""
    toggle = load_toggle()
    if not force and toggle.get("auto_update") is not True:
        return False, ""
    now = time.time() if now is None else now
    try:
        checked = float(toggle.get("update_checked_at") or 0)
    except (TypeError, ValueError):
        checked = 0
    if not force and 0 <= now - checked < UPDATE_INTERVAL:
        return False, ""

    # 실패해도 매 세션마다 GitHub를 두드리지 않도록 시도 시각을 먼저 기록한다.
    toggle["update_checked_at"] = now
    save_toggle(toggle)
    try:
        latest, latest_tuple = _latest_release()
    except (OSError, ValueError) as exc:
        return None, f"업데이트 확인 실패: {exc}"
    current = _version(__version__)
    if current is None:
        return None, f"현재 버전을 판독할 수 없습니다: {__version__}"
    if latest_tuple <= current:
        return False, f"최신 버전입니다 (v{__version__})" if force else ""

    command = _update_command(latest)
    if command is None:
        return None, "uv tool 또는 pipx 설치본에서만 자동 업데이트할 수 있습니다"
    try:
        result = subprocess.run(command, timeout=300, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"v{latest} 업데이트 실패: {exc}"
    if result.returncode != 0:
        return None, f"v{latest} 업데이트 실패 (exit {result.returncode})"
    return True, f"v{__version__} → v{latest} 업데이트 완료"


# ── 공개 API ────────────────────────────────────────────────────────────────


def _targets(config: Config, services: Optional[List[str]]) -> List[ServiceSpec]:
    if services:
        # 이름을 직접 준 경우엔 enabled 여부와 무관하게 사용자의 뜻을 따른다
        return [s for s in (config.get(n) for n in services) if s is not None]
    return config.enabled_services()


def _apply(config: Config, services: Optional[List[str]], remove: bool, dry_run: bool) -> List[str]:
    messages: List[str] = []
    for spec in _targets(config, services):
        inst = spec.install
        if inst is None or inst.target == "none":
            messages.append(f"{spec.label}: 훅 없음 (로그 감시만)")
        elif inst.path is None:
            messages.append(f"{spec.label}: install.path 가 없어 건너뜁니다")
        elif inst.target == "claude_json":
            messages.append(_apply_claude_json(spec, remove, dry_run))
        elif inst.target == "opencode_plugin":
            messages.append(_apply_opencode_plugin(spec, remove, dry_run))
        else:
            messages.append(f"{spec.label}: 알 수 없는 install.target={inst.target}")
    return messages


def install(config: Config, services: Optional[List[str]] = None, dry_run: bool = False) -> List[str]:
    """훅을 설치하고 결과 메시지를 돌려준다. 멱등."""
    return _apply(config, services, remove=False, dry_run=dry_run)


def uninstall(config: Config, services: Optional[List[str]] = None) -> List[str]:
    """우리 엔트리만 제거한다. 다른 훅은 한 글자도 바뀌지 않는다."""
    return _apply(config, services, remove=True, dry_run=False)


def installed_events(spec: ServiceSpec) -> List[str]:
    """우리 엔트리가 실제로 붙어 있는 이벤트들 (claude_json 전용, 그 외는 [])."""
    inst = spec.install
    if inst is None or inst.path is None or inst.target != "claude_json":
        return []
    try:
        hooks = _load_json(inst.path).get("hooks")
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(hooks, dict):
        return []
    return [
        event
        for event in inst.events
        if any(
            _is_ours(e, spec.name, event)
            for g in (hooks.get(event) or [])
            if isinstance(g, dict)
            for e in (g.get("hooks") or [])
        )
    ]


def install_state(spec: ServiceSpec) -> str:
    """`skip` | `missing` | `stale` | `ok` — 훅 설치 상태의 단일 판정.

    `stale` 은 **우리 엔트리는 있는데 지금 쓸 것과 다른** 상태다. 경로가 낡았거나
    (저장소 이동·venv 삭제·패키지 리네임) 이벤트가 늘어난 뒤 일부만 붙어 있는 경우로,
    둘 다 `install` 을 다시 돌려야 한다. 훅은 실패해도 조용히 exit 0 이라
    이 구분이 없으면 사용자가 알아챌 방법이 없다.
    """
    inst = spec.install
    if inst is None or inst.target == "none" or inst.path is None:
        return "skip"
    if stale_command(spec):
        return "stale"
    if inst.target == "claude_json":
        if not inst.events:
            return "missing"
        found = installed_events(spec)
        if not found:
            return "missing"
        return "ok" if len(found) == len(inst.events) else "stale"
    if inst.target == "opencode_plugin":
        try:
            if inst.path.exists() and PLUGIN_MARKER in inst.path.read_text(encoding="utf-8"):
                return "ok"
        except OSError:
            return "missing"
    return "missing"


def is_installed(spec: ServiceSpec) -> bool:
    """완전히·최신으로 붙어 있나. 낡았거나 반만 붙었으면 False."""
    return install_state(spec) == "ok"


def stale_command(spec: ServiceSpec) -> str:
    """설치돼 있지만 지금 만들 커맨드와 다르면 그 **옛 커맨드**를 돌려준다 (아니면 "").

    저장소를 옮기거나 venv 를 지우거나 패키지가 리네임돼도 `is_installed` 는 True 다
    (우리 엔트리인 건 맞으니까). 그래서 '설치됨' 만 보고 있으면 죽은 경로를 부르는
    상태를 영영 눈치채지 못한다 — 훅은 항상 exit 0 이라 에이전트도 조용하다.
    """
    inst = spec.install
    if inst is None or inst.path is None:
        return ""
    try:
        if inst.target == "claude_json":
            hooks = _load_json(inst.path).get("hooks")
            if not isinstance(hooks, dict):
                return ""
            for event in inst.events:
                wanted = hook_command(spec.name, event)
                for group in hooks.get(event) or []:
                    if not isinstance(group, dict):
                        continue
                    for entry in group.get("hooks") or []:
                        cmd = str(entry.get("command", "")) if isinstance(entry, dict) else ""
                        if _is_ours(entry, spec.name, event) and cmd != wanted:
                            return cmd
        elif inst.target == "opencode_plugin" and inst.path.exists():
            text = inst.path.read_text(encoding="utf-8")
            if PLUGIN_MARKER in text and text != plugin_source(spec.name):
                return f"{inst.path} (플러그인 내용이 현재 경로와 다름)"
    except (OSError, ValueError, json.JSONDecodeError):
        return ""
    return ""


def status(config: Config) -> List[Dict[str, Any]]:
    """서비스별 설치 현황. CLI 표 출력용."""
    rows: List[Dict[str, Any]] = []
    for spec in config.services.values():
        inst = spec.install
        rows.append(
            {
                "name": spec.name,
                "label": spec.label,
                "enabled": spec.enabled,
                "target": inst.target if inst else "none",
                "path": str(inst.path) if inst and inst.path else "",
                "installed": is_installed(spec),
                "events": list(inst.events) if inst else [],
                "log_roots_ok": bool(spec.existing_roots()),
            }
        )
    return rows
