# Finance Agent

## Finance LLM 아키텍처

Finance의 Master 연동 경로는 다음과 같다. **밖에서 보이는 계약은 그대로다** —
LangChain 과 Harness 는 재무 내부 구현이다.

```text
Master
  ↓
finance_port
  ↓
FinanceAgentController
  ↓
Finance Harness            ← 지금 무엇이 합법인가를 정한다
  ↓
LangChain Tool-calling Planner (LLM)
  ↓  선택 = 실행 요청
Finance Harness 재검증      ← Registry 직전에 한 번 더
  ↓
FinanceToolRegistry → capability (결정론)
  ↓
Finance Rules (verdict)
  ↓
Finance Finalizer (설명) → 설명 guard
```

### Finance availability fallback

```text
Finance Planner
Gemini gemini-3.5-flash-lite
  ↓ availability failure (HTTP 429/5xx, timeout, network, API key missing)
Ollama llama3.2:3b

Finance Finalizer
Gemini gemini-3.5-flash-lite
  ↓ availability failure
Ollama gemma3:4b
```

로컬 fallback을 쓰려면 다음 모델이 필요하다.

```bash
ollama pull llama3.2:3b
ollama pull gemma3:4b
```

책임은 다음과 같이 갈린다.

```text
LangChain   Tool 계약 표현과 tool calling 전송 계층. 업무 로직을 갖지 않는다.
Harness     capability 상태 · 실행 가능 Tool · 권한 · 의존 · 예산 · 중복을 정하고 강제한다.
LLM         (1) 지금 실행 가능한 Tool 중 하나를 고른다 (2) 확정된 결과를 설명한다
Tool        재무 사실을 계산한다 (금액 공식의 유일한 주인)
Rule        재무 verdict 를 정한다
Finalizer   검증된 Evidence 로 고정 문장을 고른다 — 결과를 바꾸지 못한다
```

★ **LLM 의 Tool 호출은 실행 요청이지 실행 권한이 아니다.** LangChain 에 바인딩되는
Tool 객체와 실제로 실행되는 Tool 객체는 같지만, 그 사이에 Harness 승인이 있다.
그래서 `AgentExecutor` 류의 자동 실행 루프를 쓰지 않는다 — 승인 자리가 사라진다.

### capability 소유와 의존

정본은 `application/harness.py` 하나다. 소유는 **1:1** 이다.

```text
finance_position              → assess_finance_position
cashflow_projection           → project_cashflow
finance_cap                   → calculate_purchase_finance_cap
payment_pressure              → analyze_payment_pressure
scenario_evaluation           → evaluate_purchase_scenario
amount_adjustment_validation  → validate_amount_adjustment
sales_scenario_evaluation     → evaluate_sales_scenario
```

선행 조건은 **Tool 이름 순서가 아니라 capability 조건**이다.

```text
calculate_purchase_finance_cap  requires cashflow_projection
analyze_payment_pressure        requires cashflow_projection
validate_amount_adjustment      requires scenario_evaluation
```

조건만 맞으면 어느 것을 먼저 골라도 된다. 그래서 PRE_PURCHASE 는 고정 파이프라인이
아니다 — 투영이 끝난 뒤에는 위치조사·cap·압박도 셋이 모두 합법이고, 그중 무엇을
고를지는 Planner 몫이다.

🔴 **숨은 선행 호출을 없앴다.** 예전에는 투영이 없으면 `calculate_purchase_finance_cap`
과 `analyze_payment_pressure` 가 안에서 `project_cashflow` 를 몰래 돌렸다. 값은 맞았지만
**이력이 실행을 말하지 않았다** — 현금흐름을 만든 적이 없는 실행으로 남았다. 지금은
선행 capability 를 Harness 가 드러내 놓고 강제하고, 그래도 새어 들어온 호출은
`FinancePreconditionMissing` 으로 실패한다. 이것은 **실행 순서 오류이지
`RUNTIME_NOT_READY` 가 아니다** — 재무 데이터는 멀쩡히 있다.

### 반려 사유

Harness 가 막은 이유는 값으로 남는다 (`finance_harness_trace` observation).

```text
TOOL_NOT_EXECUTABLE            지금 필요하지 않거나 이미 채워진 capability
DEPENDENCY_NOT_SATISFIED       선행 capability 가 없다
TOOL_PERMISSION_DENIED         이 mode 의 Tool 이 아니다
TOOL_BUDGET_EXHAUSTED          Tool 호출 상한
DUPLICATE_UNRESOLVED_TOOL_CALL 같은 요청 반복
```

앞의 셋은 회복 가능해 상한 안에서 되묻고(bounded replan), 뒤의 둘은 되물어도 같아서
실행을 접는다.

### Trace

`ExecutionMetadata.observations` 에 `finance_harness_trace` 가 한 덩어리로 남는다.
단계마다 **LLM 이 요청한 것 · Harness 가 허락한 것 · 실제로 돈 것**을 함께 적는다.

```text
step / branch_id
completed_capabilities · missing_capabilities · executable_tools · dependency_status
requested_tool · finalize_requested · selected_tool · executed_tool
denied_tool · denied_reason
tool_calls · llm_calls · replans
```

DB 테이블을 새로 만들지 않는다 — 기존 실행 metadata 를 넓혔을 뿐이다.

### 상한

```text
FINANCE_MAX_TOOL_CALLS   기본 8
FINANCE_MAX_REPLANS      기본 2
```

한 실행 전체에서 공유한다(분기가 늘어도 예산은 늘지 않는다). `0` 은 기본값으로
덮이지 않는다 — **한 번도 부르지 말라**는 뜻이다.

### 사용자 문장과 기계 계약

나누는 기준은 **누가 읽는가** 다. 정본은 `messages.py` 하나다.

```text
사람이 읽는 것   reasoning · payload.verdicts[].reason · HTTP 404 본문
                 → 한국어 · 업무 언어 · 구현 용어 없음

기계가 읽는 것   READY · RUNTIME_NOT_READY · ERROR
                 ok · conditional · reject · FIN-BASE-STRESS
                 Tool 이름 · capability id · payload 키 · missing_data · Trace
                 → 그대로 둔다. 번역하면 프론트·Critic·마스터가 대상을 못 찾는다.
```

읽는 사람은 Tool 도 Capability 도 Harness 도 Planner 도 모른다. 그래서 사용자 문장에는
그 낱말이 없다 — *"무엇이 어떻게 됐고 왜 그런가"* 와 **다음에 할 일**만 있다.

🔴 **기술적 사유를 회신에 싣지 않는다. 대신 잃지도 않는다.** 예전에는 실패한 실행의
`reasoning` 이 예외 문자열 그대로였다 — 사용자가 `Finance tool call limit exceeded` 를
받았다. 지금 그 문자열은 `finance_harness_trace` 의 `failure_reason` · `failure_kind` 로
가고, 사용자에게는 갈래에 맞는 한국어 문장이 나간다.

```text
failure_kind = INVALID_REQUEST  보내 주신 내용을 고쳐야 한다
failure_kind = NOT_READY        자료가 준비되면 다시 보면 된다   (RUNTIME_NOT_READY)
failure_kind = INTERNAL         우리 쪽 사정이다. 잠시 후 다시   (ERROR)
```

`ok` · `conditional` · `reject` 는 **각각 다른 문장**을 받는다. 사용자가 할 일이
다르기 때문이다 — 그대로 진행 / 조정한 뒤 재검토 / 이 조건으로는 어려움.

★ **LLM 경로와 결정론 대체 경로가 같은 문장을 쓴다.** 한쪽만 다듬으면 모델이 죽은
날에만 말투가 달라진다 — 설명이 가장 필요한 날에 설명이 제일 나빠진다.

★ 숫자 비소유는 그대로다. 설명은 여전히 **고정 문장을 고르는** 구조이고
`_validate_ready_reasoning` 이 숫자를 막는다. 금액은 payload 와 Evidence 가 든다.

## 판매 재무 검증 (SALES_VALIDATION)

### Finance mode

```text
PRE_PURCHASE         매입 경계
SCENARIO_VALIDATION  매입 시나리오 판정
SALES_VALIDATION     판매 시나리오 판정   ← 매입과 다른 책임이다
```

매입 `SCENARIO_VALIDATION` 을 판매에 재사용하지 않는다. 같은 mode 를 나눠 쓰면
`(agent, mode, call_seq)` 로 둘을 구분할 수 없고, 그러면 payload 모양을 보고 무엇인지
**추측하는** Adapter 가 생긴다.

### 흐름

```text
Sales → Master → Finance(SALES_VALIDATION) → Master → Sales Refeed
```

마스터가 순서를 소유한다. 영업과 재무는 서로를 직접 부르지 않는다
(`tests/finance/test_finance_sales_orchestration_boundary.py` 가 고정한다).

### 결정론 책임

```text
매출액 재계산       수량 × 단가 (Decimal, 반올림 없음)
보고 금액 대조      정확한 항등. 허용오차 없음
판매 원가 기준      권위 있는 재고원가 + 아직 포함 안 된 검증된 직접비
공헌이익 · 이익률   매출액 - 원가기준 / 매출액
회수일              기준일 + 결제일수 (기준점의 의미는 호출자 소유)
판매 시나리오 현금흐름  BASE + 제안 회수 유입
채권 사실           거래처 채권 · 연체 잔액 집계
종합 Finance verdict   하위 규칙 결과만으로 결정
```

계산은 `tools.py`, 판정은 `rules.py`, 조립은 `capabilities/sales.py` 가 소유한다.
Finance 내부 전용 모델은 `sales_models.py` 에 산다 — `schemas.py` 는 밖에서 읽는
계약이라 내부 계산 구조를 거기 두지 않는다.

### BASE 와 SCENARIO

```text
BASE      = 확정된 Finance 현금 Event 만
SCENARIO  = BASE + PROPOSED_SALES_COLLECTION
```

제안 회수는 **확정 채권이 아니다.** BASE 로 승격되지 않고 실제 AR 로 적재되지도
않는다. BASE 가 최소 현금을 밑돌면 SCENARIO 가 안전해도 `FAIL` 이다 — 아직 안 들어온
돈이 이미 난 구멍을 가리지 못한다.

`depends_on_projected_inflow` 는 **사실이지 판정이 아니다.** SCENARIO 최저 현금이 그
유입 덕분에 올라갔다는 것은 reason code 로 남고, 종합 판정은 권위 있는 규칙(최소 현금
정책)만 움직인다. 판정을 낮출 근거가 저장소에 없기 때문이다 — 설계서 v2.2.2 §9 의
BASE/STRESS 규칙은 *매입 STRESS 오버레이가 기준을 밑돌 때* conditional 이라는 규칙이지
*유입에 기대는가* 를 다루지 않는다. 판매 현금흐름 판정 기준이 정해지면 그때 근거와
함께 넣는다.

horizon 밖 회수일은 **날짜를 옮기지 않는다.** 동적 horizon 연장 규칙이 계약에 없어서
연장을 지어내지 않고 `collection_within_horizon=False` 로 드러낸다.

### 판정 어휘

```text
Finance 도메인    PASS · REVIEW_REQUIRED · FAIL
공통 봉투         ok · conditional · reject · skipped
```

매핑은 **Finance Adapter 가 소유한다** (`SALES_VERDICT_TO_BUSINESS_STATUS`).
마스터가 재무 판정을 다시 해석하지 않는다. 옮긴 뒤에도 원본은
`payload.finance_verdict` 에 남는다 — `conditional` 만 남으면 마진 경고인지 현금
의존인지 되돌릴 수 없다.

### 못 한 일을 성공처럼 내지 않는다

```text
INPUT_INCOMPLETE   제안에 사실이 빠졌다 (영업/마스터 쪽). runtime_status 는 READY
RUNTIME_NOT_READY  Finance 정책/데이터가 없다 (재무 쪽)
ERROR              저장소 실행 실패
```

셋 다 `business_status = skipped` 이고 `finance_verdict = None` 이다.
**없는 정책은 FAIL 이 아니다.** 없는 값을 0 으로 바꾸지도 않는다.

### LLM 책임

```text
한다      Tool 선택 · 확정된 결과 설명
안 한다   업무 숫자 생성 · verdict 결정 · 없는 정책 채우기
```

판매 Tool 은 **인자를 받지 않는다**(`_NoArguments`, `extra="forbid"`). 수량·단가·
원가·결제일수·여신은 전부 request payload 와 Finance 정책이 소유한다 — Planner 가
숫자를 실을 자리 자체가 없다.

### SuggestedAdjustment

```text
Finance 조정 축   amount 하나뿐
```

`payment_terms` · `price` · `delivery` · `quantity` · `channel_mix` 축을 만들지 않는다.
재무가 내는 결제일수 상한은 **payload 필드**다.

```text
payload.max_finance_allowed_payment_terms_days   상한(경계)이지 조정이 아니다
payload.max_finance_allowed_amount_krw
```

### 실행 경로 상태

재무 쪽은 열려 있다.

```text
finance_port(mode=SALES_VALIDATION)
  → _controller_sales_validation
  → FinanceAgentController
  → Harness (SALES_VALIDATION_TOOLS)
  → evaluate_sales_scenario
  → AgentReply + ExecutionMetadata
  → finance_agent_runs_v22 저장
```

저장 제약도 함께 열었다 — 신규 DDL 과 기존 DB 마이그레이션
(`database/finance_agent_runs_v22_sales_validation.sql`) 둘 다.
**순서가 중요하다.** 제약보다 Controller 를 먼저 열면 판정은 되는데 저장이 전부
실패한다.

아직 막힌 것은 재무 밖이다.

```text
Master capability 라우팅  FINANCIAL_VALIDATION → (finance, SALES_VALIDATION)
                        → 마스터에 capability 어휘 자체가 없다
Sales AgentName         AgentName 에 sales 가 없다 (부를 대상이 아니다)
Feedback Envelope       최종 필드명 미확정 (팀 결정)
```

공통 `Mode` 에는 `SALES_VALIDATION` 어휘만 넣었다 — 그게 없으면 유효한 재무
`AgentRequest` 자체를 만들 수 없다. 어휘와 라우팅은 다른 일이다.

권위 있는 값이 없어 **오늘은 항상 닫히는** 정책들:

```text
finance_minimum_margin_rate
finance_warning_margin_rate
max_finance_allowed_payment_terms_days
partner_credit_limit_krw
sales_collection_risk_policy
sales_installment_payment_policy
```

저장소 어디에도 이 값들이 없다 — `FinancePolicy` 의 닫힌 키에도,
`agent_policy_config` 의 finance domain 에도, 어떤 테이블에도 없다. 그래서 판정은
`RUNTIME_NOT_READY` 로 닫히고 없는 정책 이름이 `missing_data` 에 실린다.
**Purchase 의 `margin_defense_floor_rate` 를 판매 마진 임계값으로 쓰지 않는다.**

원가 기준 쪽도 아직 저장소 조회로 잇지 않았다.

```text
proposed sale 의 정본 재고원가   어느 Lot 을 쓸지는 Inventory 의 배분 결정이다
직접 물류비                      deliveries 는 실제 실행 데이터라 제안 시점에 없다
```

그래서 `sales_cost_basis` 는 **주입받는다**. 권위 있는 재고원가가 없으면 마진을
계산하지 않고, 0 으로 대체하지 않는다. 조건부 물량이 섞이면 확정 재고원가를 제안
전체의 원가처럼 쓰지 않는다.

## 승인 → 다음 재무 Actual State

승인된 매입 약정이 재무의 다음 상태가 되는 경로다. **값은 재무가, 트랜잭션은
마스터가** 소유한다.

```text
load_finance_state_row(as_of)                 as_of 시점에 유효한 상태 한 건
build_finance_transition(commitment, ...)     계산만 — DB 를 바꾸지 않는다
persist_finance_transition(conn, transition)  받은 연결로 쓰기만 — commit 하지 않는다
```

마스터 전이 Protocol(#256)이 부르는 모양이다. `FinanceTransitionAdapter` 가 그 입구다.

```python
finance = FinanceTransitionAdapter()
plan = finance.build(
    commitment,
    target_state_date=next_day,   # as_of + 1 달력일 — 마스터가 준다
    purchase_ids=purchase_ids,    # {seq: purchase_id} — 마스터가 만든다
)
finance.persist(conn, plan)       # 받은 연결로만, commit 은 마스터가 한 번만
```

★ **`purchase_ids` 는 회차별이다.** `purchases.purchase_date` 가 header 에 하나뿐이라
회차마다 `purchases` 한 행이 선다. 재무는 자기 회차의 값을 **`seq` 로 찾아 쓰기만**
한다 — 하나뿐이라고 첫 값을 집거나 정렬해서 고르지 않는다. 없거나 빈 값이면
`commitment_purchase_ids` 로 세운다.

★ 어댑터 등록은 최신 런타임의 `app.main`에서 Master가 소유한다. Finance는 구조적
Protocol 구현만 제공하고 등록 순서나 트랜잭션 경계를 가져오지 않는다.

### 회차별 정본 금액으로 N개 채무를 만든다

```text
회차 1건   → 채무 1건 (금액 누락 시 축이 하나이므로 약정 총액과 기존 의미 유지)
회차 N건   → 각 ArrivalLeg.amount_krw · purchase_ids[seq] 로 채무 N건
금액 일부/전부 누락 또는 합계 불일치 → commitment_payment_amounts
회차 0건   → commitment_arrival_schedule
```

Master가 Purchase `split_plan[].amount_krw`를 `ArrivalLeg.amount_krw`로 운반한다.
Finance는 균등·수량비례 배분을 하지 않고 정본 금액을 그대로 쓴다. 매입일이 같아도
회차가 둘이면 매입 의무도 둘이며 Payable ID는 `AP-{approval_id}-S{seq}`다.

### 실행일과 장부일은 다른 축이다

```text
매입 판단   평일만 돈다              ← 마스터 실행일 달력
장부 상태   매 달력일 전진한다        ← 토·일·공휴일 포함
```

주말에도 판매와 원장 활동이 일어나므로 재무 상태는 주말에도 서야 한다. 그래서
다음 상태 날짜는 **`as_of + 1 달력일`** 이고 금요일 승인은 **토요일 상태**를
만든다 — 그것이 정상이다. 예전에 이 자리를 *"다음 실행일 월요일이 못 읽는다"* 로
적었는데 그건 두 축을 겹쳐 본 것이다.

★ 그 날짜를 재무가 세지 않는다. `target_state_date` 를 **인자로 받고**, 보는 것은
정합성 한 가지뿐이다 — 승인일보다 뒤여야 한다. 같은 날에 상태가 둘 서면 그날의
사실을 말할 수 없다.

🔴 `master.execution_day.next_execution_day` 를 쓰지 않는다. 재무는 그 모듈을
   import 하지 않고 평일 계산도 하지 않는다.

### 두 층을 나눈다 — DB "지금" 과 요청 "그때"

```text
v_current_finance_state   축 위에서 가장 늦은 상태          "지금"
as-of 질의                state_date <= as_of 중 가장 늦은 행  "그때"
신선도 게이트             고른 행의 state_date == as_of 여야 한다
```

PostgreSQL VIEW 는 인자를 받지 않는다. `v_current_finance_state(as_of)` 같은 것은
없고, 요청 `as_of` 는 View 가 아니라 **질의**가 건다.

**공유 기본 스키마가 만드는 View 는 `finance_state_id = 'FIN-DAY30-LOAN'` 을 박아
둔다.** 그래서 승인 전이가 다음 상태를 넣어도 DB 는 계속 T0 만 돌려줬다.
`database/finance/finance_current_state_view.sql` 이 그 고정을 걷어낸다 — 기본 스키마
파일은 건드리지 않고, 그 뒤에 `CREATE OR REPLACE VIEW` 로 덮는다.

```text
FROM finance_states fs
JOIN sim_runs sr ON sr.sim_run_id = fs.sim_run_id
               AND sr.financing_mode = fs.financing_mode
WHERE fs.state_date = (그 축의 max(state_date))
```

★ **축은 `sim_runs` 가 준다.** 리터럴을 다른 리터럴로 바꾸지 않는다. 같은 날짜에
`BASE_NO_LOAN` 과 `LOAN_BASELINE` 두 행이 실제로 있는데, 어느 쪽이 이 실행의
상태인지는 `sim_runs.financing_mode` 가 정한다.

🔴 **`sim_runs.as_of` 로 고르지 않는다.** 시드된 뒤 아무도 전진시키지 않아서
   (`status = SEEDED`), 그것으로 고르면 상태 ID 대신 날짜가 박힐 뿐이다.

🔴 **동률을 View 가 줄이지 않는다.** 한 축에서 같은 날짜에 상태가 둘이면 View 는 두
   행을 그대로 보여 주고, 고르기를 거부하는 판단은 런타임이 한다
   (`finance_state_ambiguous`). View 가 대신 고르면 못 믿을 상태가 정상 응답이 된다.

### 승인은 현금을 줄이지 않는다

승인 시점에 생기는 것은 **매입채무**다. 현금은 실제 지급일에 나간다.

```text
승인일      payables OPEN 생성 (due_date = 매입일 + purchase_payment_days)
            finance_states.unsettled_purchase_payables_krw 증가
            current_cash_krw 그대로
지급일      현금흐름 투영이 그 채무를 유출로 본다
```

`financial_limit_krw` 는 생성 컬럼이라 채무가 늘면 자동으로 줄어든다.

### 네 날짜를 겹치지 않는다

```text
승인일          approval as_of          상태를 딛는 날
매입일          purchase_date           매입이 실제로 일어나는 날
계약 만기일     purchase_date + N5      N5 = 0 (달력일) → 매입 당일
실제 지급일     계약 만기일이 토·일이면 다음 월요일
```

★ **N5 는 달력일수다.** 영업일수도, 실지급일 오프셋도 아니다. 현재 값은 0 —
원장이 그렇게 말한다 (`purchases` 16/16 · `payables` 16/16 이 매입일 = 만기일).

🔴 **당일 지급은 오류가 아니라 정책이다.** `calculate_finance_cap` 은 예전에
   `as_of < payment_date` 를 요구해서 N5=0 을 **유효한 값이 아니라 예외**로 처리했다.
   지금은 `as_of <= payment_date` 다.

★ **계약일과 현금일을 분리한다** (`tools.effective_cash_date`). 원장 `due_date` 는
토·일 그대로 남고 — 실제 원장에 주말 만기가 4건 있다 — 현금 사건만 다음 월요일로
민다. 둘을 합치면 계약 사실이 사라진다. 공휴일은 다루지 않는다.

🔴 **주말 만기 채무가 월요일에 사라지지 않는다.** 예전 매입채무 조회는 하한이
   `due_date > as_of` 였다. 일요일 만기 채무를 월요일에 읽으면 `due_date < as_of` 라
   미래 현금흐름에서 통째로 빠졌다. 지금은 `OPEN` 이면 하한 없이 읽고, 현금 사건만
   `max(effective_cash_date(due_date), as_of)` 로 세운다 — 연체된 미결제 채무도
   같은 이유로 버리지 않는다. 지나간 만기를 임의로 `PAID` 로 바꾸지 않는다.

★ **주말 실행 판단은 재무가 하지 않는다.** 시뮬레이션이 평일만 도는 것은 마스터
소유이고, 경과 시간은 달력일 그대로다.

### 같은 날 승인 여러 건은 상태 하나에 누적되고 retry는 다시 더하지 않는다

Payable은 회차별 의무이고 Finance State는 날짜별 snapshot이다. 두 축을 섞지 않는다.

```text
payables        UNIQUE (purchase_id)                         승인·회차별 의무
finance_states  UNIQUE (sim_run_id, financing_mode, state_date)  일별 snapshot
```

상태 ID는 `FIN-DAY-{sim_run_id}-{financing_mode}-{YYYYMMDD}`로 transition과
`open_day`가 같이 쓴다. persist는 **이번 호출에서 실제 새로 INSERT된 Payable 금액만**
일별 상태에 원자적으로 더한다. 같은 승인 retry는 Payable INSERT가 0건이므로 상태도
다시 증가하지 않는다. 같은 날짜 승인 순서가 바뀌어도 최종 업무 숫자는 같다.

### 미지급 매입채무 취소는 지급이나 상각이 아니다

`FinanceCancellationAdapter.cancel(conn, *, purchase_ids, as_of, target_state_date)`는
향후 Master 취소 전이가 부를 Finance-owned 표면이다. Master 취소 Protocol은 아직
없으므로 이 브랜치에서 등록하거나 다른 파트의 원장을 건드리지 않는다.

```text
OPEN + paid=0   → CANCELLED
original        → 그대로
paid            → 그대로 0
cancelled       → 취소 직전 outstanding 전액
outstanding     → 0
settled_date    → 그대로
```

`WRITEOFF`는 존재하던 채무의 상각이고, `CANCELLED`는 승인·매입 원인이 철회되어
지급 전 의무가 소멸한 사실이다. 취소를 DELETE·가짜 지급·음수 승인으로 만들지 않는다.

호출은 대상 `purchase_id` 전체를 먼저 `FOR UPDATE`로 잠그고 검증한다. `PARTIAL`·
`SETTLED`·`WRITEOFF` 또는 없는 ID가 하나라도 섞이면 일부 성공 없이 실패한다. 실제
`OPEN → CANCELLED`로 바뀐 행의 `RETURNING cancelled_amount_krw` 합계만 target daily
state의 `unsettled_purchase_payables_krw`에서 뺀다. retry의 이미 `CANCELLED`인 행은
변경 0건이므로 다시 차감하지 않는다. target state가 없을 때만 exact `as_of` state를
carry하며, 날짜는 Master가 준 값을 그대로 쓴다. 안정적인 취소 사건 ID가 없는 현재
계약에서는 `OPEN`과 `CANCELLED`가 섞인 대상 집합을 합법적 부분 retry로 증명할 수
없으므로 fail-closed한다.

## 패키지 구조

책임이 어디 사는지가 파일 위치로 보이게 정리했다. 전체 디렉터리 이동보다 **책임 분리와
기존 import 호환**을 우선했으므로, 밖에서 쓰는 모듈은 원래 자리를 지킨다.

```text
app/finance/
├─ adapter.py         Master 경계 번역 (finance_port)      ← main.py 가 import
├─ router.py          HTTP 진입점 · 실행이력/판매 조회      ← main.py 가 import
├─ db.py              영속 계층: 연결·조회 헬퍼 · 데이터 경계 계약 · as-of DataPort 구현
│                                                          ← master · orchestrator 도 import
├─ schemas.py         요청·응답 계약 전체 (어휘·현금흐름·정책·상태·매입·판매·이력)
├─ state.py           한 실행 동안 살아 있는 값
├─ state_identity.py  한 Finance 축·날짜의 결정론 state ID
├─ day_open.py        Master DayOpening 구조 계약의 Finance 구현
├─ transition.py      승인 약정 → 다음 재무 상태 · 재무 원장 쓰기 (연결은 부르는 쪽 것)
├─ cancellation.py    미지급 Payable 취소 · 일별 상태 역분개 (Master 배선 대기)
├─ tools.py           결정론 재무 계산 (공식의 유일한 주인)
├─ rules.py           결정론 판정 (verdict 소유)
├─ execution.py       Evidence · DeptMeta(Critic 사이드카) · 실행이력 저장/조회
├─ messages.py        사용자에게 보이는 한국어 문장 (정본)  ← 기계 계약은 담지 않는다
├─ interpretation.py  공개 표면 (legacy/* 재수출)          ← 재무 밖 테스트가 import
├─ application/       Agent 실행 계층
│  ├─ harness.py          합법 행동공간: capability 정책 · Tool 선언/디스패치 ·
│  │                      승인 · 예산 · 중복 차단 · 실행 계약 guard · Trace
│  └─ orchestration.py    수명주기: 분기 · Planner 루프 · 결과 확정 · 설명 · 회신 · 이력
├─ capabilities/      결정론 업무 (Harness 가 부르고, 여기서 계산한다)
│  ├─ procurement.py      컨텍스트 적재 · 위치 조사 · 투영 · Finance Cap · 지급 압박도
│  └─ scenario.py         지급 일정 재구성 · BASE/STRESS overlay · 판정 · 금액 대안 검증
├─ llm/               Provider 통합 (업무 로직 없음)
│  ├─ client.py           설정 · Gemini/Ollama HTTP · 가용성 실패 판별 ·
│  │                      Gemini 전송 형식 낮추기(const → enum, null union → nullable)
│  ├─ planner.py          Planner 계약 · 프롬프트 · 사후 검증 · ChatModel ·
│  │                      LangChain tool-calling Planner · 결정론 Planner · 가용성 대체
│  ├─ finalizer.py        검증된 Evidence 에서 설명 키 선택
│  ├─ runtime.py          레거시 해석 계층 (`/finance/sales` 전용) ← 재무 밖 테스트가 import
│  └─ schemas.py          레거시 해석 계약                      ← 재무 밖 테스트가 import
└─ legacy/            Agent 이전의 결정론 경로 (입구는 `/finance/sales` 하나)
   ├─ deterministic_service.py  Finance A/B 실행
   ├─ scenario_engine.py        결정론 Scenario 실행
   └─ interpretation.py         응답 해설 보강
```

★ **한 응집 영역 = 한 모듈**이다. 늘 같이 열리는 것들을 한 파일에 둔다 — *"재무
Agent 는 어떻게 실행되는가"* 는 `application/orchestration.py`, *"이 호출이 합법인가"* 는
`application/harness.py` 하나면 된다. 파일 경계와 신뢰 경계는 다른 것이고, 후자는
클래스·절·이름으로 지킨다.

책임 경계는 다음과 같다.

```text
harness.py 무엇을 부를 수 있는지 정하고 강제한다 (숫자를 만들지 않는다)
Planner    그중 무엇을 부를지 고른다             (숫자를 만들지 않는다)
capability 허용된 재무 작업을 수행한다
tools.py   금액·현금흐름을 계산한다              (공식의 유일한 주인)
rules.py   PASS/FAIL·verdict 를 정한다
Finalizer  검증된 Evidence 를 설명한다           (고정 문장을 고를 뿐이다)
```

`tool_registry.py` 는 이름을 capability 로 넘기는 일만 한다. 예전에는 이 파일 하나가
디스패치·컨텍스트 적재·두 mode 업무·지급 일정 재구성·Evidence 조립을 모두 들고 있었다.

## 지원 Provider

- Ollama / Gemma
- Gemini API

Finance의 Primary Provider는 `gemini / gemini-3.5-flash-lite`다. Gemini를 사용할 수 없는
일부 가용성 장애에서는 `ollama / gemma3:4b`를 availability fallback으로 사용한다. 다른
Agent가 사용하는 전역 LLM 기본값은 변경하지 않는다.

## 환경 설정

Finance만 Gemini를 사용하려면 다음과 같이 설정한다.

```env
FINANCE_LLM_PROVIDER=gemini
FINANCE_LLM_MODEL=gemini-3.5-flash-lite
FINANCE_GEMINI_API_KEY=<secret>
```

Provider는 FINANCE_LLM_PROVIDER가 있으면 해당 값을 사용하고,
없으면 Finance 기본 Provider인 gemini를 사용한다.
전역 LLM_PROVIDER는 Finance Provider 선택에 상속하지 않는다.

키는 `FINANCE_GEMINI_API_KEY`를 먼저 읽고, 비어 있으면 `GEMINI_API_KEY`를 사용한다.
Finance는 `MASTER_GEMINI_API_KEY`를 읽지 않는다. Finance에서만 Gemini를 활성화하기 위해
전역 `LLM_PROVIDER`를 변경해서는 안 된다.

환경 파일은 절대경로로 `backend/.env`를 먼저 읽고 project-root `.env`를 선택적으로 읽는다.
`override=False`이므로 이미 설정된 process 환경변수가 `.env`보다 우선한다. API 키는 설정
객체, 로그 또는 Repository에 저장하지 않으며 실제 키를 Git에 커밋해서는 안 된다.

### Provider별 기본 모델과 상속 규칙

- Primary Gemini 모델: `gemini-3.5-flash-lite`
- Availability fallback Ollama 모델: `gemma3:4b`
- 명시적인 `FINANCE_LLM_MODEL`이 항상 우선한다.

모델은 Provider에 종속된다. 다음 설정처럼 전역 Provider와 Finance Provider가 다를 때
Finance는 전역 Ollama 모델을 상속하지 않고 Gemini 기본 모델을 사용한다.

```env
LLM_PROVIDER=ollama
LLM_MODEL=gemma3:4b
FINANCE_LLM_PROVIDER=gemini
```

따라서 위 설정의 Finance 모델은 `gemini-3.5-flash-lite`이며, Gemini API에
`gemma3:4b`를 요청해 404가 발생하는 구성을 방지한다.

### Availability fallback

Gemini에서 다음 가용성 오류가 발생하면 같은 Finance 실행의 남은 Planner와 Finalizer는
Ollama/Gemma를 사용한다.

- Gemini API 키 누락
- HTTP 429
- timeout
- network 또는 `URLError`
- HTTP 5xx

다음 오류는 구성이나 계약 결함일 수 있으므로 Ollama로 조용히 우회하지 않는다.

- HTTP 400
- HTTP 401/403
- HTTP 404
- schema 또는 contract 오류
- invalid JSON 및 structured output 오류

Gemini 가용성 오류 후 Gemma가 성공하면 Finance 계산 결과는 그대로 유지되고
`runtime_status=READY`, `llm_status=SUCCESS`가 된다. 이때 실제 사용 모델은
`llm_model=gemma3:4b`이며 `llm_fallback_used=False`다. Provider 전환과 deterministic
finalization fallback을 같은 상태로 취급하지 않는다.

Provider 전환 이력은 Finance가 `ExecutionMetadata.observations`에 추가하는
`finance_llm_provider` observation으로 구분한다.

```json
{
  "observation_type": "finance_llm_provider",
  "primary_provider": "gemini",
  "effective_provider": "ollama",
  "provider_fallback_used": true,
  "provider_fallback_reason": "HTTP_429"
}
```

`provider_fallback_reason`은 `API_KEY_MISSING`, `HTTP_429`, `TIMEOUT`, `NETWORK_ERROR`,
`HTTP_5XX` 중 하나이며 fallback이 없으면 `null`이다. 따라서 명시적으로 Ollama를 선택한
실행은 `primary_provider=ollama`, `effective_provider=ollama`,
`provider_fallback_used=false`로 자동 fallback과 구분된다. 공유 `ExecutionMetadata`
계약에는 Finance 전용 필드를 추가하지 않는다.

## Gemini 응답 처리

Gemini Planner와 Finalizer는 `/v1beta/models/...:generateContent` 및 structured JSON
`responseSchema`를 사용한다. 응답은 첫 번째 part만 읽지 않고 사용할 수 있는 text를
검색한다.

- `thought=true`인 part만 건너뛴다.
- `thoughtSignature`만 존재하는 part는 thought block으로 취급하지 않는다.
- 사용할 수 있는 text가 없으면 명시적으로 실패한다.
- boolean과 Tool 선택 사이의 행동 제약은 JSON 파싱 후 검증한다.
- `HTTPError`를 감싸지 않아 400/401/403/404/429/5xx 상태 코드를 보존한다.

Planner가 반환한 Tool은 현재 mode의 허용 목록과 대조한다. 필요한 capability가 남아 있으면
Planner는 하나의 허용 Tool을 선택해야 하고, 모두 충족되면 Tool 없이 finalize해야 한다.

## LLM 메타데이터 의미

- `DISABLED`: LLM이 비활성화되어 호출되지 않음
- `SUCCESS`: LLM이 호출되고 유효한 결과를 반환함
- `FALLBACK`: LLM이 호출됐지만 실패했거나 사용할 수 없는 결과를 반환함

`runtime_status`와 `llm_status`는 서로 다른 축이다. Planner가 실제로 호출된 뒤 실패하면
성공 실행으로 바꾸지 않는다.

```text
Planner invoked and failed
→ runtime_status=ERROR
→ llm_status=FALLBACK
→ llm_fallback_used=True
```

Finalizer 실패는 기존 deterministic fallback을 사용할 수 있으며, 이때도 실제 Controller의
`used_tools`, `tool_order`, `replans`, `llm_attempts`, `llm_fallback_used`, `elapsed_ms`를
보존한다.

Availability fallback으로 Ollama가 정상 결과를 반환한 경우는 LLM 실행 자체가 성공한
것이므로 `SUCCESS`다. 반면 deterministic fallback은 LLM 결과를 사용하지 못한 상태이므로
`FALLBACK`이며 `llm_fallback_used=True`다.

## Gemma vs Gemini — initial smoke benchmark

2026-08-31에 동일한 deterministic Finance 입력으로 실제 smoke test를 수행했다.

| Case | Gemini 3.5 Flash-Lite | Gemma3:4b |
| --- | ---: | ---: |
| PRE_PURCHASE | 6,905 ms | 27,827 ms |
| Accepted scenario | 3,625 ms | 4,359 ms |
| Rejected scenario | 5,014 ms | 9,015 ms |
| Total | 15,544 ms | 41,201 ms |

Gemini 측 관측 결과는 다음과 같다.

```text
Gemini real API requests: 12
Gemini Planner failures: 0
Gemini Finalizer failures: 0
Gemini structured-output failures: 0
Gemini FALLBACK: 0
Gemini ERROR: 0
Replans: 0
```

Hard invariant도 모두 유지됐다.

- Finance numerical output이 Gemma baseline과 일치했다.
- Evidence가 일치했다.
- verdict/business semantics가 일치했다.
- 선택된 Tools와 순서가 일치했다.
- LLM이 재무 수치를 생성하지 않았다.
- Finance contract validation이 통과했다.

Gemini의 잠정 분류는 `STRONG_CANDIDATE`다. Finance 정책상 Gemini를 Primary로 사용하지만,
이는 실제 Gemini Finance 실행 세 건만을 대상으로 한 초기 smoke benchmark다. benchmark
하나만으로 모든 Production 상황에서의 최종 우승 모델을 단정할 수는 없다. quota 초기화
이후 더 다양한 입력과 반복 안정성을 별도로 평가해야 한다.

## Gemini quota와 자동 테스트

현재 사용한 Gemini free-tier 환경은 하루 약 20회의 요청으로 제한된다. Finance Agent 한 번의
실행에서도 Planner의 반복 호출과 별도 Finalizer 호출 때문에 여러 API 요청을 사용할 수 있다.

```text
PRE_PURCHASE → 5
accepted validation → 3
rejected validation → 4
```

따라서 실제 Gemini API 회귀 테스트를 일반 pytest에 포함하지 않는다. 정상 자동 테스트는
`urllib.request.urlopen`을 mock하여 네트워크와 실제 API 키를 사용하지 않아야 한다.

## 알려진 별도 Evidence 이슈

다음 오류는 PurchaseProposal 호환 경로에서 이전에 관측됐다.

```text
E-EVIDENCE-MISSING
payment_schedule[0].amount_krw
```

이 오류는 최소 Controller smoke 사례에서는 재현되지 않았고 현재 Gemini에 기인한 것으로
판단하지 않는다. Gemini Provider 작업과 분리하여 별도로 조사해야 한다.
