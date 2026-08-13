"""엔드포인트 판별 + 공개 여부 분류 (Domain).

**"지금 어떤 URL 로 LLM 과 통신 중인가" 는 로그에 없다.** 클라이언트의 라우팅 설정,
즉 환경변수와 설정 파일이 유일한 단서다. 훅이 세션 시작 시 그 환경을 찍어 두고
(`hook.routing_env`), 여기서 서비스 스펙에 따라 URL 로 해석한다.

같은 `claude-opus-5` 라도 공식 API 인지 Bedrock 인지 사내 게이트웨이인지에 따라
결제처와 지연이 전혀 다르다. 모델명만으로는 그게 안 보인다.

업로드할 때는 raw URL 을 그대로 보내지 않는다. **알려진 공개 엔드포인트만 이름으로
남기고 나머지는 `self-hosted` 로 뭉갠다** — 사내 게이트웨이 주소는 프로젝트명과 같은
급의 내부 정보다. 관리자가 검증한 호스트는 `leaderboard.public_endpoints` 로 덧붙인다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple
from urllib.parse import urlsplit

from .config import ServiceSpec, dig

try:  # 3.11+ 만 있다. 없으면 TOML 프로브를 조용히 건너뛴다
    import tomllib
except ImportError:  # pragma: no cover - 파이썬 버전 의존
    tomllib = None  # type: ignore[assignment]

SELF_HOSTED = "self-hosted"
UNKNOWN = "unknown"

# 누구나 쓰는 공개 엔드포인트 — 이 호스트는 이름 그대로 올라간다.
PUBLIC_HOSTS: Tuple[str, ...] = (
    "api.anthropic.com",
    "api.openai.com",
    "chatgpt.com",
    "generativelanguage.googleapis.com",
    "api.x.ai",
    "api.deepseek.com",
    "api.mistral.ai",
    "api.groq.com",
    "openrouter.ai",
    "api.together.xyz",
    "api.fireworks.ai",
    "api.moonshot.cn",
    "api.cohere.com",
    "api.perplexity.ai",
    "integrate.api.nvidia.com",
    "api.studio.nebius.ai",
    "opencode.ai",
)

# 호스트가 테넌트마다 다른 서비스 — 서브도메인을 지우고 종류만 남긴다.
#   myco.openai.azure.com → azure-openai   (myco 가 곧 회사 이름이다)
SUFFIX_LABELS: Tuple[Tuple[str, str], ...] = (
    (".openai.azure.com", "azure-openai"),
    (".azure-api.net", "azure-openai"),
    ("-aiplatform.googleapis.com", "vertex"),
    (".amazonaws.com", "bedrock"),
)

# 환경변수 플래그로 확정되는 라벨 (URL 이 아니라 SDK 경로가 바뀌는 경우)
FLAG_LABELS = ("bedrock", "vertex")


def host_of(url: str) -> str:
    """URL → 호스트. 스킴이 없어도(`llm.example.test/v1`) 동작한다."""
    text = str(url or "").strip()
    if not text:
        return ""
    if text in FLAG_LABELS:  # 이미 라벨이면 그대로
        return text
    if "://" not in text:
        text = "https://" + text
    return (urlsplit(text).hostname or "").lower()


def classify(url: str, public: Iterable[str] = ()) -> str:
    """업로드용 라벨. 공개 목록에 있으면 호스트, 아니면 self-hosted.

    `public` 은 관리자가 검증해 추가한 호스트들이다 (기본 목록에 더해진다).
    """
    host = host_of(url)
    if not host or host == UNKNOWN:
        # 판별 실패를 self-hosted 로 넘기면 자체 호스팅 비율이 부풀려진다
        return UNKNOWN
    if host in FLAG_LABELS:
        return host
    allowed = set(PUBLIC_HOSTS) | {str(h).strip().lower() for h in public if str(h).strip()}
    if host in allowed:
        return host
    for suffix, label in SUFFIX_LABELS:
        if host.endswith(suffix):
            return label
    return SELF_HOSTED


def _probe_file(path: Path, key: str) -> Any:
    """JSON / TOML 설정 파일에서 dot-path 를 읽는다."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        if path.suffix == ".toml":
            if tomllib is None:
                return None
            data: Any = tomllib.loads(text)
        else:
            data = json.loads(text)
    except ValueError:
        return None
    return dig(data, key)


def resolve(
    spec: ServiceSpec,
    env: Optional[Dict[str, str]] = None,
    vendor: str = "",
    plan: str = "",
) -> str:
    """이 서비스가 지금 쓰는 엔드포인트. 못 찾으면 빈 문자열.

    우선순위: 플래그(bedrock/vertex) → 환경변수 → 설정 파일 → 서비스 기본값.
    `env` 는 훅이 세션 시작 때 찍어 둔 스냅샷이다 (없으면 데몬 자신의 환경).
    `plan` 은 이미 판정된 요금제 — 구독과 종량제의 목적지가 다른 경우에 쓴다
    (`spec.plan` 은 명시했을 때만 차 있으므로 그것만 보면 안 된다).
    """
    from os import environ

    env = environ if env is None else env  # type: ignore[assignment]
    probe = spec.endpoint_probe or {}

    for name, label in (probe.get("flags") or {}).items():
        value = str(env.get(str(name), "")).strip().lower()
        if value and value not in ("0", "false", "no"):
            return str(label)

    for name in probe.get("env") or []:
        value = str(env.get(str(name), "")).strip()
        if value:
            return value

    path = probe.get("path")
    key = probe.get("key")
    if path and key:
        # opencode 처럼 프로바이더마다 URL 이 다르면 dot-path 에 {vendor} 를 쓴다
        found = _probe_file(Path(str(path)).expanduser(), str(key).replace("{vendor}", vendor))
        if found:
            return str(found)

    default = probe.get("default")
    if isinstance(default, dict):  # 요금제에 따라 목적지가 갈리는 경우(codex)
        return str(default.get(plan or spec.plan) or default.get("unknown") or "")
    return str(default or spec.endpoint or "")


def _demo() -> None:
    """python3 -m tokenmeter.endpoints — 분류 규칙 자가 검증."""
    assert host_of("https://api.anthropic.com/v1/messages") == "api.anthropic.com"
    assert host_of("llm.example.test/v1") == "llm.example.test"
    assert host_of("") == "" and host_of("bedrock") == "bedrock"

    # 공개 엔드포인트는 이름 그대로
    assert classify("https://api.anthropic.com") == "api.anthropic.com"
    assert classify("https://openrouter.ai/api/v1") == "openrouter.ai"
    assert classify("bedrock") == "bedrock"
    # 사내 게이트웨이는 뭉갠다 — 여기가 이 모듈의 존재 이유다
    assert classify("https://llm.example.test/v1") == SELF_HOSTED
    assert classify("https://api.example.test/v1") == SELF_HOSTED
    # 테넌트마다 호스트가 다른 서비스는 종류만
    assert classify("https://mycompany.openai.azure.com") == "azure-openai"
    assert classify("https://bedrock-runtime.us-east-1.amazonaws.com") == "bedrock"
    # 관리자가 검증해 추가하면 그때부터 이름으로 올라간다
    assert classify("https://api.example.test/v1", ["api.example.test"]) == "api.example.test"
    # 판별 실패는 self-hosted 가 아니라 unknown 이다 (섞으면 통계가 거짓말을 한다)
    assert classify("") == UNKNOWN and classify("unknown") == UNKNOWN

    spec = ServiceSpec(name="x", label="x", endpoint_probe={
        "flags": {"USE_BEDROCK": "bedrock"},
        "env": ["X_BASE_URL"],
        "default": "https://api.anthropic.com",
    })
    assert resolve(spec, {}) == "https://api.anthropic.com"
    assert resolve(spec, {"X_BASE_URL": "https://proxy.corp/v1"}) == "https://proxy.corp/v1"
    assert resolve(spec, {"USE_BEDROCK": "1", "X_BASE_URL": "https://proxy.corp/v1"}) == "bedrock"
    assert resolve(spec, {"USE_BEDROCK": "0"}) == "https://api.anthropic.com", "0 은 꺼진 것"

    # 요금제에 따라 목적지가 갈리는 경우 — plan 은 스펙이 아니라 판정 결과로 들어온다
    codex = ServiceSpec(name="c", label="c", endpoint_probe={
        "default": {"subscription": "https://chatgpt.com/backend-api/codex",
                    "api": "https://api.openai.com/v1"}})
    assert host_of(resolve(codex, {}, plan="subscription")) == "chatgpt.com"
    assert host_of(resolve(codex, {}, plan="api")) == "api.openai.com"
    assert resolve(codex, {}) == "", "요금제를 모르면 넘겨짚지 않는다"

    # 설정 파일 프로브 + {vendor} 치환
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "opencode.json"
        cfg.write_text(json.dumps({"provider": {"example-llm": {"options": {
            "baseURL": "https://llm.example.test/v1"}}}}), encoding="utf-8")
        oc = ServiceSpec(name="o", label="o", endpoint_probe={
            "path": str(cfg), "key": "provider.{vendor}.options.baseURL"})
        assert resolve(oc, {}, vendor="example-llm") == "https://llm.example.test/v1"
        assert resolve(oc, {}, vendor="없는프로바이더") == ""
        cfg.write_text("깨진 파일", encoding="utf-8")
        assert resolve(oc, {}, vendor="example-llm") == "", "깨진 설정에도 죽지 않는다"
    print("endpoints.py 자가 검증 통과")


if __name__ == "__main__":
    _demo()
