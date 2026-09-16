"""물류 상태 관측·탐지 — 창고 상태를 **지속되는 운영 문제**로 바꾼다.

```text
Observe → Assess → Detect → (출고 뒤) Resolve      ← 전부 결정론 · LLM 없음
```

Daily Walk 가 매일 돈다(`master/inspection.py` → `detect_logistics_exceptions`). 이 층은
창고를 바꾸지 않는다 — 읽고 판단해서 `logistics_exceptions` 한 표에만 행을 남긴다.

★ 옛 `agent/` 의 Investigate → Propose → Approve → Act 흐름은 폐기했다(설계 폐기 ·
  07 §0 LOG-AGENT-002 / LOG-AGENT-001 DEFER). 남은 것은 관측·탐지·해소뿐이다.
"""
