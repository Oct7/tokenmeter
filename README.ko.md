# TokenMeter

**여러 AI 코딩 에이전트가 일하는지, 기다리는지, 컨텍스트가 찼는지 한눈에.**

TokenMeter는 Claude Code, Codex, OpenCode를 위한 로컬 우선 데스크톱 미터입니다. 여러 세션을 **확인·작업·대기·종료** 상태로 보여주고 사용량 히스토리를 로컬에 기록합니다.

[English](README.md) · [상세 레퍼런스](docs/reference.ko.md) · [새 에이전트 추가](docs/add-service.md)

```text
┌────────────────────────────────────────────────────┐
│ TOKENMETER                                    오늘 │
│ 478 전체 출력 tok/s                 API 환산 $15.8599 │
│ ██████████████░░░░░░░░░░░░░░░░░░░░░░░░           ▏ │
│ 입력 1.2M         출력 84.0k        캐시 23.5M     │
├────────────────────────────────────────────────────┤
│ 상태   프로젝트       메인/s      누적    컨텍스트  │
│ 작업   api-server       412/s      84.0k      31%  │
│ 대기   web-client          —      21.7k      75%  │
│ 확인   mobile              3/s       8.1k  95% · 높음 │
└────────────────────────────────────────────────────┘
```

## TokenMeter를 쓰는 이유

- **무엇이 실제로 일하는지 확인합니다.** 세션을 확인·작업·대기·종료 상태로 보여줍니다.
- **컨텍스트 압력을 봅니다.** 컨텍스트 점유율이 70%, 90%를 넘으면 색이 바뀝니다. Context Runway나 압축 시점 예측은 구현하지 않았습니다.
- **사용량을 로컬에서 이해합니다.** 토큰, API 환산 비용, 캐시 절감, 프로젝트, 모델, 일별 기록을 확인합니다.
- **터미널을 계속 보지 않아도 됩니다.** 세션이 명시적으로 `확인`으로 전환될 때만 데스크톱 알림이 옵니다.

TokenMeter는 로컬 에이전트 로그를 읽습니다. API 키가 필요 없고 프롬프트 내용도 저장하지 않습니다. 측정 자체는 로컬에서만 이뤄집니다. 선택형 한도 화면은 이미 로그인된 Claude, Codex, Grok 자격 증명으로 잔여 플랜 창을 읽습니다.

## 설치

요구사항은 macOS 또는 Linux, Python 3.10 이상입니다. [uv](https://docs.astral.sh/uv/)를 사용하면 격리된 환경에 한 줄로 설치됩니다.

```bash
uv tool install git+https://github.com/Oct7/tokenmeter.git
tokenmeter install
```

pipx를 사용해도 됩니다.

```bash
pipx install git+https://github.com/Oct7/tokenmeter.git
tokenmeter install
```

첫 측정을 활성화합니다.

1. Claude Code, Codex, OpenCode를 완전히 다시 엽니다.
2. 새 프롬프트를 한 번 실행합니다.
3. 오버레이가 뜨는지 확인하거나 `tokenmeter status`를 실행합니다.
4. 보이지 않으면 `tokenmeter doctor`를 실행합니다.

기존 TokenPet 훅과 로컬 데이터는 안전하게 감지해 복사합니다. 옛 파일은 삭제하지 않습니다.

## 지원 에이전트

| 에이전트 | 로컬 사용량 | 자동 생명주기 훅 |
|---|---:|---:|
| Claude Code | 지원 | 지원 |
| Codex | 지원 | 지원 |
| OpenCode | 지원 | 지원, 플러그인 자동 생성 |
| Grok CLI | 지원 | 지원, Grok 이 읽는 Claude Code 훅을 그대로 탄다 |

로그가 있는 다른 에이전트도 설정만으로 추가할 수 있습니다. [서비스 추가 가이드](docs/add-service.md)를 참고하세요.

## 주요 명령

```bash
tokenmeter status --json
tokenmeter watch --jsonl
tokenmeter receipt --format markdown
tokenmeter adapter init gemini-cli --log ~/.gemini/tmp
tokenmeter adapter check ./gemini-cli-adapter
tokenmeter team --sync
tokenmeter quota                  # Claude/Codex/Grok 잔여 한도
tokenmeter services               # 로그 감지와 훅 상태
tokenmeter doctor                 # 파서와 설치 검증
tokenmeter meter off              # 창만 숨기고 측정은 유지
tokenmeter update on              # 하루 한 번 정식 릴리스 자동 업데이트
tokenmeter off                    # 측정을 멈추고 훅은 유지
tokenmeter on                     # 측정 재개
tokenmeter uninstall              # TokenMeter 훅만 제거
```

오버레이는 드래그로 옮기고 휠로 세션 목록을 스크롤합니다. 화면에 보이는 `S/M/L` 버튼으로 미터만·패널·상세 레이아웃을 고르고, `⋯`나 우클릭으로 명시적인 전체 메뉴를 엽니다. `×`는 오버레이만 숨기며 측정은 계속됩니다. 측정까지 끝내려면 메뉴나 트레이의 `TokenMeter 종료 · 측정 중지`를 고릅니다.

전역 `tokens/s`는 서브에이전트를 포함한 전체 출력 처리량입니다. 세션·모델 `tok/s`는 서브에이전트 출력을 빼고 메인 모델의 처리량을 보여줍니다. 둘 다 제공자의 실제 스트리밍 생성 속도 벤치마크가 아니라 로그 델타 도착률입니다. 세션 표는 상태·누적 출력·컨텍스트 점유율을 서로 다른 칸에 표시합니다. 선택형 팀 탭은 endpoint가 오프라인이면 숨겨집니다.

## 에이전트 스킬

선택형 스킬을 설치하면 호환되는 코딩 에이전트가 자연어로 TokenMeter를 조작할 수 있습니다.

```bash
npx skills add Oct7/tokenmeter -g -a claude-code
```

`/tm`, `/tm-meter`, `/tm-measure`, `/tm-doctor`가 추가됩니다.

## 개인정보와 데이터

- 로컬 라이브 파일에는 허용된 세션·라우팅 메타데이터와 정규화된 이벤트·관심 시각을 저장합니다. 프롬프트·응답 내용, 툴 명령, 파일명은 저장하지 않습니다.
- 공개 JSON과 팀 출력은 더 엄격히 걸러 내부 경로·세션 ID·라우팅 URL·세션 내용을 제외합니다. 어댑터 fixture는 값을 지운 뒤 생성합니다.
- macOS 상태 경로는 `~/Library/Application Support/tokenmeter`, Linux는 `${XDG_STATE_HOME:-~/.local/state}/tokenmeter`입니다.
- 사용자 설정은 `${XDG_CONFIG_HOME:-~/.config}/tokenmeter`에 있습니다.
- 선택형 한도 화면과 `tokenmeter quota`는 Claude, Codex, Grok CLI가 이미 저장한 자격 증명으로 잔여 플랜 창을 읽습니다. 세션 로그와 프롬프트 내용은 보내지 않습니다.
- 선택형 자체 호스팅 랭킹은 기본적으로 꺼져 있습니다. 팀 동기화는 기존 endpoint를 재사용하며, 새 라이브 세션 정보로는 `today` 안의 관심 상태 집계만 추가합니다. TokenMeter는 호스팅 서비스를 제공하지 않습니다.

## 업데이트와 제거

자동 업데이트는 기본적으로 꺼져 있습니다. 데몬 시작 시 하루 한 번 확인하도록 켜거나 지금 바로 업데이트할 수 있습니다.

```bash
tokenmeter update on
tokenmeter update now
```

GitHub의 정식 릴리스만 설치합니다. 다시 끄려면 `tokenmeter update off`를 실행합니다.

수동 업데이트:

```bash
uv tool install --force git+https://github.com/Oct7/tokenmeter.git
tokenmeter install                # 훅의 절대 경로 갱신
```

격리 도구를 지우기 전에 훅을 먼저 제거합니다.

```bash
tokenmeter uninstall
uv tool uninstall oct7-tokenmeter
```

## 개발

```bash
git clone https://github.com/Oct7/tokenmeter.git
cd tokenmeter
uv sync
uv run python test_tokenmeter.py
uv run tokenmeter install --dry-run
```

TokenMeter는 [MIT 라이선스](LICENSE)로 배포됩니다.
