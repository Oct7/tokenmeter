"""엔트리 포인트 — 감시 + 오버레이를 한 번에 (`tokenmeter daemon` 과 동일)."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):  # `python3 tokenmeter/main.py` 로 직접 실행한 경우
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tokenmeter.cli import main as cli_main
else:
    from .cli import main as cli_main


def main() -> int:
    return cli_main(["daemon", *sys.argv[1:]])


if __name__ == "__main__":
    sys.exit(main())
