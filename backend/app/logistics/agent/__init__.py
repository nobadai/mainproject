"""재고·물류 Agent 층 — 창고 상태를 **지속되는 운영 문제**로 바꾼다 (#628 Core).

```text
Observe → Assess → Detect        ← Commit 2 (여기까지 · 전부 결정론)
       → Investigate → Propose   ← Commit 4 · 5 (LLM)
       → Approve → Act → Verify  ← Commit 5 · 6
```

🔴 **이 층은 창고를 바꾸지 않는다.** 원장·예약·폐기·입고를 쓰는 코드는 기존
   서비스뿐이고, 여기서는 **읽고 판단해서 한 표에 행을 남긴다**
   (`logistics_exceptions`). 그래서 걷기 안에 들어가도 재현성이 안 깨진다.

🔴 **Commit 2 에 LLM 이 없다.** 탐지·해소는 전부 결정론이고, 임계 비교는 기존
   `rules.py` · `tools.py` 함수를 **그대로 부른다** — 신선도 공식도 용량 공식도
   여기서 새로 적지 않는다.
"""
