"""Agent 이전의 결정론 Finance 실행 경로 — **Sales 와 Procurement 둘 다**.

이 패키지가 뜻하는 것은 *"레거시 Sales"* 가 아니라 **"폐기된 pre-Agent 결정론 경로
전부"** 다. 두 갈래가 한 곳에 있다.

- **레거시 Sales 경로** — 운영에서 아직 닿는다. 입구는 `POST /finance/sales` 하나다
  (`run_finance_sales` → `run_finance_sales_scenario`).

- **레거시 Procurement 결정론 경로** — 운영 호출자가 없다. 매입은
  `app.finance.adapter` → `FinanceAgentController` 를 탄다. `run_finance_procurement*`
  와 `run_finance_procurement_scenario` 는 호환/테스트를 위해 남겨 둔 것이다.

두 갈래를 디렉터리로 가르지 않은 이유는 **같은 비공개 헬퍼를 공유하기 때문**이다
(`_get_current_finance_runtime_context_or_none` · `_base_events` · `_base_projection`).
지금 가르면 헬퍼를 복제하거나 세 번째 공용 모듈을 만들어야 한다.

**새 재무 기능을 여기에 붙이지 않는다** — 이 층은 Sales 가 Agent 로 옮겨질 때 통째로
사라진다.
"""
