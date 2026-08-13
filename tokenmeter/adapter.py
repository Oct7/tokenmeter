"""개인 로그를 노출하지 않는 TokenMeter 어댑터 초안 도구."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .config import TOKEN_FIELDS, dig, parse_service

SECRETISH = re.compile(r"key|token|secret|password|credential|auth|cookie", re.IGNORECASE)

_ALIASES = {
    "input": ("input_tokens", "input", "prompt_tokens"),
    "cache_read": ("cache_read_input_tokens", "cached_input_tokens", "cache_read_tokens"),
    "cache_write": ("cache_creation_input_tokens", "cache_write_input_tokens", "cache_write_tokens"),
    "output": ("output_tokens", "output", "completion_tokens", "generated_tokens"),
    "cwd": ("cwd", "working_directory", "workspace"),
    "model": ("model", "model_id", "modelid", "model_name"),
    "session": ("session_id", "sessionid", "session", "conversation_id"),
}


def redact_fixture(value: Any, key: str = "") -> Any:
    if key and SECRETISH.search(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): redact_fixture(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_fixture(item) for item in value]
    if isinstance(value, bool):
        return False
    if isinstance(value, str):
        return ""
    if isinstance(value, (int, float)):
        return 0
    return None


# ponytail: adapter init scans a selected JSONL once; switch to reverse chunk reads if multi-GB logs make this measurable.
def _read_record(log_path: Path) -> Tuple[Optional[Dict[str, Any]], str]:
    if log_path.suffix.lower() == ".jsonl":
        try:
            with log_path.open(encoding="utf-8") as fh:
                lines = deque(fh, maxlen=100)
        except OSError as exc:
            return None, f"로그를 읽을 수 없습니다: {exc}"
        for line in reversed(lines):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value, ""
        return None, "유효한 JSON 객체를 찾지 못했습니다"
    try:
        value = json.loads(log_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"로그를 읽을 수 없습니다: {exc}"
    if not isinstance(value, dict):
        return None, "JSON 로그는 객체여야 합니다"
    return value, ""


def _latest_log(path: Path) -> Tuple[Optional[Path], str]:
    if path.is_file():
        return path, ""
    if not path.is_dir():
        return None, f"로그 경로를 찾을 수 없습니다: {path}"
    try:
        paths = [candidate for candidate in path.rglob("*")
                 if candidate.is_file() and candidate.suffix.lower() in {".json", ".jsonl"}]
        return (max(paths, key=lambda candidate: candidate.stat().st_mtime), "") if paths else (
            None, "JSON 또는 JSONL 로그를 찾지 못했습니다")
    except OSError as exc:
        return None, f"로그를 찾을 수 없습니다: {exc}"


def _paths(value: Any, prefix: str = "") -> Dict[str, str]:
    found: Dict[str, str] = {}
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = str(raw_key)
            path = f"{prefix}.{key}" if prefix else key
            found.setdefault(key.lower(), path)
            found.update(_paths(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.update(_paths(item, f"{prefix}.{index}" if prefix else str(index)))
    return found


def _find_path(paths: Dict[str, str], name: str) -> Optional[str]:
    return next((paths[alias] for alias in _ALIASES[name] if alias in paths), None)


def _service_yaml(name: str, record: Dict[str, Any], log_path: Path) -> str:
    paths = _paths(record)
    fields = {field: _find_path(paths, field) for field in TOKEN_FIELDS}
    context = {field: _find_path(paths, field) for field in ("cwd", "model", "session")}
    fmt = "json" if log_path.suffix.lower() == ".json" else "jsonl"
    pattern = f"**/*.{fmt}"
    spec = {
        "services": {name: {
            "enabled": True, "label": name, "roots": ["~/agent/logs"], "patterns": [pattern],
            "format": fmt, "mode": "choose-delta-or-cumulative", "key": None, "match": {},
            "fields": fields, "context": context, "default_model": "default",
            "install": {"target": "none"},
        }},
    }
    rendered = yaml.safe_dump(spec, allow_unicode=True, sort_keys=False)
    return ("# mode, key, match는 로그 의미를 확인한 뒤 선택하세요.\n" + rendered)


def _atomic_write(path: Path, text: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def init_adapter(name: str, log_path: Path, output: Path) -> Tuple[bool, str]:
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        return False, f"출력 디렉터리가 비어 있지 않습니다: {output}"
    selected, error = _latest_log(log_path)
    if selected is None:
        return False, error
    record, error = _read_record(selected)
    if record is None:
        return False, error
    fixture_text = json.dumps(redact_fixture(record), ensure_ascii=False, indent=2) + "\n"
    service_text = _service_yaml(name, record, selected)
    created: List[Path] = []
    try:
        output.mkdir(parents=True, exist_ok=True)
        for filename, text in (("fixture.json", fixture_text), ("service.yaml", service_text)):
            path = output / filename
            _atomic_write(path, text)
            created.append(path)
    except OSError as exc:
        for path in created:
            try:
                path.unlink()
            except OSError:
                pass
        return False, f"어댑터를 쓸 수 없습니다: {exc}"
    return True, f"어댑터 초안을 만들었습니다: {output}"


def check_adapter(path: Path) -> Tuple[bool, List[str]]:
    try:
        raw = yaml.safe_load((path / "service.yaml").read_text(encoding="utf-8")) or {}
        fixture = json.loads((path / "fixture.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        return False, [f"어댑터를 읽을 수 없습니다: {exc}"]
    services = raw.get("services") if isinstance(raw, dict) else None
    if not isinstance(services, dict) or len(services) != 1:
        return False, ["service.yaml에는 services 아래 서비스가 정확히 하나 있어야 합니다"]
    name, service_raw = next(iter(services.items()))
    try:
        spec = parse_service(str(name), service_raw if isinstance(service_raw, dict) else {})
    except (AttributeError, TypeError, ValueError) as exc:
        return False, [f"서비스 설정이 올바르지 않습니다: {exc}"]
    errors: List[str] = []
    if spec.mode not in {"delta", "cumulative"}:
        errors.append("mode: delta 또는 cumulative 중 하나를 선택하세요")
    for group, fields in (("fields", spec.fields), ("context", spec.context)):
        for field, dot_path in fields.items():
            if dot_path and dig(fixture, dot_path) is None:
                errors.append(f"{group}.{field}: {dot_path} 경로가 fixture.json에 없습니다")
    return not errors, errors or [f"{name}: 구조 검증 통과"]
