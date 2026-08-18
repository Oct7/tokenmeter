# TokenMeter 훅

훅이 세션 시작/종료를 알려주면 데몬이 알아서 뜨고, 라이브 세션이 없어지면 알아서 죽습니다.
**평소에 켜고 끌 일은 없습니다** — 잠시 멈추고 싶을 때만 `off` 를 씁니다.

```bash
tokenmeter install     # 등록 (멱등 — 여러 번 돌려도 엔트리는 1개)
tokenmeter uninstall   # 해제 (우리 엔트리만 제거)
tokenmeter services    # 어디에 무엇이 붙었는지 확인

tokenmeter off         # 측정 일시 정지 (훅은 남김 — 아래 참고)
tokenmeter on
tokenmeter meter off   # 창만 끄기 (측정은 계속)
```

`off` 는 사용자 상태 디렉터리의 `toggle.json`에 플래그를 쓰고, 훅이 그걸 보고 즉시 물러납니다. 훅 엔트리는
설정 파일에 남아 있으므로 `on` 하면 재설치 없이 곧바로 돌아옵니다. **이 파일이 JSON 인 이유는
훅이 읽어야 하기 때문입니다** — 훅은 yaml 을 import 할 수 없어 `services.yaml` 을 못 봅니다.

수동으로 JSON 을 편집할 필요는 없습니다. 아래는 `install` 이 실제로 무엇을 넣는지에 대한 설명입니다.

## 훅 엔트리

```
python3 /절대경로/tokenmeter/tokenmeter/hook.py <서비스> <이벤트>
```

- `SessionStart` → 사용자 상태 디렉터리의 `live/<서비스>__<세션>.json` 기록 + 데몬 보장 기동
- `Stop` / `session.idle` → 라이브 파일을 지우지 않고 상태를 `대기`로 바꾼다 (턴 완료)
- `SessionEnd` / `session.deleted` → 해당 라이브 파일 삭제
- 그 밖의 이벤트 → 라이브 파일 mtime 갱신 (생존 신호)

지켜지는 규칙:

- **stdout 에 아무것도 쓰지 않습니다.** Claude Code 는 SessionStart 훅의 stdout 을 컨텍스트에 주입합니다.
- 무슨 일이 있어도 `exit 0`. 훅이 에이전트를 막는 일은 없습니다.
- 표준 라이브러리만 import 합니다 (기동 시간이 곧 비용).
- 데몬 기동은 상태 디렉터리의 `daemon.lock`으로 경합을 막아 여러 세션이 동시에 떠도 하나만 뜹니다.

### 환경변수

| 변수 | 효과 |
|---|---|
| `TOKENMETER_DISABLE=1` | 훅이 즉시 종료 (측정 일시 중지) |
| `TOKENMETER_DEBUG=1` | 진단 메시지를 stderr 로 (stdout 은 여전히 무음) |
| `TOKENMETER_CWD` | stdin 에 cwd 가 없을 때 쓸 작업 디렉토리 |
| `TOKENMETER_NO_DAEMON=1` | 데몬 기동만 건너뜀 (테스트/CI 용) |

## 서비스별로 설치되는 것

| 서비스 | 대상 | 형태 |
|---|---|---|
| Claude Code | `~/.claude/settings.json` | `hooks.SessionStart` / `hooks.SessionEnd` 배열에 그룹 1개 append |
| Codex | `~/.codex/hooks.json` | 같은 스키마 |
| Grok CLI | `~/.grok/hooks/tokenmeter.json` | Claude 와 같은 JSON 훅 파일 |
| OpenCode | `~/.config/opencode/plugin/tokenmeter.js` | ESM 플러그인 파일 생성 (`tokenmeter:generated` 마커) |

Grok 가 `~/.claude/settings.json` 훅을 compat 로 실행해도, 받은 세션 id 가 `GROK_SESSION_ID` 와 같을 때만 `grok` 라이브 파일로 옮깁니다. 셸에 변수가 남아 있는 것만으로는 Claude Code 세션을 가로채지 않습니다.

```json
{
  "matcher": "",
  "hooks": [
    {
      "type": "command",
      "command": "\"/경로/python3\" \"/경로/tokenmeter/tokenmeter/hook.py\" claude-code SessionStart",
      "timeout": 5
    }
  ]
}
```

기존 키·순서·값은 건드리지 않고, 첫 수정 전에 `<파일>.bak-tokenmeter` 백업을 남깁니다.
재설치 시 새 그룹을 만들지 않고 기존 엔트리의 `command` 만 제자리에서 갱신합니다.

## 수동 등록 (훅을 못 쓰는 환경)

```bash
tokenmeter start --service manual   # 라이브 세션 등록 + 데몬 기동
tokenmeter stop  --service manual   # 해제 (history 스냅샷 저장)
```
