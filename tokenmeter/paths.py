"""설치 위치와 무관한 TokenMeter 설정·상태 경로."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def data_dir() -> Path:
    override = os.environ.get("TOKENMETER_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "tokenmeter"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "tokenmeter"
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "tokenmeter"


def config_dir() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "tokenmeter"


def _copy_missing(source: Path, destination: Path) -> None:
    if not source.is_dir():
        return
    for item in source.rglob("*"):
        target = destination / item.relative_to(source)
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def migrate_legacy(legacy_root: Path, legacy_config: Path) -> None:
    """옛 체크아웃 데이터와 설정에서 없는 파일만 복사한다. 원본은 남긴다."""
    try:
        state, config = data_dir(), config_dir()
        if not state.exists():
            _copy_missing(legacy_root / "data", state)
        if not config.exists():
            _copy_missing(legacy_config, config)
    except OSError:
        pass
