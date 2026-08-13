# 새 서비스 붙이기 — 실전 가이드

TokenMeter 에 **아직 지원하지 않는 AI 코딩 도구**를 붙이는 방법입니다.
파이썬 코드는 한 줄도 건드리지 않습니다. `tokenmeter/services.yaml` 블록 하나가 전부입니다.

> 개인용으로만 쓸 거면 `~/.config/tokenmeter/services.yaml` 에 같은 구조로 쓰면 됩니다.
> 저장소의 기본 설정 위에 깊은 병합(deep merge)되므로, 기존 서비스의 필드 하나만 덮어쓰는 것도 됩니다.

## 어댑터로 초안 만들기 (선택)

로그 구조를 먼저 익히려면 다음 명령으로 익명화된 초안을 만듭니다.

```bash
tokenmeter adapter init gemini-cli --log ~/.gemini/tmp
tokenmeter adapter check ./gemini-cli-adapter
```

`init`은 최신 JSON/JSONL 레코드를 읽어 현재 디렉터리에 정확히 두 파일만 생성합니다.
`fixture.json`에는 문자열·숫자·불리언 값을 남기지 않고, `service.yaml`에는 발견한
dot-path만 채웁니다. `mode`는 `choose-delta-or-cumulative`로 남겨지므로 실제 로그가
증분인지 누적인지 확인해 `delta` 또는 `cumulative`로 직접 바꿔야 합니다. `key`와
`match`도 같은 이유로 미해결 상태입니다. 비어 있지 않은 대상 디렉터리는 덮어쓰지 않습니다.

프롬프트·응답·툴 명령·파일명·경로는 어댑터 fixture와 공개 출력에 저장하거나 전송하지
않습니다. 어댑터는 측정 설정 초안일 뿐 로그 원문을 복제하지 않습니다.

---

## 0. 먼저 알아야 할 것 — 두 축은 독립이다

| 축 | 하는 일 | 없으면 |
|---|---|---|
| **로그 감시** (`roots`/`fields`/…) | 토큰을 **잰다** | 아무것도 측정되지 않음 |
| **훅** (`install`) | 세션이 시작/종료될 때 데몬을 **켜고 끈다** | 측정은 되지만 데몬을 손으로 띄워야 함 |

훅을 붙일 방법이 없는 도구라도 `install: {target: none}` 으로 **로그 감시만** 붙일 수 있습니다.
데몬이 한 번 떠 있으면 설정된 모든 서비스를 동시에 감시하므로, Claude Code 훅 하나만 설치돼 있어도
그 데몬이 다른 도구의 로그까지 같이 먹습니다.

---

## 1단계 — 로그 파일 찾기

대부분의 CLI 에이전트는 홈 디렉토리 어딘가에 세션 로그를 남깁니다.
도구를 한 번 실행한 직후 **최근에 수정된 파일**을 찾는 게 가장 빠릅니다.

```bash
# 방금 3분 안에 바뀐 JSON/JSONL 을 홈에서 훑는다
find ~ -maxdepth 6 \( -name "*.jsonl" -o -name "*.json" \) -mmin -3 2>/dev/null | head -20

# 흔한 위치
ls ~/.<도구>/ ~/.config/<도구>/ ~/.local/share/<도구>/
```

찾았으면 **토큰 숫자가 실제로 들어 있는지** 먼저 확인합니다. 없으면 그 도구는 로그로 잴 수 없습니다.

```bash
grep -l -i "token\|usage" <찾은경로>/*.jsonl | head
```

---

## 2단계 — 레코드 한 줄을 뜯어보기

`format` 을 여기서 정합니다.

- 한 파일에 한 줄씩 계속 append 된다 → **`format: jsonl`**
- 메시지 하나가 파일 하나다 → **`format: json`**

```bash
# jsonl 이면: 토큰이 들어 있는 마지막 줄의 구조를 본다
python3 - <<'EOF'
import json
last = None
for line in open("/경로/세션.jsonl", errors="ignore"):
    try: o = json.loads(line)
    except: continue
    if "token" in line.lower(): last = o
print(json.dumps(last, indent=1, ensure_ascii=False)[:1500])
EOF

# json 이면: 그냥 파일 하나를 편다
python3 -m json.tool /경로/메시지.json | head -40
```

여기서 다음 **dot-path** 들을 받아 적습니다. (`a.b.c` = `obj["a"]["b"]["c"]`)

| 받아 적을 것 | 예 | 없으면 |
|---|---|---|
| 이 레코드가 "토큰 레코드"임을 구분하는 필드 → `match` | `type: assistant` | 전부 매칭 |
| 토큰 4종 → `fields` | `message.usage.input_tokens` | 그 종류는 0 |
| 중복 제거용 고유 id → `key` | `uuid` | 파일+줄번호가 키 |
| 작업 디렉토리 → `context.cwd` | `cwd` | 프로젝트 귀속 안 됨 |
| 모델 이름 → `context.model` | `message.model` | `default_model` 사용 |
| **세션 id** → `context.session` | `sessionId` | **세션 수 비교에서 빠짐** |
| **벤더** → `context.vendor` | `providerID` | `vendor:` → 모델명 추론 순 |
| 추론 강도 → `context.effort` | `effort` | 세션 줄의 effort 칸이 빈다 |

`context` 값들은 **같은 레코드에 없어도 됩니다.** 같은 파일의 다른 줄
(예: 세션 헤더)에 있으면 파일 단위로 기억해뒀다가 씁니다.

여기에 더해 **요금제**(`plan` / `plan_probe`)와 **엔드포인트**(`endpoint` /
`endpoint_probe`)를 정해야 구독 vs 종량제, 공식 API vs 사내 게이트웨이 비교에 들어갑니다.
둘 다 로그에 없어서 인증·라우팅 설정(환경변수, 설정 파일)을 봅니다 — README 의
「무엇을 나눠 재나」를 보세요. 전부 `doctor` 가 판정 결과를 찍어 줍니다.

---

## 3단계 — `mode` 고르기 (가장 많이 틀리는 곳)

토큰 레코드를 **시간순으로 2~3개** 나란히 놓고 보세요.

```
1번째 레코드: input=24254   ← 2번째가 1번째보다 크고, 누적처럼 보이는가?
2번째 레코드: input=50327
3번째 레코드: input=1372338
```

| 관찰 | `mode` | 이유 |
|---|---|---|
| 값이 매 턴 **처음부터 다시** 센다 (독립적인 작은 수) | `delta` | 값 자체가 증가분 |
| 값이 계속 **커지기만** 한다 | `cumulative` | 직전값과의 차이만 반영해야 함 |
| 같은 레코드가 **여러 번 다시 기록**된다 (0 → 실제값) | `cumulative` | 마지막 값만 반영됨 |

`cumulative` 를 골랐다면 `key` 를 무엇으로 할지도 정해야 합니다.

- **파일(=세션) 단위로 누적**된다 → `key: null` (파일 경로가 키)
- **레코드(=메시지) 단위로 갱신**된다 → `key: <고유 id 의 dot-path>`

> 확신이 안 서면 **`cumulative` 가 더 안전합니다.** 이벤트가 중복 기록돼도 이중계상되지 않습니다.
> 반대로 `delta` 를 잘못 고르면 조용히 값이 몇 배로 부풀어 오릅니다.

---

## 4단계 — YAML 블록 쓰기

```yaml
services:
  my-agent:                       # 서비스 id (CLI 인자로 쓰임)
    enabled: true
    label: My Agent               # 표에 표시될 이름
    roots:                        # 없는 경로는 자동으로 무시된다. 여러 개 나열 가능
      - "~/.my-agent/sessions"
      - "~/.local/share/my-agent"
    patterns: ["**/*.jsonl"]
    format: jsonl
    match:                        # 모든 조건이 일치하는 레코드만 사용 (생략하면 전부)
      type: assistant
    mode: delta
    key: uuid                     # 중복 제거 기준. 전역 고유 id 여야 한다
    input_includes_cache: false   # input 에 캐시가 포함돼 있으면 true
    # 세션 줄의 ctx% — 생략하면 input+cache_read+cache_write.
    # mode: cumulative 면 반드시 지정한다 (누적치는 컨텍스트가 아니다)
    # ctx_tokens: usage.context_tokens
    # ctx_window: usage.context_window
    fields:
      input: usage.input_tokens
      cache_read: usage.cache_read_tokens
      cache_write: usage.cache_write_tokens
      output: usage.output_tokens
    context:
      cwd: cwd                    # → 프로젝트 이름 = 이 경로의 마지막 조각
      model: model                # → 가격 계산에 쓰임
      session: sessionId          # → 세션 수 집계
      vendor: providerID          # → 벤더 비교 (없으면 vendor: 나 모델명 추론)
    default_model: default        # 모델을 못 찾았을 때 (tokenmeter/pricing.py 의 키)
    plan_probe: { env: [FOO_API_KEY], if_set: api, else: subscription }
    install:
      target: none                # 일단 로그 감시만 (훅은 6단계에서)
```

없는 필드는 그냥 빼면 됩니다. 예를 들어 캐시 개념이 없는 도구는
`cache_read`/`cache_write` 를 생략하고 `input`/`output` 만 쓰면 됩니다.

**가격**이 `tokenmeter/pricing.py` 의 `PRICES` 에 없는 모델이면 `default` 단가로 계산됩니다.
정확한 비용이 필요하면 거기에 항목을 추가하세요 (1M 토큰당 USD).

---

## 5단계 — `doctor` 로 검증 (건너뛰지 마세요)

```bash
tokenmeter doctor my-agent
```

실제 로그 파일을 골라 파싱해보고, **어느 dot-path 가 비어 있는지** 짚어줍니다.

| 증상 | 원인 | 처방 |
|---|---|---|
| `로그 파일 없음` | `roots` / `patterns` 가 틀림 | `ls` 로 실제 경로 확인. `~` 는 확장되지만 `*` 는 `patterns` 에만 |
| `match 일치 0개` | `match` 의 dot-path 나 값이 틀림 | doctor 가 관측된 실제 값을 같이 찍어줍니다. 그걸 그대로 쓰세요 |
| `⚠ 전부 None` | `fields` dot-path 오타 | 2단계로 돌아가 실제 구조 재확인 |
| `context ⚠ 못 찾음` | cwd/model 이 다른 레코드에 있음 | 그 레코드의 dot-path 를 쓰세요. 없으면 생략하고 `default_model` 로 |
| 합계가 비정상적으로 큼 | `mode: delta` 인데 실제로는 누적 | 3단계로 돌아가 `cumulative` 로 |
| 합계가 실제보다 큼 (2배 근처) | 부분집합 필드를 더함 | 아래 "함정" 참고 |

`추출 델타` 줄의 토큰 수가 그 도구가 실제로 쓴 양과 얼추 맞으면 성공입니다.

---

## 6단계 — 훅 붙이기 (선택)

세션 시작/종료에 맞춰 데몬을 자동으로 켜고 끄고 싶을 때만 필요합니다.

### A. Claude Code 계열 hooks JSON 을 쓰는 도구

Claude Code, Codex 처럼 `{"hooks": {"SessionStart": [...]}}` 스키마의 설정 파일을 읽는 도구.

```yaml
    install:
      target: claude_json
      path: ~/.my-agent/settings.json
      events: [SessionStart, SessionEnd]
```

`install` 은 그 파일의 **기존 내용을 보존한 채 우리 엔트리만 추가**하고,
`uninstall` 은 우리 엔트리만 제거합니다. 두 번 설치해도 중복되지 않습니다.
처음 고칠 때 `<파일>.bak-tokenmeter` 백업이 남습니다.

**그 도구에 세션 중간 이벤트가 있으면 같이 넣으세요** (Claude Code 는 `UserPromptSubmit`).
라이브 파일 mtime 이 갱신돼 장시간 세션이 `live_ttl_hours` 로 잘리지 않고, 데몬이 죽어 있으면
그 이벤트가 되살립니다. 없어도 토큰이 들어오는 한 미터가 대신 갱신하니 필수는 아닙니다.

### B. OpenCode 플러그인

```yaml
    install:
      target: opencode_plugin
      path: ~/.config/opencode/plugin/tokenmeter.js
```

절대 경로가 박힌 ESM 플러그인 파일을 생성합니다. 남이 만든 동명 파일은 덮어쓰지 않습니다.

### C. 그 외 — `target: none` + 손으로 한 줄

훅 메커니즘이 다르거나 없는 도구는 `target: none` 으로 두고, 그 도구의 방식대로
아래 명령을 직접 등록하세요. 하는 일은 "라이브 세션 기록 + 데몬 보장 기동" 뿐입니다.

```bash
"<파이썬>" "<저장소>/tokenmeter/hook.py" my-agent SessionStart
"<파이썬>" "<저장소>/tokenmeter/hook.py" my-agent SessionEnd
```

`tokenmeter/hook.py` 는 **표준 라이브러리만** 쓰고 stdout 에 아무것도 쓰지 않으며 **항상 exit 0** 입니다
(실측 ~30ms). 실패해도 에이전트를 막지 않습니다. `TOKENMETER_DISABLE=1` 로 끌 수 있고,
stdin JSON 의 `cwd` 나 `TOKENMETER_CWD` 환경변수로 프로젝트를 알려줄 수 있습니다.

설치 후 확인:

```bash
tokenmeter install --dry-run    # 무엇이 바뀔지만 보기
tokenmeter install --service my-agent
tokenmeter services              # 훅 설치 여부 표
```

---

## 실전 예제 3종 — 왜 이렇게 설정했는가

기본 제공 서비스들이 최고의 교재입니다. 셋 다 서로 다른 함정을 밟고 있습니다.

### Claude Code — 가장 단순한 `delta`

```json
{"type":"assistant","uuid":"...","cwd":"/Users/me/proj",
 "message":{"model":"claude-opus-5",
   "usage":{"input_tokens":2,"cache_creation_input_tokens":2161,
            "cache_read_input_tokens":60955,"output_tokens":813}}}
```

값이 그 턴의 증가분 그 자체 → `mode: delta`, `key: uuid`.
`input_tokens` 는 캐시를 포함하지 않음 → `input_includes_cache: false`.

> ⚠️ 단, `uuid` 는 **세션을 fork/resume 하면 새 파일로 복사**됩니다.
> 그래서 중복 제거 집합은 파일별이 아니라 **서비스 전역**이어야 합니다 (이미 그렇게 동작합니다).
> 직접 파서를 만든다면 여기서 4배 과다계상이 나기 쉽습니다.

### Codex — 누적치 + 캐시 포함 + 부분집합

```json
{"type":"event_msg","payload":{"type":"token_count","info":{
  "total_token_usage":{"input_tokens":50327,"cached_input_tokens":34304,
    "output_tokens":384,"reasoning_output_tokens":129}}}}
```

세 가지가 동시에 걸려 있습니다.

1. `total_token_usage` 는 **세션 누적치** → `mode: cumulative`, `key: null`(파일 단위)
2. `input_tokens` 가 `cached_input_tokens` 를 **포함** → `input_includes_cache: true`
3. `reasoning_output_tokens` 는 `output_tokens` 의 **부분집합** → **더하면 안 됨**
4. 컨텍스트 점유도 누적치로 보면 늘 100% 다 → `ctx_tokens: payload.info.last_token_usage.total_tokens`
   (창 크기는 로그가 알려준다: `ctx_window: payload.info.model_context_window`)

`match` 대상이 `type` 이 아니라 `payload.type` 이라는 것도 놓치기 쉽습니다.
그리고 `cwd`/`model` 은 token_count 레코드에 없고 같은 파일의 `session_meta`/`turn_context`
줄에 있어서, `context` 가 파일 단위로 학습합니다.

### OpenCode — 파일 하나가 레코드 하나, 두 번 기록됨

```json
{"id":"msg_...","role":"assistant","modelID":"...","path":{"cwd":"/Users/me"},
 "tokens":{"input":3265,"output":88,"cache":{"read":25344,"write":0}}}
```

메시지 파일이 **생성 시점(토큰 0)과 완료 시점에 각각** 기록됩니다.
`delta` 로 두면 0 을 먼저 먹고 실제값을 못 먹거나 이중계상됩니다
→ `mode: cumulative`, `key: id`.

---

## 자주 밟는 함정 5가지

1. **누적치를 델타로 더하기** — 값이 폭증합니다. 레코드 2~3개를 꼭 눈으로 비교하세요.
2. **부분집합 필드 더하기** — `reasoning`, `total` 같은 필드가 다른 필드에 이미 포함돼 있는지
   `total == input + output` 인지 계산해서 확인하세요.
3. **캐시가 input 에 포함된 걸 모르기** — 비용이 캐시 할인 없이 계산돼 몇 배로 뜁니다.
4. **같은 레코드가 여러 파일에 복사되는 걸 모르기** — fork/resume 로 이중계상.
5. **`match` 없이 전부 먹기** — 요약·서브에이전트 레코드까지 섞여 들어갑니다.

기동할 때 워처가 `prime()` 을 돌려 **과거 로그는 먹지 않고** 그 시점 이후 증가분만 먹습니다.
그래서 설정을 바꾼 뒤에는 데몬을 재시작해야 새 서비스가 잡힙니다.

```bash
pkill -f "tokenmeter.cli daemon"     # 다음 세션 훅이 알아서 다시 띄웁니다
```

---

## 잘 됐으면 기여해주세요

`tokenmeter/services.yaml` 블록과 `tokenmeter doctor <서비스>` 출력을 함께 PR 로 올려주시면
다른 사람도 그 도구를 바로 쓸 수 있습니다. 로그 샘플 한 줄(민감정보 제거)도 있으면 좋습니다.
