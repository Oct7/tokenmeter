# TokenMeter 📊

Claude Code / Codex / OpenCode 가 쓰는 토큰을 **자동으로** 재고,
화면 위에 항상 떠 있는 **미터기 + 라이브 세션·히스토리**로 보여주는 도구입니다.

측정을 켜고 끄는 일은 없습니다. 한 번 설치하면 에이전트를 켜는 순간 알아서 붙습니다.

```
┌────────────────────────────────────────────────────┐
│ TOKEN METER                                   오늘 │
│ 478 출력 tok/s                           ≈$15.8599 │  ← ≈ = 구독분 포함 (환산가)
│ ██████████████░░░░░░░░░░░░░░░░░░░░░░░░           ▏ │  ← 채움 = 지금 생성 속도, ▏ = 피크 홀드
│ ^ 1.2M          v 84.0k     ~ 23.5M   절감 $12.40  │  ← 입력 / 출력 / 캐시 / 캐시 절감액
├────────────────────────────────────────────────────┤
│ 세션                                 더블클릭 전환 │
│ tokenmeter      opus-5[1m]  xhi     412/s       31% │  ← 작업 상태는 금색, 끝은 ctx 점유
│ hauswe         gpt-5.6-sol high     63/s       75% │
│ api-server   sonnet-4-5  med       3/s       95% │  ← 90% 넘으면 빨강 (높은 점유 경고)
│ web-client  gemini-3-pro         1.2M           │  ← 창 크기를 모르면 빈칸
│ 세션 7개 기록 · 라이브 4개                         │
└────────────────────────────────────────────────────┘
```

기본 화면이 **세션 목록**입니다 — 프로젝트 · 모델 · effort · **tok/s** · **ctx%** 와
**확인·작업·대기·종료** 상태가 한 줄씩 표시됩니다. 상태 순서가 우선이고,
같은 상태에서는 tok/s와 마지막 활동 순으로 정렬됩니다.

**ctx% 는 그 세션의 컨텍스트가 얼마나 찼는지**입니다 (마지막 턴 기준). 90% 를 넘으면
높은 점유 상태로 빨강 표시합니다. 이는 압축 시점이나 남은 시간을 예측하는 기능이 아닙니다.
창 크기를 모르는 모델은 아예 그리지 않습니다
(`tokenmeter price set <모델> --window N` 으로 알려주면 그때부터 나옵니다).
벤더 칸은 확장 모드에서만 나옵니다 — 좁은 창에서는 모델명으로 짐작되는 벤더보다
다른 데서 볼 방법이 없는 ctx% 가 그 자리를 씁니다.

**게이지 채움이 곧 출력 토큰 생성 속도(tok/s)입니다.** 토큰이 들어오면 차오르고,
유입이 끊기면 지수감쇠로 내려가 0 에서 멈춥니다 — 화면을 안 봐도
곁눈질만으로 에이전트가 일하는 중인지 알 수 있습니다.

> 캐시 읽기는 **속도에 넣지 않습니다.** 이미 저장된 프롬프트의 재사용이라 어떤 속도로
> '생성'된 적이 없고, 실측에서 출력 토큰의 300배가 넘어 넣는 순간 미터가 캐시 히트율
> 그래프가 됩니다. 입력·캐시 총량은 아래 `^ v ~` 줄에서 봅니다.
> 만땅 기준은 `settings.overlay.full_scale` (기본 300 출력 tok/s) 로 조정합니다.

세션 상태는 다음 네 가지뿐입니다. `확인`은 permission/question/stop 또는 attention-required
같은 명시적 에이전트 이벤트가 있을 때만 표시되며, 조용함이나 비활동만으로 추론하지 않습니다.
`작업`은 최근 토큰이 들어오는 상태, `대기`는 라이브지만 토큰 유입이 멈춘 상태,
`종료`는 라이브 기록이 없는 상태입니다. Context Runway와 압축 시점 예측 기능은 구현하지 않았습니다.

아래쪽 패널은 **더블클릭으로 셋을 돌려 봅니다.**

| 패널 | 보이는 것 |
|---|---|
| **세션** (기본) | 프로젝트 · 모델 · effort · tok/s · ctx%(확장 모드는 열린 시각·벤더도). 확인 → 작업 → 대기 → 종료, 같은 상태는 tok/s → 마지막 활동 순. 확인은 빨강, 작업은 금색, 대기는 회색, 종료는 짙은 회색 |
| **자체 호스팅 랭킹** (선택) | 비용 내림차순, 내 줄 금색. `leaderboard.endpoint` 가 비면 나 혼자 한 줄 (네트워크를 **한 번도** 건드리지 않습니다) |
| **일별 히스토리** | 최근 7일 날짜별 토큰·비용. 진행 중인 오늘이 맨 위에 함께 놓입니다 |

세션 줄의 tok/s 는 미터 바늘과 같은 방식(임펄스 + 지수감쇠)으로 **그 세션의 출력 토큰만**
재므로, 여러 에이전트를 동시에 돌릴 때 지금 실제로 일하는 게 누구인지 바로 보입니다.
effort 는 로그에 있는 서비스만 채웁니다 (Claude Code · Codex).

**미터 영역 더블클릭 = 오늘 ↔ 누적, 패널 영역 더블클릭 = 패널 전환입니다.**

---

## 빠른 시작 — 설치 → 자동 동작

```bash
uv tool install git+https://github.com/Oct7/tokenmeter.git
tokenmeter install                       # 훅 등록 (멱등, 기존 훅은 건드리지 않음)
tokenmeter services                      # 무엇이 어디에 붙었는지 확인
```

소스에서 개발하려면 `uv sync` 후 `uv run tokenmeter ...`를 사용합니다.

### 스킬로 쓰기 (선택)

CLI 를 외우기 싫으면 스킬을 설치해서 말로 시키면 됩니다.

```bash
npx skills add Oct7/tokenmeter -g -a claude-code     # 전역 설치
npx skills add . -g -a claude-code                  # 로컬 체크아웃에서
```

`~/.claude/skills/tokenmeter` 에 들어가고, 그 뒤로는 "토큰 얼마나 썼어", "미터기 꺼줘",
"코덱스는 재지 마" 같은 말로 아래 명령들이 대신 실행됩니다. 스킬은 저장소 위치를
찾지 않고 PATH의 `tokenmeter` 명령을 직접 실행합니다.
`skills` 는 claude-code 외 70여 개 에이전트도 지원합니다 (`-a` 로 지정).

> 저장소를 직접 고치면서 쓸 거라면 복사 대신 심볼릭 링크를 거세요 —
> `ln -sfn "$PWD/skills/tokenmeter" ~/.claude/skills/tokenmeter`

끝입니다. 다음에 에이전트를 켜면 이렇게 흘러갑니다.

```
에이전트 세션 시작
   └─ 훅 tokenmeter/hook.py                → <상태 디렉터리>/live/<서비스>__<세션>.json 기록
                                    → 데몬이 죽어 있으면 백그라운드로 기동

데몬  tokenmeter daemon
   ├─ 워처     tokenmeter/watcher.py       → 로그 파일을 증분 파싱해 TokenDelta 생성
   ├─ 계량기   tokenmeter/meter.py         → <상태 디렉터리>/state.json 갱신 (단일 writer, 원자적 쓰기)
   ├─ 랭킹     tokenmeter/leaderboard.py   → 주기적으로 업로드·조회 (endpoint 있을 때만)
   └─ 오버레이 tokenmeter/overlay.py       → 200ms 마다 상태를 읽어 미터/랭킹에 반영

세션이 도는 동안   → 라이브 파일 mtime 갱신 (아래 '살아 있음' 참고)
세션 종료 훅       → 라이브 파일 삭제
라이브 0개가 idle_exit_minutes(기본 30분) 지속 → 데몬 스스로 종료
```

기동할 때 워처가 `prime()` 을 돌려 **과거 로그는 먹지 않습니다.** 그때부터 늘어난 분만 먹습니다.

해제는 `tokenmeter uninstall` — 우리가 넣은 엔트리만 빠지고 다른 훅은 그대로입니다.

### 세션이 '살아 있음' 을 어떻게 아나 — 두 겹으로 봅니다

라이브 파일이 `live_ttl_hours`(기본 6시간)보다 오래 방치되면 종료 훅을 놓친 잔재로 보고
지웁니다. 그런데 **생존 신호가 세션 시작 때 한 번뿐이면 6시간 넘게 도는 멀쩡한 세션이
잘려 나가고**, 라이브가 0개가 되면 30분 뒤 데몬이 세션 도중에 자살합니다. 그래서 둘로 봅니다.

| 신호 | 누가 | 적용 범위 |
|---|---|---|
| 훅 중간 이벤트 | Claude Code `UserPromptSubmit`, OpenCode `Ping`(플러그인) | 등록된 이벤트가 있는 서비스 |
| **토큰 유입** | `Meter._touch_live` — 델타가 들어오면 그 세션의 라이브 파일을 갱신 | **모든 서비스** (Codex 처럼 중간 이벤트가 없어도) |

훅 이벤트에만 기대면 서비스마다 새는 곳이 생기므로, 실제 토큰 유입을 최종 근거로 씁니다.

그리고 **종료 아닌 모든 훅 이벤트가 데몬 생존을 확인합니다.** 데몬이 크래시하거나 오버레이를
닫아 죽었더라도 다음 프롬프트에 알아서 되살아납니다 (살아 있으면 pid 검사에서 즉시 물러나므로
비용은 없습니다). 예전에는 `SessionStart` 에서만 띄워서, 한 번 죽으면 다음 세션까지 측정이 통째로 비었습니다.

> ⚠️ 훅에는 **설치 시점의 파이썬 경로와 저장소 경로가 절대 경로로 박힙니다.**
> 저장소를 옮기거나 가상환경을 지웠다면 `install` 을 다시 실행하세요 (멱등이라 그냥 덮어씁니다).
> `tokenmeter services` 가 표 아래에 **박힌 커맨드**를 그대로 찍어주고,
> 설정 파일의 값이 그것과 다르면 훅 설치 칸이 `갱신 필요` 로 바뀝니다 — 훅은 실패해도
> 조용히 exit 0 이라, 이 표시가 없으면 죽은 경로를 부르고 있다는 걸 알 방법이 없습니다.
>
> **제품이 TokenPet → TokenMeter로 바뀌었습니다.** 이전 버전을 설치해 두셨다면
> `tokenmeter install`을 한 번 다시 돌리세요 — 옛 경로를 가리키던 엔트리를 알아보고 **제자리에서
> 교체**하므로 중복이 생기지 않습니다. 다시 돌리지 않으면 그 엔트리가 없는 파일을 계속 부릅니다.

---

## 요구사항이 어디서 충족되나

| 요구사항 | 어디서 | 확인 방법 |
|---|---|---|
| ① 에이전트 세션에 맞춰 측정이 자동으로 켜지고 꺼진다 | `tokenmeter/hook.py`(SessionStart/UserPromptSubmit/SessionEnd — 종료 아닌 모든 이벤트가 데몬 생존 확인) + `tokenmeter/installer.py`(훅 설치) + `tokenmeter/cli.py daemon`(유휴 자동 종료) | `tokenmeter services` / `status` 의 라이브 세션 |
| ② 입력·출력 토큰을 자동으로 잰다 | `tokenmeter/watcher.py` 가 3사 로그를 증분 파싱, `tokenmeter/meter.py` 가 누적 | `tokenmeter doctor` |
| ③ 입력 / 출력 / 캐시를 구분해서 보여준다 | 오버레이 미터의 `^입력 v출력 ~캐시` 색 구분(입력=초록, 출력=시안, 캐시=앰버), CLI `status` 표 | `tokenmeter status` |
| ④ 항상 최전면에 생성 속도가 보인다 | `tokenmeter/overlay.py` — 28칸 세그먼트 게이지, 출력 토큰 tok/s 지수평균에 연동된 채움·피크 홀드·감속 관성 | `python3 -m tokenmeter.demo` / `python3 -m tokenmeter.overlay`(미터 물리 자가 검증) |
| ⑦ 지나간 사용량을 되짚어 본다 | `tokenmeter/meter.py` 가 하루가 끝날 때 `days` 에 남기고, 오버레이 패널이 일별·세션별로 보여줍니다 | 오버레이 패널 더블클릭 / `tokenmeter status` |
| ⑤ 다른 사람들과 랭킹을 겨룬다 | `tokenmeter/leaderboard.py` — 비용 내림차순, 내 줄 강조, 오프라인이면 로컬 폴백 | `tokenmeter status --sync` |
| ⑥ 벤더·요금제·모델·세션을 나눠 재서 비교한다 | `tokenmeter/watcher.py`(축 판정) + `tokenmeter/meter.py`(집계) + `tokenmeter/leaderboard.py`(업로드) | `tokenmeter doctor` / `status` |

비용(USD)은 `tokenmeter/pricing.py` 의 모델별 단가로 캐시 읽기/쓰기까지 나눠 계산합니다.

---

## 무엇을 나눠 재나 — 비교의 축

사용자가 늘었을 때 **"어떤 벤더를 많이 쓰나 · 어떤 모델이 호출이 많나 · 어떤 모델로 세션이
많이 도나 · 구독과 종량제 비율은 얼마나"** 를 답하려면, 그 축들이 처음부터 따로 세어져 있어야
합니다. 델타(= 모델 호출) 한 건이 아래 다섯 축에 동시에 들어갑니다.

| 축 | 무엇 | 어떻게 알아내나 |
|---|---|---|
| `vendors` | API 벤더 (anthropic / openai / opencode / example-llm …) | 로그의 벤더 필드 → 서비스 `vendor:` → 모델명 추론(`pricing.vendor_of`) 순 |
| `endpoints` | 실제로 통신하는 URL (공식 API / Bedrock / 사내 게이트웨이) | 훅이 찍은 라우팅 환경변수 → 클라이언트 설정 파일 → 기본값 (아래) |
| `plans` | 구독제 vs API 종량제 | `plan` 명시값 → `plan_probe` (아래) |
| `models` | 모델별 | 로그의 모델 필드 (+ 그 모델의 벤더를 함께 기록) |
| `services` | 어떤 CLI 로 쓰는지 | 서비스 이름 |
| `projects` | 프로젝트별 (**로컬 전용 — 업로드 안 함**) | cwd 의 마지막 경로 조각 |

각 축마다 **토큰 4종 · 비용 · 캐시 절감액 · 호출 수 · 세션 수**를 셉니다.

- **호출 수** = 델타 건수. "어떤 모델이 가장 많이 불렸나" 는 토큰량과 다른 질문입니다
  (캐시를 크게 읽는 모델 하나가 토큰은 1등이어도 호출은 3등일 수 있습니다).
- **세션 수** = 서로 다른 세션 id 의 개수. 한 세션이 도중에 모델을 바꾸면 **두 모델 모두**
  1세션으로 셉니다. 세션 id 는 `context.session` dot-path 로 읽습니다
  (Claude Code `sessionId`, Codex `payload.session_id`, OpenCode `sessionID`).
- **캐시 절감액** = 캐시로 읽은 토큰을 입력으로 냈다면 더 들었을 차액
  (`cache_read × (입력단가 − 캐시단가)`). 낸 돈이 아니라 **안 낸 돈**이라 비용과 따로 셉니다.
  미터의 `~` 줄 오른쪽과 `status` 에 나옵니다. 켠 시점부터 쌓이므로 기존 누적치에는 없습니다.

### 구독 vs API 는 로그에 없습니다

세 도구 모두 로그 레코드에 결제 형태를 남기지 않습니다. 유일한 단서는 **클라이언트의 인증
설정**이라, 서비스마다 `plan_probe` 로 그걸 봅니다. 확실하면 `plan:` 에 못 박으면 됩니다.

```yaml
  claude-code:
    plan_probe:                    # API 키가 있으면 그걸 쓰고, 없으면 OAuth(구독)
      env: [ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN]
      if_set: api
      else: subscription
  codex:
    plan_probe:                    # ~/.codex/auth.json 의 auth_mode
      path: ~/.codex/auth.json
      key: auth_mode
      map: { chatgpt: subscription, apikey: api }
      default: unknown
  opencode:
    plan: api                      # 프로바이더 키를 직접 넣는 구조
```

> 판정 결과는 `doctor` 가 서비스마다 찍어 줍니다. `unknown` 이 뜨면 그 사용자는 요금제
> 비교에서 빠지므로, 그때는 `plan:` 을 직접 적어 주세요.

**구독분이 섞여 있으면 금액 앞에 `≈` 가 붙습니다.** 구독 사용자에게 `$15.86` 은 청구서가
아니라 "같은 토큰을 API 로 샀다면 이만큼" 이라는 환산가입니다. 미터와 `status` 가 같은
표기를 씁니다 — 숫자를 지우지 않되, 그 숫자가 무엇인지는 틀리지 않게 합니다.

```
$ tokenmeter doctor codex
   감지 세션 : 1개  (예: 019fdc74-f389-7aa1-815a-)
   감지 벤더 : openai   (기본값 openai)
   요금제    : subscription (프로브)
   엔드포인트: https://chatgpt.com/backend-api/codex   → 업로드 라벨 'chatgpt.com'
```

### 어떤 URL 로 통신 중인지 — 로그가 아니라 훅이 압니다

같은 `claude-opus-5` 라도 공식 API 인지 Bedrock 인지 사내 게이트웨이인지에 따라 결제처가
다릅니다. 그런데 이것도 로그에는 없습니다. **훅은 에이전트 프로세스의 자식이라 그 세션의
환경변수를 그대로 물려받으므로**, 세션 시작 때 라우팅 관련 변수만 찍어 라이브 파일에
남깁니다(`hook.routing_env`). 데몬은 여러 세션에 걸쳐 살아 있어서 데몬 자신의 환경으로는
세션별 구분이 안 되기 때문입니다.

찍는 것은 **URL·프록시·SDK 플래그뿐이고, 이름에 `KEY`/`TOKEN`/`SECRET`/`AUTH` 가 들어간
변수는 통째로 제외**합니다 — 훅이 토큰을 파일에 쓰는 일은 없습니다.

```yaml
  claude-code:
    endpoint_probe:
      flags:                                  # SDK 경로 자체가 바뀌는 경우
        CLAUDE_CODE_USE_BEDROCK: bedrock
        CLAUDE_CODE_USE_VERTEX: vertex
      env: [ANTHROPIC_BASE_URL]               # 게이트웨이/프록시 경유
      default: https://api.anthropic.com
  opencode:
    endpoint_probe:                           # 프로바이더마다 URL 이 다르다
      path: ~/.config/opencode/opencode.json
      key: provider.{vendor}.options.baseURL  # {vendor} = providerID
```

우선순위는 **플래그 → 환경변수 → 설정 파일 → 기본값**이고, `default` 를 dict 로 쓰면
요금제별로 갈 수 있습니다 (Codex 구독은 ChatGPT 백엔드, 종량제는 api.openai.com).

> 네트워크를 뜯는 방법(`lsof`, MITM 프록시)도 이론상 가능하지만 쓰지 않습니다.
> `lsof` 는 호스트만 주고 경로는 못 주는 데다 CDN 뒤라 무의미하고, MITM 은 루트 CA 설치와
> 전 트래픽 복호화를 요구합니다 — 토큰 계량기가 감수할 비용이 아닙니다.

#### 사내 주소는 서버로 나가지 않습니다

로컬(`status`/`doctor`)에서는 실제 URL 을 그대로 보여주지만, **업로드할 때는 분류된
라벨만** 나갑니다. 저명한 공개 엔드포인트(`api.anthropic.com`, `api.openai.com`,
`openrouter.ai`, `bedrock`, `vertex`, `azure-openai` …)는 `tokenmeter/endpoints.py` 에 기본
탑재돼 이름 그대로 올라가고, 그 밖의 주소는 전부 `self-hosted` 한 칸으로 합쳐집니다.
판별 실패는 `unknown` 으로 따로 셉니다(`self-hosted` 에 섞으면 통계가 거짓말을 합니다).

```
로컬 표시                         업로드 라벨
https://api.anthropic.com    →    api.anthropic.com
https://chatgpt.com/…        →    chatgpt.com
https://llm.example.test/v1    →    self-hosted   ← 합쳐짐
https://api.example.test/v1  →    self-hosted   ↲
mycorp.openai.azure.com      →    azure-openai  (테넌트명 제거)
```

관리자가 검증해 공개해도 된다고 판단한 호스트만 화이트리스트에 덧붙입니다.

```yaml
settings:
  leaderboard:
    public_endpoints: ["llm.mycorp.com"]   # 이때부터 이름 그대로 올라간다
```

---

## 명령어

| 명령 | 하는 일 |
|---|---|
| `install [--service X] [--dry-run]` | 훅 설치 (멱등). `--dry-run` 은 무엇이 바뀔지만 보여줍니다 |
| `uninstall [--service X]` | 우리 엔트리만 제거 |
| `on` / `off` `[--service X]` | 측정 켜기/끄기. **훅은 그대로 두고 무력화**하므로 재설치가 필요 없습니다. `--service` 로 특정 에이전트만 |
| `meter [on\|off]` | 세션이 시작될 때 미터 창을 띄울지. 인자 없이 부르면 현재 값 |
| `services` | 서비스별 활성 / 로그 경로 / 로그 파일 수 / 최근 로그 / 훅 설치 여부 |
| `doctor [서비스...]` | **새 서비스 설정 검증.** 최근 로그를 실제로 파싱해 매칭 수·필드 합계·감지된 model/cwd 를 보여주고, None 인 dot-path 를 짚어줍니다 |
| `price` | 모델별 단가·컨텍스트 창 조회. 가격표에 없어 **default 단가로 추정 중인 모델**을 짚어줍니다 |
| `price set <모델> [--input N] [--cache-read N] [--cache-write N] [--output N] [--window N]` | 그 모델의 단가/컨텍스트 창을 직접 못 박습니다 (`~/.config/tokenmeter/prices.json`). 적어 넣은 항목만 이기고 나머지는 기본 표에서 옵니다 |
| `price unset <모델>` | 지정한 단가를 지웁니다 |
| `status [--scope today\|total] [--sync]` | 누적·오늘·세션 토큰, **벤더/요금제/모델/클라이언트/프로젝트별 토큰·호출·세션·비용**, 선택형 랭킹, 라이브 세션 |
| `status --json` | 내부 경로·세션 ID·라우팅 URL을 제외한 공개 상태 스냅샷 한 개를 출력 |
| `daemon [--no-overlay]` | 워처(스레드) + 랭킹 동기화 + 오버레이(메인 스레드). 훅이 자동으로 띄웁니다 |
| `start` / `stop` | 훅이 없는 환경에서 라이브 세션을 수동 등록/해제 |
| `watch [--service X]` | 오버레이 없이 감시만 |
| `watch --jsonl` | 상태 스냅샷과 양의 토큰 변화/관심 상태 변화를 읽기 전용 JSONL로 출력 |
| `receipt --format text\|markdown\|json` | 가장 최근 세션 영수증. 금액 라벨은 API면 `예상 사용액`, 구독이면 `API 환산 가치` |
| `adapter init NAME --log PATH` | 최근 JSON/JSONL 로그를 익명화해 `NAME-adapter/fixture.json`과 `service.yaml`, 정확히 두 파일을 생성 |
| `adapter check PATH` | 두 파일의 구조와 dot-path를 검사하며 `mode: delta` 또는 `cumulative` 선택은 사용자에게 남김 (`unresolved`) |
| `team [--sync] [--json]` | 기존 자체 호스팅 leaderboard의 `today.attention` 집계로 팀의 확인·작업·대기·위험 수를 표시 |
| `overlay` | 오버레이만 (읽기 전용 — 토큰은 데몬이 먹입니다) |
| `reset --yes` | 누적 통계 초기화 |

### 공개 JSON과 개인정보 경계

`status --json`은 `schema_version`, `type`, `timestamp`가 있는 공개 스냅샷을 한 개
출력합니다. `watch --jsonl`은 첫 스냅샷 뒤 `delta`, `attention`, 또는 초기화 시
새 `snapshot`을 한 줄씩 출력합니다. 공개 JSON에는 내부 경로, 세션 ID, 라우팅 URL이
없으며 프롬프트, 응답, 툴 명령, 파일명을 저장하거나 전송하지 않습니다.

관심 파일에는 이벤트 이름과 시각만 남깁니다. 이 공개 투영은 프로젝트·서비스·모델과
집계 토큰 같은 화면에 필요한 값만 허용 목록으로 내보냅니다. 어댑터 fixture는 모든
일반 값을 지우고 비밀처럼 보이는 키만 `<redacted>`로 바꿉니다.

### 영수증, 어댑터, 팀 모드

영수증은 저장된 세션 중 마지막으로 갱신된 하나를 읽기 전용으로 보여줍니다. `text`는
5줄, `markdown`은 제목과 4개 항목, `json`은 동일한 영수증 객체입니다. 비용 표시는
API 요금제에서 `예상 사용액`, 구독에서 `API 환산 가치`, 알 수 없는 요금제에서
`API 환산가`를 사용합니다. 세션이 없으면 명령은 1로 끝나며 영수증을 만들 수 없다는
메시지만 출력합니다.

`adapter init NAME --log PATH`는 최신 JSON/JSONL 레코드를 한 번 읽어 현재 디렉터리의
`NAME-adapter/` 아래 `fixture.json`과 `service.yaml`만 만듭니다. 원래 값은 fixture에 남기지
않고 일반 문자열·숫자·불리언은 빈 값·0·false로, secretish 키의 값은 `<redacted>`로
바꿉니다. `service.yaml`의 `mode`, `key`, `match`는 `choose-delta-or-cumulative` 등
미해결 선택으로 둡니다. 비어 있지 않은 대상은 덮어쓰지 않습니다. `adapter check PATH`는
`mode`를 `delta` 또는 `cumulative`로 고르고 dot-path가 fixture 구조에 맞는지만 확인합니다.

`team`은 endpoint가 설정되지 않았으면 로컬 사용자 한 줄만 표시하며 네트워크를 호출하지
않습니다. `team --sync`는 기존 자체 호스팅 leaderboard endpoint가 있을 때만 즉시 동기화하고,
없거나 오프라인이면 캐시/로컬 폴백으로 계속 표시합니다. 전송되는 새 관심 정보는
`today` 아래의 `check`, `working`, `waiting`, `risk` 정수 집계뿐입니다. TokenMeter는
호스팅 팀 서비스가 아니라 기존 endpoint를 사용하는 클라이언트입니다.

## 끄는 방법이 셋입니다 — 서로 다릅니다

```bash
tokenmeter meter off              # 창만 안 뜸. 측정은 계속 (나중에 status 로 다 보입니다)
tokenmeter off                    # 측정 정지 + 데몬 종료. 훅은 남아 있어 on 하면 즉시 재개
tokenmeter off --service codex    # Codex 만 빼고 나머지는 계속
tokenmeter uninstall              # 설정 파일에서 훅 엔트리 제거 (다시 쓰려면 install)
```

| | 창 | 측정 | 훅 엔트리 | 되돌리기 |
|---|---|---|---|---|
| `meter off` | 안 뜸 | **계속** | 그대로 | `meter on` |
| `off` | 안 뜸 | 정지 | 그대로 | `on` (재설치 불필요) |
| `uninstall` | 안 뜸 | 정지 | 제거됨 | `install` |

대개 거슬리는 건 창이지 측정이 아니므로 **`meter off` 가 기본 선택**입니다. `status` 는 현재 어느
스위치가 꺼져 있는지 맨 위에 찍어줍니다 — 조용히 안 되고 있는 상태를 만들지 않기 위해서입니다.

## 오버레이 조작

- **좌드래그** 이동 · **휠** 크기 조절(0.6~2.0) — `<상태 디렉터리>/overlay.json`에 저장됩니다
- **미터 영역 더블클릭** = 오늘 ↔ 누적 전환 (실수로 닫히지 않게 종료가 아닙니다)
- **패널 영역 더블클릭** = 세션 → 자체 호스팅 랭킹 → 일별 히스토리 순환
- **우상단 ▾ 클릭** = 히스토리 펼치기 (다시 누르면 접힙니다)
- **우클릭** = 히스토리 여닫기 · 오늘/누적 · 패널 전환 · 패널 접기 · **미니 모드** · 항상 위 · 지금 동기화 · 통계 초기화 · 종료

**처음 띄우면 아래에 조작 힌트가 20초간 뜹니다.** 창을 한 번 건드리면 바로 사라지고,
그 사실이 `<상태 디렉터리>/overlay.json`에 남아 다음부터는 나오지 않습니다.

### 미니 모드

우클릭 → **미니 모드 (한 줄만)**. 속도 · 게이지 · 비용만 남기고 창이 1/5 로 줄어듭니다.
회의·화면 공유·녹화처럼 화면을 비워야 할 때 쓰고, **더블클릭이면 원래대로** 돌아옵니다
(빠져나가는 길이 메뉴뿐이면 갇힌 것처럼 느껴지므로). 측정은 그대로 계속됩니다.

PyQt6 가 없거나 디스플레이가 없으면 오버레이만 조용히 꺼지고 **측정은 계속 동작합니다.**

### 히스토리 (확장 모드)

▾ 를 누르면 창이 넓어지고 위에 **시간별 사용량 그래프**, 아래에 두 배로 긴 목록이 붙습니다.

- 막대는 **프로젝트별로 쌓입니다** — 색은 이름에서 뽑으므로 프로젝트마다 항상 같습니다
- **[오늘] [7일] [30일]** 로 범위를 바꿉니다
- **세션 줄을 클릭**하면 그래프가 그 프로젝트만 보여줍니다. **일별 줄을 클릭**하면 그 날의 24시간으로 바뀝니다. 같은 줄 재클릭 또는 **ESC** 로 풀립니다
- 확장 모드의 세션 목록에는 **세션이 열린 시각**(`MM-DD HH:MM`)이 맨 앞에 붙습니다

그래프의 원료는 `<상태 디렉터리>/hours.jsonl`입니다. 진행 중인 한 시간은 `state.json` 안에 있다가
시간이 넘어갈 때 한 줄로 append 되고, 60일치(1,440줄)까지 보관합니다. **설치 이전 기간은
복원되지 않습니다** — 집계만 저장돼 있었기 때문에 그래프는 켠 시점부터 쌓입니다.

---

## 모르는 모델은 비용을 조용히 틀립니다 — 그래서 짚어줍니다

가격표(`tokenmeter/pricing.py`)에 없는 모델은 `default` 단가($3/$15)로 계산됩니다. 새 모델이
나오면 **아무 경고 없이 비용이 틀리는** 상태가 되므로, `status` 와 `price` 가 그걸 먼저 말합니다.

```
$ tokenmeter status
  ⚠ 가격표에 없는 모델: nemotron-3-ultra-free, gemini-3-pro
    default 단가로 추정 중입니다 — `tokenmeter price set <모델> --input .. --output ..`

$ tokenmeter price set nemotron-3-ultra-free --input 0.9 --output 1.8 --window 128000
  nemotron-3-ultra-free 단가를 저장했습니다: input=0.9, output=1.8, window=128000
  파일: ~/.config/tokenmeter/prices.json  (데몬 재시작 없이 다음 계산부터 반영)
  ※ 이미 쌓인 비용은 그때의 단가로 계산돼 있습니다 — 소급되지 않습니다.
```

- 적어 넣은 항목만 이깁니다. 빠뜨린 `cache_read` 같은 건 기본 표에서 옵니다.
- **기본 표의 모델도 덮어쓸 수 있습니다** — `claude-opus-5[1m]` 같은 롱컨텍스트 변형은
  단가가 다른데 기본 표는 본체 단가를 씁니다. 정확히 세려면 여기서 못 박으세요.
- `--window` 는 세션 줄 **ctx% 의 분모**입니다. `[1m]` 이 붙은 모델은 지정하지 않아도 1M 로 봅니다.
- 데몬이 파일 mtime 만 보므로 **재시작 없이** 다음 계산부터 반영됩니다.

## 유휴 알림 — 에이전트가 멈추면 알려줍니다

세션이 열려 있는데 토큰 유입이 `settings.idle_notify_seconds`(기본 90초) 동안 끊기면
데스크톱 알림이 한 번 뜹니다 — 대개 "턴이 끝나 내 차례가 돌아왔다" 는 신호입니다.

```yaml
settings:
  idle_notify_seconds: 90    # 0 = 끄기
```

- **조용한 구간마다 한 번만** 울립니다. 토큰이 다시 들어오면 다시 무장됩니다.
- 라이브 세션이 하나도 없으면 울리지 않습니다 (기다리는 사람이 없으므로).
- macOS 는 `osascript`, Linux 는 `notify-send` 를 씁니다. 둘 다 없으면 조용히 넘어가고
  **측정은 그대로 계속됩니다.**

## 글로벌 랭킹 붙이기

`tokenmeter/services.yaml` 의 `settings.leaderboard` 만 채우면 됩니다. 코드는 건드리지 않습니다.

```yaml
settings:
  leaderboard:
    endpoint: "https://<프로젝트>.supabase.co/rest/v1/leaderboard"
    handle: "alice"
    sync_seconds: 60
    headers:
      apikey: "<anon key>"
      Authorization: "Bearer <anon key>"
      Prefer: "resolution=merge-duplicates"   # POST 를 upsert 로
```

동작은 단순합니다. `sync_seconds` 마다 **POST 로 내 줄을 올리고 GET 으로 전체를 받아**
`<상태 디렉터리>/leaderboard.json`에 캐싱합니다. 오버레이와 CLI 는 그 캐시만 읽습니다.

**올라가는 것** — 핸들, 오늘/누적 합계(토큰 4종 · 비용 · 호출 · 세션),
그리고 **모델 / 벤더 / 요금제 / 클라이언트**별 내역(각각 토큰·비용·호출·세션).
`team` 명령이 추가하는 것은 `today` 안의 확인·작업·대기·위험 **정수 집계뿐**입니다.
**안 올라가는 것** — 프로젝트명, 경로, 세션 ID, 라우팅 URL, 세션 내용, 프롬프트, 응답,
툴 명령, 파일명. `state.json` 의 `projects`
버킷과 `sessions` 기록은 전송 대상이 아닙니다.

받는 쪽(서버)이 돌려줄 모양은 우리가 올리는 본문과 같습니다. 리스트든 `{"entries": [...]}` 든 받습니다.

```json
{ "handle": "alice",
  "today":  { "input_tokens": 157, "cache_read": 23575766, "cache_write": 128360,
              "output_tokens": 130759, "cost_usd": 15.86, "calls": 231, "sessions": 4,
              "attention": { "check": 1, "working": 3, "waiting": 1, "risk": 1 },
              "date": "2026-08-11" },
  "total":  { "…": 0, "calls": 12043, "sessions": 318 },
  "models": { "claude-opus-5": { "…": 0, "calls": 231, "sessions": 12, "vendor": "anthropic" } },
  "vendors":{ "anthropic": { "…": 0, "calls": 231, "sessions": 12 } },
  "plans":  { "subscription": { "…": 0, "calls": 291, "sessions": 14 } },
  "clients":{ "claude-code": { "…": 0, "calls": 231, "sessions": 12 } },
  "endpoints": { "api.anthropic.com": { "…": 0 }, "self-hosted": { "…": 0 } } }
```

Supabase 라면 테이블 하나로 받고, 비교는 뷰로 뽑습니다.

```sql
create table leaderboard (
  handle text primary key,
  today jsonb, total jsonb,
  models jsonb, vendors jsonb, plans jsonb, clients jsonb, endpoints jsonb,
  updated_at timestamptz default now()
);

-- 어떤 벤더를 몇 명이, 얼마나 쓰나
create view vendor_share as
select k as vendor,
       count(*)                              as users,
       sum((v->>'cost_usd')::numeric)        as cost_usd,
       sum((v->>'calls')::bigint)            as calls,
       sum((v->>'sessions')::bigint)         as sessions
from leaderboard, lateral jsonb_each(vendors) as e(k, v)
group by k order by cost_usd desc;

-- 어떤 모델이 호출이 많나 / 세션이 많나 (같은 표에서 둘 다 보인다)
create view model_share as
select k as model, max(v->>'vendor') as vendor,
       count(*)                      as users,
       sum((v->>'calls')::bigint)    as calls,
       sum((v->>'sessions')::bigint) as sessions,
       sum((v->>'cost_usd')::numeric) as cost_usd
from leaderboard, lateral jsonb_each(models) as e(k, v)
group by k order by calls desc;

-- 구독 vs 종량제 비율
create view plan_share as
select k as plan, count(*) as users, sum((v->>'cost_usd')::numeric) as cost_usd
from leaderboard, lateral jsonb_each(plans) as e(k, v)
group by k;

-- 공식 API vs 클라우드 vs 자체 게이트웨이 (사내 주소는 self-hosted 로 들어온다)
create view endpoint_share as
select k as endpoint, count(*) as users, sum((v->>'calls')::bigint) as calls
from leaderboard, lateral jsonb_each(endpoints) as e(k, v)
group by k order by users desc;
```

검증은 `tokenmeter status --sync` — 업로드·조회를 즉시 돌리고 결과 표를 찍습니다.
서버가 죽어 있어도 측정은 멈추지 않고, 마지막으로 받은 랭킹이 그대로 남습니다.

> `endpoint` 가 비어 있는 동안에는 네트워크 코드가 **호출되지 않습니다.** 기본값이 그렇습니다.
> 오버레이의 랭킹은 사람 단위만 보여줍니다 — 벤더/모델 비교는 서버 집계가 붙은 뒤에
> 붙이는 게 맞아서(지금은 로컬 값밖에 없어 `status` 와 같은 내용이라) 일부러 비워 뒀습니다.

---

## 새 서비스 추가하기

코드는 건드리지 않습니다. `tokenmeter/services.yaml` 에 블록 하나를 더 쓰면 훅 설치와 토큰 측정이 함께 붙습니다.
(사용자 오버라이드: `~/.config/tokenmeter/services.yaml`, 같은 구조로 깊은 병합)

> 처음 붙여보는 거라면 **[docs/add-service.md](docs/add-service.md)** 를 보세요.
> 로그 찾기 → 레코드 뜯어보기 → `mode` 고르기 → doctor 검증 → 훅 붙이기까지 단계별로 안내하고,
> 기본 서비스 3종이 각각 어떤 함정(누적치·부분집합·캐시포함·fork 중복)을 밟고 있는지 설명합니다.

```yaml
services:
  gemini-cli:
    enabled: true
    label: Gemini CLI
    roots: ["~/.gemini/tmp"]
    patterns: ["**/*.json"]
    format: json
    match: { type: assistant }
    mode: delta
    key: id
    fields:
      input: usage.promptTokenCount
      output: usage.candidatesTokenCount
    context: { cwd: cwd, model: model, session: sessionId }
    default_model: default
    vendor: google
    plan_probe: { env: [GEMINI_API_KEY], if_set: api, else: subscription }
    install: { target: none }
```

| 필드 | 뜻 |
|---|---|
| `enabled` | false 면 감시·설치 모두 건너뜀 |
| `roots` | 로그가 쌓이는 디렉토리들 (`~` 확장). 없는 경로는 무시 |
| `patterns` | roots 하위에서 감시할 glob |
| `format` | `jsonl`(한 줄 = 한 레코드) \| `json`(파일 전체 = 한 레코드) |
| `match` | `{dot.path: 값}` 이 전부 일치하는 레코드만 사용 (생략 = 전부) |
| `mode` | `delta`(값이 그 자체로 증가분) \| `cumulative`(누적치 → 직전값과의 차이만 반영) |
| `key` | 중복 제거·누적 기준 키의 dot-path. 생략하면 파일 경로가 키 |
| `fields` | 토큰 4종(`input`/`cache_read`/`cache_write`/`output`)의 dot-path |
| `input_includes_cache` | true 면 `input - cache_read` 를 순수 입력으로 계산 |
| `ctx_tokens` | 지금 컨텍스트에 차 있는 토큰의 dot-path (세션 줄의 ctx%). 생략하면 레코드의 `input+cache_read+cache_write`. **`mode: cumulative` 면 반드시 지정** — 안 그러면 세션 누적치가 컨텍스트로 잡혀 늘 100% 로 보입니다 (Codex 는 `payload.info.last_token_usage.total_tokens`) |
| `ctx_window` | 컨텍스트 창 크기의 dot-path (Codex `payload.info.model_context_window`). 생략하면 모델 가격표의 `window`, 그것도 모르면 ctx% 를 안 그립니다 |
| `context` | 레코드 어디서든 발견되면 파일 단위로 기억할 값 (`cwd`, `model`, `session`, `vendor`) |
| `default_model` | 모델을 못 찾았을 때 가격 계산에 쓸 기본값 |
| `vendor` | 레코드에 벤더가 없을 때의 기본값 (그것도 없으면 모델명에서 추론) |
| `plan` / `plan_probe` | 구독 vs 종량제 판정 — [비교의 축](#구독-vs-api-는-로그에-없습니다) 참고 |
| `endpoint` / `endpoint_probe` | 통신 대상 URL 판정 (플래그 → 환경변수 → 설정 파일 → 기본값) |
| `install.target` | `claude_json`(hooks JSON 병합) \| `opencode_plugin`(.js 생성) \| `none`(로그 감시만) |
| `install.path` | 대상 파일 |
| `install.events` | 등록할 이벤트 이름 (`SessionStart`, `SessionEnd` …). 중간 이벤트(`UserPromptSubmit` 등)를 넣으면 장시간 세션의 생존 신호 + 데몬 자가치유가 붙습니다 |

### 그리고 반드시 doctor 로 검증

```bash
tokenmeter doctor gemini-cli
```

```
[Gemini CLI] gemini-cli  (format=json, mode=delta, key=id)
   로그 파일 : ~/.gemini/tmp/xxx.json  외 최근 39개
   레코드    : 전체 40개 / match {'type': 'assistant'} 일치 33개
  필드         dot-path                    합계     비고
  input        usage.promptTokenCount     862,991  OK
  output       usage.candidatesTokenCount   9,719  ⚠ 전부 None — dot-path 확인
   context.cwd   : cwd → /Users/me/proj
   추출 델타 : 32건 · 1,024,774 토큰 · $2.7804
   감지 세션 : 6개  (예: ses_0447ea610ffenII1Xdpw)
   감지 벤더 : google   (기본값 모델명에서 추론)
   요금제    : unknown (판정 불가)  ⚠ plan 또는 plan_probe 를 설정하세요
```

- `일치 0개` → `match` 의 dot-path 나 값이 틀렸습니다 (관측된 실제 값을 같이 찍어줍니다)
- `⚠ 전부 None` → `fields` 의 dot-path 가 틀렸습니다
- `context ⚠ 못 찾음` → 프로젝트/모델 판별이 안 되어 `default_model` 로 계산됩니다
- `감지 세션 ⚠ 없음` → `context.session` 이 없어 **세션 수 비교에서 이 서비스가 빠집니다**
- `요금제 unknown` → 요금제 비교에서 빠집니다. `plan:` 을 직접 적어 주세요

---

## 데이터 파일

아래 `<상태 디렉터리>`는 macOS에서 `~/Library/Application Support/tokenmeter`, Linux에서
`${XDG_STATE_HOME:-~/.local/state}/tokenmeter`입니다. `TOKENMETER_HOME`으로 바꿀 수 있습니다.

| 경로 | 내용 |
|---|---|
| `<상태 디렉터리>/state.json` | 누적/오늘/세션 + 벤더·요금제·모델·클라이언트·프로젝트별 토큰·비용·호출·세션, 최근 세션 기록(`session_history` 개), 일별 기록(`days`, 최근 60일). writer 는 데몬 하나 |
| `<상태 디렉터리>/leaderboard.json` | 마지막으로 받은 자체 호스팅 랭킹 + 동기화 상태 |
| `<상태 디렉터리>/live/*.json` | 세션당 1개. 훅이 만들고 종료 훅이 지웁니다. 그 세션의 라우팅 환경(`routing_env`)이 여기 담깁니다 |
| `<상태 디렉터리>/history/*.json` | 세션 종료 스냅샷 |
| `<상태 디렉터리>/overlay.json` | 오버레이 위치·크기·랭킹 접힘·오늘/누적 |
| `<상태 디렉터리>/toggle.json` | `on`/`off`/`meter` 스위치. **훅도 읽으므로 JSON** (훅은 yaml 을 못 씁니다). 없거나 깨지면 전부 켜진 것으로 봅니다 |
| `~/.config/tokenmeter/prices.json` | `price set` 이 쓰는 모델 단가·컨텍스트 창 오버라이드. 기본 가격표를 이깁니다. 깨져 있으면 조용히 무시하고 기본 표로 계산합니다 |
| `<상태 디렉터리>/tokenmeter.pid`, `daemon.lock`, `daemon.log` | 데몬 중복 기동 방지 + 로그 |

`~/.claude/settings.json` 등을 처음 고칠 때 `<파일>.bak-tokenmeter` 백업을 남깁니다.

기존 펫 버전(`state.json` v1)에서 올라오면 **누적 토큰은 그대로 이어지고 레벨·경험치만 버려집니다.**

## 구조 (Layered Architecture)

```
Presentation    tokenmeter/overlay.py   tokenmeter/cli.py   tokenmeter/hook.py   tokenmeter/demo.py
Application     tokenmeter/meter.py     tokenmeter/leaderboard.py
Domain          tokenmeter/pricing.py   tokenmeter/endpoints.py
Infrastructure  tokenmeter/watcher.py   tokenmeter/installer.py   tokenmeter/config.py
```

`tokenmeter/cli.py` 와 `tokenmeter/hook.py` 는 **표준 라이브러리만**으로 돌아갑니다.
훅이 CLI 를 불러 데몬을 띄우는 구조라, 여기에 무거운 의존성이 끼면 연동 전체가 조용히 죽습니다.
`tokenmeter/leaderboard.py` 도 같은 이유로 `urllib` 만 씁니다.

## 검증

```bash
python3 test_tokenmeter.py        # 3사 실제 로그 샘플 파싱, prime, 버킷 누적, 호출·세션 집계,
                                # 요금제/벤더/엔드포인트 판정, 업로드 익명화, 훅 멱등성, 가격,
                                # 컨텍스트 추출, 캐시 절감, 단가 오버라이드, 유휴 알림 판정
python3 -m tokenmeter.overlay          # 미터 물리(차오르고 멈춤) + 레이아웃
python3 -m tokenmeter.leaderboard      # 랭킹 정렬 / 업로드 범위 / 오프라인 폴백
python3 -m tokenmeter.endpoints        # 엔드포인트 판별 + 공개/비공개 분류
```

실제 `~/.claude` 나 사용자 상태 파일은 건드리지 않고 전부 임시 디렉터리에서 돕니다.
