"""서비스 레지스트리 로딩 (Infrastructure).

패키지의 services.yaml 을 읽고 ~/.config/tokenmeter/services.yaml 로 덮어쓴다.
서비스 추가는 YAML 편집만으로 끝난다 — 파서/훅 모두 이 스펙을 따라 동작한다.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .paths import config_dir, data_dir, migrate_legacy

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = data_dir()
LIVE_DIR = DATA_DIR / "live"
HISTORY_DIR = DATA_DIR / "history"
STATE_FILE = DATA_DIR / "state.json"
HOURS_FILE = DATA_DIR / "hours.jsonl"  # 시간별 버킷, 한 시간에 한 줄씩 append
PID_FILE = DATA_DIR / "tokenmeter.pid"
LOG_FILE = DATA_DIR / "daemon.log"
TOGGLE_FILE = DATA_DIR / "toggle.json"  # hook.py 도 읽는다 (그쪽은 경로를 따로 계산)

DEFAULT_CONFIG = Path(__file__).with_name("services.yaml")
USER_CONFIG = config_dir() / "services.yaml"
USER_PRICES = USER_CONFIG.parent / "prices.json"  # 모델 단가/컨텍스트 오버라이드
LEGACY_CONFIG_DIR = Path.home() / ".config" / "tokenpet"

TOKEN_FIELDS = ("input", "cache_read", "cache_write", "output")


def ensure_dirs() -> None:
    if not os.environ.get("TOKENMETER_HOME"):
        migrate_legacy(ROOT, LEGACY_CONFIG_DIR)
    for d in (DATA_DIR, LIVE_DIR, HISTORY_DIR):
        d.mkdir(parents=True, exist_ok=True)


def dig(obj: Any, path: Optional[str]) -> Any:
    """'a.b.c' 형태 dot-path 조회. 없으면 None."""
    if not path:
        return None
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if cur is None:
            return None
    return cur


def deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class InstallSpec:
    target: str  # claude_json | opencode_plugin | none
    path: Optional[Path] = None
    events: List[str] = field(default_factory=list)


@dataclass
class ServiceSpec:
    name: str
    label: str
    enabled: bool = True
    roots: List[Path] = field(default_factory=list)
    patterns: List[str] = field(default_factory=lambda: ["**/*.jsonl"])
    format: str = "jsonl"  # jsonl | json
    match: Dict[str, Any] = field(default_factory=dict)
    mode: str = "delta"  # delta | cumulative
    key: Optional[str] = None
    fields: Dict[str, Optional[str]] = field(default_factory=dict)
    input_includes_cache: bool = False
    context: Dict[str, str] = field(default_factory=dict)
    # 지금 컨텍스트에 얼마나 차 있나 (세션 줄의 ctx%). 생략하면 레코드의
    # input+cache_read+cache_write 로 본다 — 누적(cumulative) 서비스는 그 값이
    # 세션 총합이라 반드시 ctx_tokens 를 지정해야 한다.
    ctx_tokens: Optional[str] = None
    ctx_window: Optional[str] = None  # 생략하면 모델 가격표의 window 를 쓴다
    # 이 경로가 참이면 하위 에이전트가 남긴 레코드다. 부모와 같은 sessionId 로 찍히므로
    # 토큰/비용은 그 세션에 합산하되, 컨텍스트는 남의 창이라 쓰지 않고 몫만 따로 센다.
    subagent: Optional[str] = None
    default_model: str = "default"
    vendor: str = ""  # 레코드에서 벤더를 못 찾았을 때 쓸 기본값
    plan: str = ""  # subscription | api | unknown. 비우면 plan_probe 로 판정
    plan_probe: Dict[str, Any] = field(default_factory=dict)
    endpoint: str = ""  # 통신 대상 URL 기본값
    endpoint_probe: Dict[str, Any] = field(default_factory=dict)
    install: Optional[InstallSpec] = None

    def existing_roots(self) -> List[Path]:
        return [r for r in self.roots if r.exists()]


@dataclass
class Config:
    services: Dict[str, ServiceSpec]
    settings: Dict[str, Any]
    enabled: bool = True  # 전체 측정 스위치 (toggle.json)
    overlay_auto: bool = True  # 세션이 시작되면 미터 창을 띄우나

    def enabled_services(self) -> List[ServiceSpec]:
        if not self.enabled:
            return []
        return [s for s in self.services.values() if s.enabled]

    def get(self, name: str) -> Optional[ServiceSpec]:
        return self.services.get(name)

    def setting(self, path: str, default: Any = None) -> Any:
        v = dig(self.settings, path)
        return default if v is None else v


def _expand(p: str) -> Path:
    return Path(os.path.expandvars(str(p))).expanduser()


def parse_service(name: str, raw: Dict[str, Any]) -> ServiceSpec:
    inst_raw = raw.get("install") or None
    install = None
    if inst_raw:
        target = inst_raw.get("target", "none")
        install = InstallSpec(
            target=target,
            path=_expand(inst_raw["path"]) if inst_raw.get("path") else None,
            events=list(inst_raw.get("events") or []),
        )
    return ServiceSpec(
        name=name,
        label=raw.get("label", name),
        enabled=bool(raw.get("enabled", True)),
        roots=[_expand(r) for r in (raw.get("roots") or [])],
        patterns=list(raw.get("patterns") or ["**/*.jsonl"]),
        format=raw.get("format", "jsonl"),
        match=dict(raw.get("match") or {}),
        mode=raw.get("mode", "delta"),
        key=raw.get("key"),
        fields={k: (raw.get("fields") or {}).get(k) for k in TOKEN_FIELDS},
        input_includes_cache=bool(raw.get("input_includes_cache", False)),
        context=dict(raw.get("context") or {}),
        ctx_tokens=raw.get("ctx_tokens"),
        ctx_window=raw.get("ctx_window"),
        subagent=raw.get("subagent"),
        default_model=raw.get("default_model", "default"),
        vendor=str(raw.get("vendor") or ""),
        plan=str(raw.get("plan") or ""),
        plan_probe=dict(raw.get("plan_probe") or {}),
        endpoint=str(raw.get("endpoint") or ""),
        endpoint_probe=dict(raw.get("endpoint_probe") or {}),
        install=install,
    )


def resolve_plan(spec: ServiceSpec) -> str:
    """구독제인지 API 종량제인지 판정한다.

    **로그 레코드에는 결제 형태가 없다.** 클라이언트의 인증 설정이 유일한 단서라
    `plan_probe` 로 그 파일이나 환경변수를 본다. 명시된 `plan` 이 항상 이긴다.

      env    : 이 환경변수 중 하나라도 있으면 if_set, 아니면 else_
      path   : JSON 파일의 key(dot-path) 값을 map 으로 옮긴다
    """
    if spec.plan:
        return spec.plan
    probe = spec.plan_probe or {}
    names = probe.get("env") or []
    if names:
        hit = any(os.environ.get(str(n)) for n in names)
        return str(probe.get("if_set" if hit else "else") or "unknown")
    if not probe.get("path"):
        return "unknown"
    default = str(probe.get("default") or "unknown")
    try:
        raw = json.loads(_expand(str(probe["path"])).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    value = dig(raw, probe.get("key"))
    if value in (None, ""):
        return default
    mapping = probe.get("map") or {}
    return str(mapping.get(str(value), default)) if mapping else str(value)


def load_toggle() -> Dict[str, Any]:
    """런타임 on/off 스위치. **JSON 인 이유는 훅이 읽어야 하기 때문**이다 —
    hook.py 는 yaml 을 import 할 수 없어서 services.yaml 을 볼 수 없다.
    깨져 있으면 켜진 것으로 본다 (측정이 조용히 멈추는 것보다 낫다)."""
    try:
        data = json.loads(TOGGLE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_toggle(data: Dict[str, Any]) -> None:
    TOGGLE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOGGLE_FILE.with_name(TOGGLE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, TOGGLE_FILE)


def load_config() -> Config:
    raw: Dict[str, Any] = {}
    if DEFAULT_CONFIG.exists():
        raw = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8")) or {}
    if USER_CONFIG.exists():
        user = yaml.safe_load(USER_CONFIG.read_text(encoding="utf-8")) or {}
        raw = deep_merge(raw, user)
    services = {
        name: parse_service(name, spec or {})
        for name, spec in (raw.get("services") or {}).items()
    }
    # 토글은 yaml 위에 덮어쓴다 — `enabled_services()` 하나만 거치면 워처·설치·표가
    # 전부 따라오므로, 서비스별 스위치를 여기서 spec 에 반영해 두는 게 가장 싸다.
    toggle = load_toggle()
    per_service = toggle.get("services")
    if isinstance(per_service, dict):
        for name, spec in services.items():
            if name in per_service:
                spec.enabled = bool(per_service[name])
    return Config(
        services=services,
        settings=raw.get("settings") or {},
        enabled=toggle.get("enabled", True) is not False,
        overlay_auto=toggle.get("overlay", True) is not False,
    )
