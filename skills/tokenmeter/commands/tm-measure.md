---
description: TokenMeter 토큰 측정 켜기/끄기 (훅은 그대로 남김)
argument-hint: "on|off [--service claude-code|codex|opencode]"
allowed-tools: Bash(~/.claude/skills/tokenmeter/tm:*)
---

!`~/.claude/skills/tokenmeter/tm $ARGUMENTS`

측정 자체를 멈추거나 재개하는 스위치다 (`off` 는 데몬도 내린다). 훅은 남아 있으므로
`on` 이면 즉시 재개된다. 인자가 비어 있어 usage 가 떴다면 `on` 인지 `off` 인지만 되물어라.
