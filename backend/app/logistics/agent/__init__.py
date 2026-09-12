"""재고·물류 Agent 층 — 창고 상태를 **지속되는 운영 문제**로 바꾼다 (#628 Core).

```text
Observe → Assess → Detect        ← Commit 2 (전부 결정론)
       → Investigate            ← Commit 4 (LLM 은 **Tool 을 고르기만** · 쓰기 0)
       → Propose → Approve      ← Commit 5 (사람이 결정한다 · **LLM 없음**)
       → Act → Verify           ← Commit 6
```

조사 Runtime 은 `graph.run_investigation` 하나로 들어간다. 그 층의 말은
`investigation.py`, guard 는 `tool_dispatch.py`, 공급자 전송은 `llm_client.py` 에 있다.

🔴 **이 층은 창고를 바꾸지 않는다.** 원장·예약·폐기·입고를 쓰는 코드는 기존
   서비스뿐이고, 여기서는 **읽고 판단해서 자기 표 둘에만 행을 남긴다**
   (`logistics_exceptions` · `logistics_action_proposals`). 그래서 걷기 안에 들어가도
   재현성이 안 깨진다.

🔴 **Commit 5 의 `APPROVED` 는 `EXECUTED` 가 아니다.** *"이 대응안으로 진행해도 좋다"*
   는 사람의 결정일 뿐이고, 승인 뒤에도 재고·판매·매입은 한 줄도 안 바뀐다 — 실제 실행은
   Commit 6 이다. 승인·거절에 LLM 을 부르지 않는다(사람이 이미 정한 것을 되묻지 않는다).

🔴 **Commit 2 에 LLM 이 없다.** 탐지·해소는 전부 결정론이고, 임계 비교는 기존
   `rules.py` · `tools.py` 함수를 **그대로 부른다** — 신선도 공식도 용량 공식도
   여기서 새로 적지 않는다.

🔴 **Commit 4 의 LLM 도 숫자를 만들지 않는다.** 조사에서 AI 가 하는 일은 *"다음에 어떤
   Tool 을 어떤 인자로 부를까"* 와 *"모은 사실을 어떻게 문장으로 이을까"* 둘뿐이다.
   재고·용량·신선도·예약·원가·판정은 전부 `tools.py` 의 8개 Tool 이 낸다.
   ⚠️ 조사는 **걷기에 안 붙는다** — 사람이 부를 때만 돈다.
"""
