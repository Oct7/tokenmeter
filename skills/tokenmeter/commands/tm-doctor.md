---
description: TokenMeter 진단 — 훅이 어디에 붙었나 + 데몬 로그 꼬리
allowed-tools: Bash(~/.claude/skills/tokenmeter/tm:*), Bash(tail:*)
---

!`~/.claude/skills/tokenmeter/tm services`

!`~/.claude/skills/tokenmeter/tm status | head -8`

로그 꼬리:
!`tail -15 "$(~/.claude/skills/tokenmeter/tm status | sed -n 's/^  로그   : //p')"`

위 세 출력만 보고 진단하라. 훅이 `갱신 필요` 면 `install` 재실행, `PyQt6 없음` 이면
`uv tool install --force git+https://github.com/Oct7/tokenmeter.git`, `자동 표시 꺼짐` 이면
`/tm-meter on`. 짚이는 게 없으면 없다고 말하라.
추측으로 원인을 만들어내지 마라.
