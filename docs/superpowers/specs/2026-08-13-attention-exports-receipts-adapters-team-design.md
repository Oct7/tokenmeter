# TokenMeter 관심 상태·내보내기·영수증·어댑터·팀 설계

**작성일:** 2026-08-13  
**상태:** 사용자 승인됨

## 목표

TokenMeter를 여러 AI 코딩 에이전트 중 지금 사용자가 봐야 할 세션을 짧게 알려주는 관제 도구로 확장한다. 기존 로컬 계량·오버레이·자체 호스팅 동기화 구조를 재사용하고 새 서버나 새 의존성은 추가하지 않는다.

이번 범위는 다음 네 묶음이다.

1. 세션별 관심 상태와 짧은 표시
2. 기계가 읽을 수 있는 상태 스트림
3. 세션 영수증
4. 커뮤니티 어댑터 생성·검증과 자체 호스팅 팀 현황

## 범위 밖

- Context Runway, 압축 횟수·예측·추가 경고는 구현하지 않는다.
- 프롬프트, 응답, 마지막 어시스턴트 메시지, 도구 입력, 명령, 파일명 등 세션 내용은 저장하거나 전송하지 않는다.
- 중앙 TokenMeter 서버, 계정, 인증, 웹 대시보드, 원격 제어는 만들지 않는다.
- 자동 팝업 영수증, 클립보드 조작, PNG·SVG 생성은 만들지 않는다.
- JSON 소비자가 생기기 전까지 로컬 HTTP 서버와 Prometheus exporter는 만들지 않는다.

## 1. 관심 상태

### 사용자 표현

상태명은 네 단어만 사용한다.

| 상태 | 의미 | 근거 |
|---|---|---|
| `확인` | 사용자 입력이나 승인이 필요함 | 완료·승인·질문 훅 이벤트 |
| `작업` | 최근 출력 토큰이 유입됨 | 세션의 마지막 토큰 유입이 30초 이내 |
| `대기` | 세션은 열려 있지만 위 두 조건이 아님 | 라이브 파일은 존재하나 명시적 관심 이벤트가 없음 |
| `종료` | 최근 기록이지만 라이브 세션은 아님 | 라이브 파일 없음 |

정렬 우선순위는 `확인 → 작업 → 대기 → 종료`, 같은 상태에서는 기존처럼 tok/s와 마지막 활동 시각을 사용한다. 30초는 판정 상수로 두고 설정 항목은 늘리지 않는다. 컨텍스트 비율 표시는 그대로 유지하지만 새 예측 로직은 넣지 않는다.

오버레이 세션 줄에는 긴 설명 대신 상태 한 단어만 표시한다. 데스크톱 알림도 `web-client · 확인 필요`처럼 프로젝트와 행동만 한 줄로 보낸다.

### 이벤트 매핑

훅은 이벤트 이름과 시각만 라이브 파일에 기록한다. 이벤트 payload의 내용 필드는 보관하지 않는다.

| 서비스 | `확인`으로 전환 | `작업`으로 전환 | 종료 처리 |
|---|---|---|---|
| Claude Code | `PermissionRequest`, `Notification`, `Stop` | `UserPromptSubmit`, 토큰 유입 | `SessionEnd` |
| Codex | `PermissionRequest`, `Stop` | `UserPromptSubmit`, 토큰 유입 | `SessionEnd` |
| OpenCode | `permission.asked`, `question.asked`, `session.idle` | 토큰 유입 및 실행 상태 이벤트 | 세션 삭제·종료 이벤트가 있으면 제거, 아니면 기존 TTL |

`Notification`은 `permission_prompt`, `idle_prompt`, `elicitation_dialog`처럼 사용자의 관심이 필요한 유형만 `확인`으로 취급한다. 훅이 명시적 신호를 제공하지 않는 서비스는 조용하다는 이유만으로 `확인`을 만들지 않고 `대기`로 둔다.

Codex의 `PermissionRequest` payload가 자동 검토자를 명시하면 `확인`으로 올리지 않는다. 이 필드가 없는 구버전은 실제 승인 요청을 놓치지 않도록 `확인`으로 취급한다.

토큰 유입은 이전의 `확인` 상태를 `작업`으로 덮는다. 새 사용자 프롬프트도 `작업`으로 되돌린다. 이 규칙으로 한 번 확인된 세션이 영원히 상단에 남는 것을 막는다.

### 단일 판정 위치

상태 판정은 `tokenmeter/meter.py`의 순수 헬퍼 한 곳에 둔다. 오버레이, 텍스트 CLI, JSON, 팀 업로드가 모두 같은 결과를 사용해 서로 다른 상태를 말하지 않게 한다.

현재 전체 마지막 유입을 보는 유휴 알림은 세션별 `확인` 전환 알림으로 교체한다. 동일한 세션·이벤트 시각은 한 번만 알리고, 알림 실패는 측정을 멈추지 않는다.

## 2. JSON 상태와 스트림

### 명령

```bash
tokenmeter status --json
tokenmeter watch --jsonl
```

`status --json`은 사람이 읽는 표 대신 현재 스냅샷 하나를 stdout에 출력한다. `watch --jsonl`은 읽기 전용으로 `state.json`과 라이브 파일의 mtime을 폴링하므로 실행 중인 데몬과 충돌하지 않는다. 시작 시 `snapshot` 한 줄 뒤 합계 증가분이나 관심 상태 변경 때 `delta` 또는 `attention` 한 줄을 출력한다. 여러 유입이 폴링 사이에 겹치면 한 `delta`로 합쳐진다. Ctrl+C로 종료한다.

모든 레코드에는 `schema_version: 1`, `type`, `timestamp`가 있다. 공개 상태는 합계, 집계 축, 라이브 세션의 서비스·프로젝트·모델·상태·시각·ctx 수치만 포함한다. tok/s는 오버레이 프로세스 안에서만 계산되므로 스냅샷에는 지어내지 않는다. 내부 경로, 전체 세션 내용, 훅 payload 내용은 제외한다.

JSON 직렬화는 표준 라이브러리 `json`을 사용한다. 파이프가 먼저 닫히면 기존 `BrokenPipeError` 처리로 정상 종료한다. JSON 모드의 stdout에는 안내 문구나 로그를 섞지 않는다.

## 3. 세션 영수증

### 명령

```bash
tokenmeter receipt
tokenmeter receipt --format markdown
tokenmeter receipt --format json
```

기본 대상은 `state["sessions"]`에서 `last_seen`이 가장 최근인 세션이다. 기록이 없으면 비정상 종료가 아니라 명확한 한 줄과 종료 코드 1을 반환한다. 세션 선택 옵션은 실제 요구가 생길 때 추가한다.

기본 형식은 터미널용 짧은 텍스트다. `markdown`은 복사 가능한 코드 블록 없는 요약, `json`은 같은 데이터를 구조화해 출력한다.

포함 필드는 프로젝트 별칭, 클라이언트, 마지막 모델·effort, 시작·마지막 시각, 토큰 4종, 호출 수, 캐시 절감액, 서브에이전트 비용 비율, 컨텍스트 최고치가 아닌 마지막 관측치다. 저장하지 않은 데이터는 복원하거나 추정하지 않는다.

금액 명칭은 세션의 요금제에 따라 고정한다.

- `api`: `예상 사용액`
- `subscription`: `API 환산 가치`
- `unknown` 또는 혼합 판정 불가: `API 환산가`

영수증 생성은 읽기 전용이며 새 기록을 만들지 않는다. 기존 세션 집계만 포맷한다.

## 4. Adapter Kit

### 명령과 산출물

```bash
tokenmeter adapter init gemini-cli --log ~/.gemini/tmp
tokenmeter adapter check ./gemini-cli-adapter
```

`init`은 지정한 로그 경로에서 가장 최근 JSON 또는 JSONL 레코드를 읽어 다음 두 파일만 만든다.

```text
gemini-cli-adapter/
├── service.yaml
└── fixture.json
```

`fixture.json`은 원래 값을 담지 않는다. 객체 키와 배열 구조는 유지하되 문자열은 `""`, 숫자는 `0`, 불리언은 `false`, null은 그대로 두어 스키마 모양만 남긴다. 키 이름에 `key`, `token`, `secret`, `password`, `credential`, `auth`, `cookie`가 들어가면 값의 형식과 관계없이 `"<redacted>"`로 바꾼다.

`service.yaml`은 서비스 이름, 로그 root·pattern·format과 발견 가능한 토큰 필드 후보를 채운 최소 초안이다. match, delta/cumulative, 세션·모델 필드처럼 자동 판정이 위험한 값에는 작동하는 척하는 기본값을 넣지 않고 주석으로 사용자가 확인할 항목을 남긴다.

`check`는 디렉터리의 두 파일을 읽어 기존 `ServiceSpec`과 `ServiceReader`가 요구하는 구조와 dot-path를 검증한다. 익명 fixture의 숫자는 0이므로 실제 사용량을 재현하지 않으며, 성공 시 연결된 토큰 필드만 짧게 출력하고 실패 시 수정할 dot-path를 알려준다. 별도 로그 파서는 만들지 않는다.

출력 디렉터리가 이미 있고 비어 있지 않으면 덮어쓰지 않는다. 지원 파일을 찾지 못하거나 JSON을 읽지 못하면 아무 파일도 만들지 않고 실패한다.

## 5. 자체 호스팅 팀 현황

### 명령

```bash
tokenmeter team
tokenmeter team --sync
tokenmeter team --json
```

기존 `settings.leaderboard.endpoint`, headers, handle, 동기화 캐시를 그대로 사용한다. 별도 team endpoint나 인증 설정은 추가하지 않는다. endpoint가 비어 있으면 네트워크를 호출하지 않고 로컬 한 줄만 보여준다.

기존 서버 테이블 변경을 피하기 위해, 이미 JSON 객체로 전송되는 `today` 안에 다음 집계만 추가한다.

```json
{
  "today": {
    "attention": {
      "check": 1,
      "working": 3,
      "waiting": 1,
      "risk": 1
    }
  }
}
```

`risk`는 새 Context Runway가 아니라 기존 `ctx >= 90%`인 라이브 세션 수다. 상태와 독립된 보조 숫자다. 프로젝트, 경로, 세션 ID, 모델별 라이브 세션, 이벤트 내용은 올리지 않는다.

팀 표는 한 사람당 한 줄만 표시한다.

```text
핸들       확인  작업  대기  위험  오늘
alice         1     3     1     1  $12.40
```

서버가 아직 `attention`을 저장하지 않거나 돌려주지 않으면 모든 상태를 0으로 보고 기존 랭킹 동기화는 계속한다. 따라서 서버 변경 전후가 호환된다. `--json`은 서버 응답에서 허용된 팀 필드만 정규화해 출력한다.

## 데이터 흐름

```text
에이전트 훅/플러그인 ── 이벤트명·시각 ──> live/*.json
로그 워처 ── 토큰 델타 ──> Meter ──> state.json
                                  ├─> 오버레이 상태 한 단어
                                  ├─> status/watch JSON
                                  ├─> receipt
                                  └─> 익명 상태 개수 ──> 기존 자체 호스팅 endpoint
```

훅과 OpenCode 플러그인은 에이전트 실행 경로에 있으므로 실패해도 종료 코드 0을 유지한다. 상태·영수증·팀 출력은 모두 로컬 상태가 깨졌을 때 빈 구조로 강등하고, 쓰기나 동기화 실패로 계량을 중단하지 않는다.

## 파일 변경

- 수정: `tokenmeter/hook.py`, `installer.py`, `services.yaml` — 관심 이벤트 기록과 설치
- 수정: `tokenmeter/meter.py` — 공통 상태 판정
- 수정: `tokenmeter/overlay.py` — 한 단어 상태 표시와 정렬
- 수정: `tokenmeter/cli.py` — JSON, 스트림, 영수증, 어댑터, 팀 명령
- 수정: `tokenmeter/leaderboard.py` — 익명 관심 집계 업로드·팀 행 파싱
- 신규: `tokenmeter/adapter.py` — 익명 fixture와 YAML 초안
- 수정: `test_tokenmeter.py` — 각 비자명 경로의 최소 회귀 검사
- 수정: `README.md`, `README.ko.md`, `docs/reference.ko.md` — 명령과 개인정보 계약

새 패키지와 새 런타임 프로세스는 없다.

## 검증 기준

1. 동일한 입력에서 오버레이·CLI·팀 payload의 상태 개수가 일치한다.
2. 명시적 관심 이벤트만 `확인`이 되고, 토큰 유입이나 사용자 프롬프트가 이를 `작업`으로 해제한다.
3. `status --json`과 `watch --jsonl`의 모든 줄이 독립적으로 파싱되며 `schema_version`이 있다.
4. 영수증이 API와 구독 금액 명칭을 다르게 출력하고 읽기 전용으로 동작한다.
5. 어댑터 fixture에 원본 문자열·숫자·비밀값이 남지 않고, 기존 디렉터리를 덮어쓰지 않는다.
6. 팀 payload에 프로젝트·경로·세션 ID·내용이 없고 기존 서버 응답에서도 정상 동작한다.
7. 기존 `python3 test_tokenmeter.py` 전체 자가검증과 모듈별 자가검증이 통과한다.
