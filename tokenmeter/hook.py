#!/usr/bin/env python3
"""TokenMeter 훅 엔트리 (Presentation).

    python3 /abs/tokenmeter/tokenmeter/hook.py <service> <event>

에이전트 프로세스 안에서 매 세션마다 실행되는 코드라 다음 세 가지가 절대 규칙이다.

1. **표준 라이브러리만** import 한다 (yaml/typer/rich/PyQt 금지). 기동 시간이 곧 비용.
2. **stdout 에 아무것도 출력하지 않는다.** Claude Code 는 SessionStart 훅의 stdout 을
   그대로 컨텍스트에 주입한다. 진단 출력은 TOKENMETER_DEBUG=1 일 때만 stderr 로.
3. 무슨 일이 있어도 **exit 0**. 훅이 에이전트를 막는 일은 없어야 한다.

config.py 를 import 하지 못하므로(yaml 의존) 경로 상수만 여기서 다시 계산한다.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import select
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
try:
    from tokenmeter.paths import data_dir, migrate_legacy
    from tokenmeter.views import project_key
except ImportError:  # 소스 체크아웃에서 이 파일을 직접 실행한 경우
    sys.path.insert(0, str(ROOT))
    from tokenmeter.paths import data_dir, migrate_legacy
    from tokenmeter.views import project_key

DATA_DIR = data_dir()
LIVE_DIR = DATA_DIR / "live"
PID_FILE = DATA_DIR / "tokenmeter.pid"
LOCK_FILE = DATA_DIR / "daemon.lock"
LOG_FILE = DATA_DIR / "daemon.log"
TOGGLE_FILE = DATA_DIR / "toggle.json"  # config.py 와 같은 파일 (여기선 yaml 을 못 쓴다)

# 세션 종료로 간주해 라이브 파일을 지우는 이벤트
STOP_EVENTS = {"SessionEnd", "session.deleted"}
CHECK_EVENTS = {
    "PermissionRequest", "permission.asked", "permission.v2.asked",
    "question.asked", "question.v2.asked",
}
WAIT_EVENTS = {"Stop", "session.idle"}
WORK_EVENTS = {
    "SessionStart", "UserPromptSubmit", "session.created", "permission.replied",
    "permission.v2.replied", "question.replied", "question.v2.replied",
}
ATTENTION_NOTIFICATIONS = {"permission_prompt", "idle_prompt", "elicitation_dialog"}
# 데몬 기동 경합 방지용 락이 이 시간을 넘기면 스테일로 보고 탈취한다
LOCK_STALE_SECONDS = 60.0
# stdin 이 열린 채 아무것도 안 오는 경우를 대비한 대기 상한
STDIN_TIMEOUT = 0.2

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

# 이 세션이 **어떤 URL 로 LLM 과 통신하는지** 를 결정하는 환경변수들.
# 훅은 에이전트 프로세스의 자식이라 여기 찍히는 값이 곧 그 세션의 라우팅 설정이다
# (데몬은 오래 살면서 여러 세션을 걸치므로 데몬의 환경으로는 세션별 구분이 안 된다).
_ROUTING_ENV = re.compile(
    r"(_BASE_URL|_API_BASE|_ENDPOINT)$|^(HTTP_PROXY|HTTPS_PROXY|ALL_PROXY)$|USE_BEDROCK|USE_VERTEX",
    re.IGNORECASE,
)
# 값에 비밀이 섞일 수 있는 이름은 통째로 제외한다. 훅이 토큰을 파일에 쓰면 안 된다.
_SECRETISH = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH", re.IGNORECASE)


def routing_env() -> dict:
    """라우팅에 영향을 주는 환경변수만 추린다 (URL·프록시·SDK 플래그)."""
    return {
        k: v[:200]
        for k, v in os.environ.items()
        if _ROUTING_ENV.search(k) and not _SECRETISH.search(k) and v.strip()
    }


def _debug(msg: str) -> None:
    if os.environ.get("TOKENMETER_DEBUG") == "1":
        print(f"[tokenmeter-hook] {msg}", file=sys.stderr)


def _safe(value: str) -> str:
    """파일명에 그대로 못 쓰는 문자(/ 공백 등)를 _ 로 치환."""
    return _UNSAFE.sub("_", str(value))[:120] or "unknown"


def _read_payload() -> dict:
    """stdin 의 JSON 을 best-effort 로 읽는다. 비어 있거나 깨져도 {} 를 준다."""
    try:
        if sys.stdin is None or sys.stdin.closed or sys.stdin.isatty():
            return {}
        # 호스트가 payload 만 쓰고 stdin 을 안 닫으면 read() 는 EOF 까지 막힌다.
        # 기다리는 시간뿐 아니라 읽는 시간까지 통째로 상한을 건다.
        fd = sys.stdin.fileno()
        deadline = time.time() + STDIN_TIMEOUT
        chunks = []
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                break
            if not select.select([fd], [], [], remain)[0]:
                break
            chunk = os.read(fd, 65536)
            if not chunk:  # EOF
                break
            chunks.append(chunk)
        raw = b"".join(chunks).decode("utf-8", "replace")
        data = json.loads(raw) if raw.strip() else {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001 - 훅은 어떤 입력에도 죽으면 안 된다
        _debug(f"stdin 파싱 실패: {exc!r}")
        return {}


def _pick(payload: dict, *keys: str) -> str:
    for k in keys:
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _resolve_cwd(payload: dict) -> str:
    """cwd 는 stdin JSON → TOKENMETER_CWD → os.getcwd() 순으로 결정한다."""
    return (
        _pick(payload, "cwd", "workspace", "project_dir")
        or os.environ.get("TOKENMETER_CWD", "").strip()
        or os.getcwd()
    )


def attention_signal(service: str, event: str, payload: dict) -> str:
    """이벤트를 UI가 쓸 수 있는 최소 주의 신호로 정규화한다."""
    if event == "Notification":
        return "check" if _pick(payload, "notification_type") in ATTENTION_NOTIFICATIONS else ""
    if service == "codex" and event == "PermissionRequest":
        reviewer = _pick(payload, "approvals_reviewer", "approval_reviewer").lower()
        if reviewer in {"auto_review", "guardian", "guardian_subagent"}:
            return "working"
    if event in CHECK_EVENTS:
        return "check"
    if event in WAIT_EVENTS:
        return "waiting"
    return "working" if event in WORK_EVENTS else ""


def _explicit_session_id(payload: dict, argv_session_id: str = "") -> str:
    """훅이 실제로 받은 세션 id. cwd 해시 폴백은 넣지 않는다."""
    return argv_session_id or _pick(payload, "session_id", "sessionId", "id")


def _resolve_session_id(payload: dict, cwd: str, argv_session_id: str = "") -> str:
    """세션 식별자. 훅이 아무 정보도 못 받으면 cwd 해시로 대체한다."""
    sid = _explicit_session_id(payload, argv_session_id)
    if sid:
        return sid
    # ponytail: cwd 해시 폴백 — OpenCode 플러그인처럼 세션 id 를 못 주는 경우용.
    #           같은 디렉토리의 동시 세션은 한 파일을 공유한다. 필요해지면 ppid 를 섞을 것.
    return "cwd-" + hashlib.md5(cwd.encode("utf-8", "replace")).hexdigest()[:8]


def _service_for_hook(service: str, session_id: str) -> str:
    """Claude-compat 훅이 진짜 Grok 세션을 대행할 때만 서비스명을 고친다.

    Grok CLI 는 ~/.claude/settings.json 훅도 실행한다. 인자만 믿으면 grok
    세션이 claude-code 라이브 파일로 남는다. 다만 GROK_SESSION_ID 가 셸에
    남아 있는 것만으로는 Claude Code 세션을 가로채지 않는다.
    """
    if service != "claude-code" or not session_id:
        return service
    grok_sid = (os.environ.get("GROK_SESSION_ID") or "").strip()
    return "grok" if grok_sid and session_id == grok_sid else service


def live_path(service: str, session_id: str) -> Path:
    return LIVE_DIR / f"{_safe(service)}__{_safe(session_id)}.json"


def _write_live(
    path: Path, service: str, session_id: str, cwd: str, event: str, model: str, attention: str
) -> None:
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    now = time.time()
    try:
        started_at = float(existing.get("started_at"))
    except (TypeError, ValueError):
        started_at = 0.0
    try:
        attention_at = float(existing.get("attention_at"))
    except (TypeError, ValueError):
        attention_at = 0.0
    record = {
        "service": service,
        "session_id": session_id,
        "project": project_key(cwd),
        "cwd": cwd,
        "model": model or _pick(existing, "model"),
        "started_at": started_at or now,
        "event": event,
        "event_at": now,
        "routing_env": existing.get("routing_env") if isinstance(existing.get("routing_env"), dict) else routing_env(),
        "attention": attention or _pick(existing, "attention"),
    }
    if attention:
        record["attention_at"] = now
    elif attention_at:
        record["attention_at"] = attention_at
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)  # 데몬이 반쯤 쓰인 파일을 읽지 않도록 원자적 교체


def daemon_pid() -> int:
    """살아 있는 데몬의 pid, 없으면 0. (CLI 도 이 판정을 그대로 쓴다)

    pid 존재 여부만 보면 크래시로 남은 pid 가 재사용됐을 때 데몬이 영영 안 뜬다
    (자가 치유 경로가 없다). 커맨드라인까지 확인해야 한다.
    """
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0
    if pid <= 0:
        return 0
    try:
        out = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2,
        ).stdout
    except Exception:  # noqa: BLE001 - ps 가 없으면 존재 확인만으로 물러선다
        try:
            os.kill(pid, 0)
            return pid
        except OSError:
            return 0
    return pid if ("tokenmeter.cli" in out and "daemon" in out) else 0


def _take_lock() -> bool:
    """동시에 뜬 훅들이 데몬을 여러 개 띄우지 않도록 한 놈만 통과시킨다."""
    for _ in range(2):
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                age = time.time() - LOCK_FILE.stat().st_mtime
            except OSError:
                continue  # 방금 사라졌으면 한 번 더 시도
            if age < LOCK_STALE_SECONDS:
                return False
            try:
                LOCK_FILE.unlink()
            except OSError:
                return False
        except OSError as exc:
            _debug(f"락 실패: {exc!r}")
            return False
    return False


def ensure_daemon() -> bool:
    """데몬이 죽어 있으면 백그라운드로 띄운다. 띄웠으면 True."""
    if os.environ.get("TOKENMETER_NO_DAEMON") == "1":
        return False
    if daemon_pid():
        return False
    if not _take_lock():
        return False  # 다른 훅이 지금 띄우는 중
    log = subprocess.DEVNULL
    try:
        log = open(LOG_FILE, "a", encoding="utf-8")  # noqa: SIM115 - 자식이 물고 간다
    except OSError:
        pass
    try:
        subprocess.Popen(
            [sys.executable, "-m", "tokenmeter.cli", "daemon"],
            # cwd 대신 PYTHONPATH 로 패키지를 찾게 한다. cwd 에 의존하면 체크아웃
            # 밖(pip/uv 설치)에서 못 돌고, 데몬이 에이전트의 프로젝트 디렉토리를
            # 붙들지도 않는다. 설치본에서는 sys.path 에 이미 있어 무해하게 겹친다.
            env={**os.environ, "PYTHONPATH": os.pathsep.join(
                p for p in (str(ROOT), os.environ.get("PYTHONPATH", "")) if p
            )},
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,  # 에이전트가 죽어도 데몬은 살아남는다
            close_fds=True,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        _debug(f"데몬 기동 실패: {exc!r}")
        return False
    finally:
        if log is not subprocess.DEVNULL:
            log.close()


def is_off(service: str) -> bool:
    """`tokenmeter off` 로 꺼져 있나 (전체 또는 이 서비스만).

    깨졌거나 없으면 켜진 것으로 본다 — 파일 하나 때문에 측정이 조용히 멈추는 쪽이 나쁘다.
    """
    try:
        data = json.loads(TOGGLE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    if data.get("enabled") is False:
        return True
    per_service = data.get("services")
    return isinstance(per_service, dict) and per_service.get(service) is False


def main(argv: list[str]) -> int:
    if os.environ.get("TOKENMETER_DISABLE") == "1":
        return 0

    service = argv[1] if len(argv) > 1 else "unknown"
    event = argv[2] if len(argv) > 2 else ""
    payload = _read_payload()
    event = event or _pick(payload, "hook_event_name") or "SessionStart"
    cwd = _resolve_cwd(payload)
    session_id = _resolve_session_id(payload, cwd, argv[3] if len(argv) > 3 else "")
    service = _service_for_hook(service, session_id)
    if is_off(service):
        return 0

    if not os.environ.get("TOKENMETER_HOME"):
        migrate_legacy(ROOT, Path.home() / ".config" / "tokenpet")
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = live_path(service, session_id)

    if event in STOP_EVENTS:
        path.unlink(missing_ok=True)
    else:
        _write_live(
            path, service, session_id, cwd, event, _pick(payload, "model"),
            attention_signal(service, event, payload),
        )
        # SessionStart 에서만 띄우면 데몬이 죽었을 때(크래시·오버레이 종료·강제 kill)
        # 다음 세션이 시작될 때까지 측정이 통째로 빈다. 살아 있으면 daemon_pid() 검사에서
        # 곧장 물러나므로 매 이벤트마다 불러도 비용이 없다.
        ensure_daemon()

    _debug(f"{service}/{event} sid={session_id} → {path}")
    return 0


if __name__ == "__main__":
    try:
        main(sys.argv)
    except Exception as exc:  # noqa: BLE001 - 어떤 경우에도 에이전트를 막지 않는다
        _debug(f"무시된 예외: {exc!r}")
    sys.exit(0)
