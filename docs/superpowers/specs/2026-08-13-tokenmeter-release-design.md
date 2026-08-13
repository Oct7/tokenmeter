# TokenMeter 배포·리브랜딩 설계

**작성일:** 2026-08-13
**상태:** 사용자 승인됨

## 목표

현재 비공개 소스 체크아웃에서만 실행되는 TokenPet을 `TokenMeter`로 전면 변경하고, 처음 보는 사용자가 저장소를 복제하지 않아도 한 줄로 설치한 뒤 첫 AI 에이전트 세션을 측정할 수 있는 공개 배포물로 만든다.

핵심 사용자는 Claude Code, Codex, OpenCode 중 하나 이상을 사용하는 개발자다. 제품의 첫 약속은 “여러 AI 코딩 에이전트가 일하는지, 끝났는지, 컨텍스트가 찼는지를 로컬에서 한눈에 보여준다”이다.

## 결정

### 이름

- 제품 표시명: `TokenMeter`
- GitHub 저장소: `Oct7/tokenmeter`
- Python 배포 이름: `oct7-tokenmeter` (`tokenmeter`는 이미 다른 PyPI 소유자가 사용 중)
- Python import 패키지: `tokenmeter`
- 콘솔 명령: `tokenmeter`
- 사용자 설정·데이터 디렉터리와 환경변수 접두사: `tokenmeter`, `TOKENMETER_`
- 스킬 이름과 기본 슬래시 명령: `tokenmeter`, `/tm`, `/tm-meter`, `/tm-measure`, `/tm-doctor`
- 이전 `TokenPet`, `tokenpet`, `token-pet`, `/tp*` 표기는 레거시 훅·데이터 이전 코드와 변경 이력에서만 허용한다.

공개 출시 전 버전이므로 이전 Python import나 CLI 이름을 유지하는 호환 패키지는 만들지 않는다. 단, 현재 개발자의 기록과 설치된 훅을 잃지 않도록 기존 데이터는 복사 이전하고 이전 훅 엔트리는 새 엔트리로 교체한다.

### 배포 방식

세 접근을 비교했다.

1. **Python 패키지 + uv/pipx 설치 — 채택.** 기존 Python/PyQt 코드를 그대로 사용하고 `uv tool install git+https://github.com/Oct7/tokenmeter.git` 또는 `pipx install git+...` 한 줄로 설치한다. 가장 작은 변경으로 격리된 실행 환경과 안정된 콘솔 명령을 얻는다.
2. **서명된 macOS 앱 + Homebrew Cask — 보류.** 사용자 경험은 가장 좋지만 앱 번들, 코드 서명, 공증, 자동 업데이트가 별도 배포 시스템을 요구한다. Python 배포의 실제 수요와 실패 데이터를 확인한 뒤 진행한다.
3. **curl 설치 스크립트 — 제외.** 빠르게 보이지만 원격 셸 실행에 대한 신뢰 장벽과 업데이트·삭제 책임이 커진다.

`pyproject.toml`은 setuptools를 사용하고 Python 3.10 이상을 지원한다. 기본 설치에 PyYAML, PyQt6, watchdog을 포함해 사용자가 GUI extra를 알아낼 필요가 없게 한다. 배포 이름은 충돌 없는 `oct7-tokenmeter`, import와 `tokenmeter = tokenmeter.cli:main` 콘솔 엔트리 포인트는 제품명과 동일하게 유지한다. 첫 배포는 공개 GitHub 저장소와 GitHub Release를 기준으로 하며 PyPI 배포는 별도 자격 증명이 준비될 때 추가한다.

### 파일과 데이터

설치된 패키지 디렉터리는 업데이트되거나 읽기 전용일 수 있으므로 런타임 데이터를 그 안에 쓰지 않는다.

- macOS: `~/Library/Application Support/tokenmeter`
- Linux: `${XDG_STATE_HOME:-~/.local/state}/tokenmeter`
- Windows: `${LOCALAPPDATA}/tokenmeter`
- 테스트·이식 실행 오버라이드: `TOKENMETER_HOME`
- 사용자 설정: `${XDG_CONFIG_HOME:-~/.config}/tokenmeter`

기본 서비스 레지스트리는 `tokenmeter/services.yaml` 패키지 데이터로 포함한다. 사용자 설정은 기본값 위에 깊은 병합한다.

새 데이터 디렉터리가 비어 있고 소스 체크아웃의 기존 `data/`가 있으면 첫 실행 때 복사한다. 기존 파일은 삭제하지 않는다. `~/.config/tokenpet`도 새 설정 디렉터리가 없을 때만 복사한다. 이 안전한 1회 이전은 실패해도 새 실행을 막지 않는다.

### 설치와 활성화

README의 기본 경로는 다음 세 단계다.

```bash
uv tool install git+https://github.com/Oct7/tokenmeter.git
tokenmeter install
tokenmeter status
```

`pipx` 대안을 바로 아래에 둔다. 소스 체크아웃 설치법과 아키텍처 설명은 고급 문서로 내린다.

`tokenmeter install`은 훅 설치 결과와 감지된 서비스 상태를 보여준 뒤 다음 행동을 번호로 출력한다.

1. 사용 중인 Claude Code/Codex/OpenCode를 완전히 다시 연다.
2. 새 프롬프트를 한 번 실행한다.
3. 오버레이가 뜨거나 `tokenmeter status`에 첫 측정이 보이는지 확인한다.
4. 보이지 않으면 `tokenmeter doctor`를 실행한다.

PyQt6를 import할 수 없으면 설치 명령을 다시 제안하는 대신 현재 인터프리터와 패키지 경로를 포함한 명확한 진단을 출력한다. 데이터가 아직 없을 때의 상태 화면은 오류처럼 보이지 않도록 “첫 세션 대기 중”과 동일한 다음 행동을 보여준다.

### 메시지와 문서

`README.md`는 영문 기본 랜딩 페이지, `README.ko.md`는 같은 구조의 한국어 문서로 만든다. 첫 화면에는 다음만 둔다.

- 한 문장 가치 제안
- 지원 에이전트와 로컬 우선 개인정보 약속
- 실제 오버레이 화면 또는 터미널 프리뷰
- 한 줄 설치와 첫 측정 단계
- 기능 4개: 실시간 세션, 컨텍스트 경고, 비용·히스토리, 완료 알림
- 삭제/비활성화 방법

기존의 서비스 레지스트리, 가격 판정, 엔드포인트 분류, Supabase 예시는 고급 문서로 보존한다. “글로벌 랭킹”은 기본 기능처럼 약속하지 않고 “선택형 자체 호스팅 랭킹”으로 정확히 표현한다. 중앙 랭킹이 실제 제공되기 전에는 사용자가 혼자 보게 되는 화면을 글로벌이라고 부르지 않는다.

권장 헤드라인은 다음과 같다.

> See when your AI coding agents are working, finished, or running out of context.

한국어 대응 문구:

> 여러 AI 코딩 에이전트가 일하는지, 끝났는지, 컨텍스트가 찼는지 한눈에.

### 저장소 공개

코드 변경과 검증이 끝난 뒤 다음 순서로 외부 상태를 변경한다.

1. 작업 트리와 전체 Git 이력에서 비밀정보를 검사한다.
2. MIT `LICENSE`, 저장소 설명, 토픽, 설치 문서를 확인한다.
3. 기존 사용자 변경을 포함한 출시 트리를 명시적으로 검토한다.
4. 개인 홈 경로와 내부 fixture가 포함된 비공개 개발 이력은 push하지 않고, 검증된 현재 트리로 부모 없는 공개 `main` 커밋 하나를 만든다. 작성자 이메일은 GitHub noreply 주소를 사용한다.
5. 새 `main`만 private 원격에 push한 뒤 다시 비밀정보를 검사한다.
6. GitHub 저장소를 `Oct7/tokenmeter`로 이름 변경하고 public으로 전환한다.
7. `v0.1.0` GitHub Release를 만든다.

비밀정보 검사나 공개할 단일 커밋 검토에서 의심 항목이 하나라도 나오면 public 전환은 중단하고 로컬 코드까지만 완료한다. 이전 개발 이력은 로컬 feature 브랜치에만 보존한다.

## 이번 범위에서 제외

- 서명된 `.app`, Homebrew Cask, Windows 설치 프로그램
- 중앙 계정·인증·공개 글로벌 랭킹 서버
- 주간 공유 PNG와 펫 성장 시스템
- 사용량 텔레메트리
- 새 AI 에이전트 파서 추가

공유 카드와 중앙 랭킹은 제품 활성화가 확인된 뒤 별도 작업으로 진행한다. 현재 자체 호스팅 랭킹 클라이언트는 유지하되 기본 설치·온보딩 경로에서는 숨긴다.

## 오류 처리와 안전

- 훅 설치는 기존과 동일하게 다른 훅을 보존하고 백업하며 멱등이어야 한다.
- 레거시 데이터 이전은 복사만 하고 원본을 삭제하지 않는다.
- 설정이나 데이터 이전 실패는 stderr 진단을 남기되 에이전트 실행을 막지 않는다.
- GUI가 없어도 계량과 CLI는 계속 동작한다.
- 공개 전환은 로컬 테스트, 빌드, 임시 격리 설치, 비밀정보 검사를 모두 통과한 뒤에만 수행한다.

## 검증 기준

- `python3 test_tokenmeter.py`의 전체 자가검증 통과
- 헤드리스 오버레이·랭킹·엔드포인트 자가검증 통과
- wheel과 sdist 빌드 성공
- 빈 임시 홈에서 wheel을 설치하고 `tokenmeter --help`, `tokenmeter services`, `tokenmeter install --dry-run` 성공
- 소스와 사용자-facing 문서에 허용되지 않은 이전 이름이 남지 않음
- `tokenmeter install` 출력만 읽고 첫 측정까지의 다음 행동을 알 수 있음
- 전체 Git 이력 비밀정보 검사 통과 후에만 GitHub public 전환
