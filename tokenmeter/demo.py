"""데모 — 가짜 델타를 흘려 미터 게이지와 랭킹 갱신을 확인한다.

실제 `data/state.json` 에 반영되므로 되돌리려면 `tokenmeter reset --yes`.
"""

from __future__ import annotations

import random
import sys
import threading
import time
from pathlib import Path

if __package__ in (None, ""):  # `python3 tokenmeter/demo.py` 로 직접 실행한 경우
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tokenmeter.config import load_config
    from tokenmeter.hook import daemon_pid
    from tokenmeter.meter import Meter, TokenDelta
    from tokenmeter.overlay import run_overlay
else:
    from .config import load_config
    from .hook import daemon_pid
    from .meter import Meter, TokenDelta
    from .overlay import run_overlay

MODELS = ("claude-opus-5", "claude-sonnet-4.6", "gpt-5.6-sol")
PROJECTS = ("tokenmeter", "demo-project")

_stop = threading.Event()


def _fake_delta() -> TokenDelta:
    """캐시 위주 턴 70% / 큰 입출력 턴 30%."""
    heavy = random.random() < 0.3
    return TokenDelta(
        input_tokens=random.randint(1000, 5000) if heavy else random.randint(50, 400),
        cache_read=random.randint(0, 500) if heavy else random.randint(2000, 15000),
        cache_write=random.randint(500, 2000) if heavy else random.randint(0, 300),
        output_tokens=random.randint(500, 2000) if heavy else random.randint(100, 800),
        model=random.choice(MODELS),
        service="demo",
        project=random.choice(PROJECTS),
    )


def _feeder(meter: Meter, interval: float) -> None:
    while not _stop.is_set():
        delta = _fake_delta()
        totals = meter.ingest(delta)
        print(f"  +{delta.total:,} 토큰 ({delta.model})  →  누적 ${totals['cost_usd']:.4f}")
        _stop.wait(random.uniform(interval * 0.6, interval * 1.6))


def main() -> int:
    # 기본 간격은 미터가 중간쯤에서 놀도록 잡았다 (턴당 ~700 출력 토큰 / 5초 ≈ 140 tok/s)
    interval = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
    pid = daemon_pid()
    if pid:
        # 데모는 진짜 state.json 에 가짜 델타를 쓴다 → 데몬과 같이 돌면 서로의 누적치를 덮어쓴다
        print(f"✗ 데몬(pid {pid})이 실행 중입니다 — 먼저 멈추세요: kill {pid}")
        return 1
    meter = Meter(load_config())
    print("데모 시작 — 가짜 토큰을 흘립니다. (Ctrl+C 로 종료)")
    print("되돌리기: tokenmeter reset --yes")
    threading.Thread(target=_feeder, args=(meter, interval), daemon=True).start()
    try:
        if not run_overlay(meter):
            print("오버레이를 띄울 수 없어 콘솔에만 출력합니다.")
            while True:
                time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        _stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
