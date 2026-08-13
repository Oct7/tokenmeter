"""모델별 토큰 가격표 (2026-08 기준, USD per 1M tokens) + 벤더 판별 (Domain).

가격표에 없는 모델은 `default` 단가로 조용히 계산된다 — 새 모델이 나오면 비용이
소리 없이 틀린다. 그래서 (1) `known()` 으로 모르는 모델을 짚어내고
(2) `~/.config/tokenmeter/prices.json` 으로 사용자가 단가를 직접 못 박을 수 있게 한다.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

from .config import USER_PRICES

# 모델명 조각 → API 벤더. 로그에서 벤더를 직접 못 찾았을 때의 마지막 폴백이다.
# 정규화 전 원본 이름으로 찾는다 ('nemotron-3-ultra-free' 는 PRICES 에 없어도 nvidia 다).
VENDORS: Tuple[Tuple[str, str], ...] = (
    ("claude", "anthropic"),
    ("gpt", "openai"),
    ("o3-", "openai"),
    ("o4-", "openai"),
    ("codex", "openai"),
    ("gemini", "google"),
    ("deepseek", "deepseek"),
    ("grok", "xai"),
    ("llama", "meta"),
    ("mistral", "mistral"),
    ("mixtral", "mistral"),
    ("qwen", "alibaba"),
    ("nemotron", "nvidia"),
    ("command-r", "cohere"),
    ("kimi", "moonshot"),
)


def vendor_of(model: str) -> str:
    """모델명에서 벤더를 추론한다. 모르면 'unknown'."""
    n = (model or "").lower().replace("_", "-")
    for frag, vendor in VENDORS:
        if frag in n:
            return vendor
    return "unknown"

# (input, cache_read, cache_write, output)  per 1M tokens + window(컨텍스트 창 토큰)
# cache_write 기본값은 input * 1.25 (Anthropic 스타일). 모르는 모델은 보수적으로 처리.
# window 는 세션 줄의 ctx% 분모다. 0 이면 '모름' 이라 ctx% 를 아예 안 그린다.
PRICES: Dict[str, Dict[str, float]] = {
    # Anthropic
    "claude-fable-5": {"input": 10.0, "cache_read": 1.0, "cache_write": 12.5, "output": 50.0,
                       "window": 200_000},
    "claude-opus-5": {"input": 5.0, "cache_read": 0.5, "cache_write": 6.25, "output": 25.0,
                      "window": 200_000},
    "claude-opus-4.8": {"input": 5.0, "cache_read": 0.5, "cache_write": 6.25, "output": 25.0,
                        "window": 200_000},
    "claude-sonnet-5": {"input": 2.0, "cache_read": 0.2, "cache_write": 2.5, "output": 10.0,
                        "window": 200_000},
    "claude-sonnet-4.6": {"input": 3.0, "cache_read": 0.3, "cache_write": 3.75, "output": 15.0,
                          "window": 200_000},
    "claude-haiku-4.5": {"input": 1.0, "cache_read": 0.1, "cache_write": 1.25, "output": 5.0,
                         "window": 200_000},
    # OpenAI
    "gpt-5.6-sol": {"input": 5.0, "cache_read": 0.5, "cache_write": 5.0, "output": 30.0,
                    "window": 400_000},
    "gpt-5.6-terra": {"input": 2.0, "cache_read": 0.2, "cache_write": 2.0, "output": 12.0,
                      "window": 400_000},
    "gpt-5.6-luna": {"input": 0.2, "cache_read": 0.02, "cache_write": 0.2, "output": 1.2,
                     "window": 400_000},
    "gpt-5.4": {"input": 2.5, "cache_read": 0.25, "cache_write": 2.5, "output": 15.0,
                "window": 400_000},
    # DeepSeek
    "deepseek-v4-flash": {"input": 0.14, "cache_read": 0.014, "cache_write": 0.14, "output": 0.28,
                          "window": 128_000},
    "deepseek-v4-pro": {"input": 0.435, "cache_read": 0.0435, "cache_write": 0.435, "output": 0.87,
                        "window": 128_000},
    # xAI
    "grok-4.5": {"input": 2.0, "cache_read": 0.5, "cache_write": 2.0, "output": 6.0,
                 "window": 256_000},
    # Fallback — 모르는 모델. 여기로 떨어지면 비용은 '추정' 이다 (known() 이 False)
    "default": {"input": 3.0, "cache_read": 0.3, "cache_write": 3.75, "output": 15.0,
                "window": 0},
}

FIELDS = ("input", "cache_read", "cache_write", "output", "window")
LONG_WINDOW = 1_000_000     # 롱컨텍스트 변형의 창 (claude-opus-5[1m] 등)

# ── 사용자 단가 오버라이드 (~/.config/tokenmeter/prices.json) ─────────────────
# {"claude-opus-5[1m]": {"input": 6, "output": 22.5, "window": 1000000}}
# 적어 넣은 항목만 이기고, 빠진 항목은 기본 표에서 가져온다. 파일이 바뀌면
# 데몬을 재시작하지 않아도 다음 계산부터 반영된다 (mtime 만 본다).
_OVER: Dict[str, Dict[str, float]] = {}
_OVER_MTIME: float = -1.0


def _key(name: Any) -> str:
    return str(name or "").strip().lower().replace("_", "-").replace(" ", "-")


def overrides() -> Dict[str, Dict[str, float]]:
    global _OVER, _OVER_MTIME
    try:
        mtime = USER_PRICES.stat().st_mtime
    except OSError:
        _OVER, _OVER_MTIME = {}, -1.0
        return _OVER
    if mtime == _OVER_MTIME:
        return _OVER
    try:
        raw = json.loads(USER_PRICES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    book: Dict[str, Dict[str, float]] = {}
    if isinstance(raw, dict):
        for name, values in raw.items():
            if not isinstance(values, dict):
                continue
            clean = {}
            for field in FIELDS:
                try:
                    clean[field] = float(values[field])
                except (KeyError, TypeError, ValueError):
                    continue  # 적어 넣은 항목만 이긴다
            if clean:
                book[_key(name)] = clean
    _OVER, _OVER_MTIME = book, mtime
    return _OVER


def save_overrides(book: Dict[str, Dict[str, float]]) -> None:
    USER_PRICES.parent.mkdir(parents=True, exist_ok=True)
    USER_PRICES.write_text(
        json.dumps(book, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def set_price(model: str, values: Dict[str, float]) -> Dict[str, float]:
    """모델 하나의 단가/컨텍스트를 못 박는다. 준 항목만 덮어쓴다."""
    book = dict(overrides())
    entry = dict(book.get(_key(model)) or {})
    entry.update({k: float(v) for k, v in values.items() if k in FIELDS and v is not None})
    book[_key(model)] = entry
    save_overrides(book)
    return entry


def unset_price(model: str) -> bool:
    book = dict(overrides())
    if book.pop(_key(model), None) is None:
        return False
    save_overrides(book)
    return True


def normalize_model(name: str) -> str:
    if not name:
        return "default"
    n = name.lower().replace("_", "-").replace(" ", "-")
    for key in PRICES:
        if key in n or n in key:
            return key
    # partial matches
    if "fable" in n:
        return "claude-fable-5"
    if "opus" in n:
        return "claude-opus-4.8"
    if "sonnet" in n:
        return "claude-sonnet-4.6"
    if "haiku" in n:
        return "claude-haiku-4.5"
    if "luna" in n:
        return "gpt-5.6-luna"
    if "terra" in n:
        return "gpt-5.6-terra"
    if "sol" in n or "gpt-5.6" in n:
        return "gpt-5.6-sol"
    if "deepseek" in n and "flash" in n:
        return "deepseek-v4-flash"
    if "deepseek" in n:
        return "deepseek-v4-pro"
    if "grok" in n:
        return "grok-4.5"
    return "default"


def has_override(model: str) -> bool:
    """사용자가 직접 못 박은 모델인가 (표에서 '출처' 를 가른다)."""
    return _key(model) in overrides()


def known(model: str) -> bool:
    """이 모델의 단가를 실제로 아는가. False 면 비용은 default 단가 추정치다."""
    return _key(model) in overrides() or normalize_model(model) != "default"


def prices_for(model: str) -> Dict[str, float]:
    """사용자 오버라이드가 기본 표를 이긴다. 빠진 항목은 기본 표에서 채운다."""
    base = PRICES[normalize_model(model)]
    over = overrides().get(_key(model))
    return {**base, **over} if over else base


def cost_usd(
    model: str,
    input_tokens: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
    output_tokens: int = 0,
) -> float:
    p = prices_for(model)
    return (
        input_tokens * p["input"]
        + cache_read * p["cache_read"]
        + cache_write * p["cache_write"]
        + output_tokens * p["output"]
    ) / 1_000_000


def cache_savings(model: str, cache_read: int) -> float:
    """캐시 읽기로 아낀 돈 — 같은 토큰을 캐시 없이 입력으로 냈다면 들었을 차액."""
    p = prices_for(model)
    return max(0.0, cache_read * (p["input"] - p["cache_read"])) / 1_000_000


def context_window(model: str, observed: int = 0) -> int:
    """이 모델의 컨텍스트 창(토큰). 모르면 0 — ctx% 를 그리지 않는다는 뜻이다.

    observed(지금 실제로 차 있는 토큰)가 창을 넘으면 그 창이 아니다 — 롱컨텍스트로
    붙은 세션이다. Claude Code 로그의 model 은 '[1m]' 없이 찍히므로, 200k 창에
    400k 프롬프트가 들어갔다는 관측 자체가 유일한 단서다 (없으면 계속 100% 로 보인다).
    """
    n = _key(model)
    over = overrides().get(n) or {}
    if over.get("window"):
        return int(over["window"])
    if "[1m]" in n or n.endswith("-1m"):  # 롱컨텍스트 변형 (claude-opus-5[1m])
        return LONG_WINDOW
    win = int(PRICES[normalize_model(model)].get("window", 0))
    return LONG_WINDOW if win and observed > win else win
