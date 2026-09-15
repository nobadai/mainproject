-- 2026-01-05 상태전이 증명 T0 — **재무 소유 SIM_FIXED fixture.**
--
-- WHY
--   `target_state_date = commitment.as_of + 1 달력일` 이라, 전이 배선을 증명하려면
--   승인일(2026-01-05)에 재무 T0 가 서 있어야 한다. 번인은 2025-12-31 에서 끝나고
--   `finance_states` 에 2026년 행이 없어서, 증명이 딛고 설 자리가 없다.
--
-- WHAT
--   그 축의 2026-01-05 행 **한 건**만 넣는다.
--
-- ORDER
--   `database/10_domain_schema.sql` 다음. 축 선택은
--   `finance_current_state_view.sql` 이 이미 고쳐 둔 View 를 따른다.
--
--
-- 🔴 **이 행은 2026-01-05 의 실제 영업 실적이 아니다.** 팀이 고정한 시뮬레이션
--    값(SIM_FIXED)이고, 하는 일은 전이 배선을 증명하는 것 하나뿐이다.
--
-- 🔴 **`open_day` 가 아니고, 일별 carry-forward 정책을 세우는 것도 아니다.**
--    아래 상속은 이 fixture 한 건에 한정된 의도적 상속이다. 매일 상태를 이어
--    가는 규칙은 별도 계약이고 여기서 만들지 않는다.
--
-- 🔴 **만기가 지났다고 채권을 자동 회수하지 않는다.** AR-001(2026-01-02) 과
--    AR-002(2026-01-04) 는 이 증명에서 **미회수(OPEN)** 로 둔다. 회수/정산 사건이
--    이 증명에 없기 때문이다. 그래서 현금과 채권 잔액이 12-31 과 같다 — 아무 일도
--    없었다고 단정한 것이 아니라, **회수 사건을 만들지 않았다**는 사실의 결과다.
--
-- ★ 상속 컬럼은 같은 축의 `FIN-DAY30-LOAN`(2025-12-31) 값 그대로다.
-- ★ `financial_limit_krw` 는 GENERATED ALWAYS 라 넣지 않는다 — PostgreSQL 이 만든다.
-- ★ 2026-01-06 행은 **여기서 만들지 않는다.** 그 행은 전이가 만들어야 증명이 성립한다.
--
-- 재실행 안전: `finance_states_pkey (finance_state_id)`.

INSERT INTO haetdeul.finance_states (
    finance_state_id,
    sim_run_id,
    state_date,
    state_type,
    financing_mode,
    current_cash_krw,
    minimum_operating_cash_krw,
    committed_outflows_krw,
    unsettled_purchase_payables_krw,
    receivables_krw,
    inventory_book_value_krw,
    operational_inventory_value_krw,
    current_debt_krw,
    recommended_loan_amount_krw,
    note
) VALUES (
    'FIN-PROOF-20260105-LOAN',
    'SIM-BURNIN-202512',
    DATE '2026-01-05',
    'TRANSITION_PROOF_T0',
    'LOAN_BASELINE',
    31993913.770000,
    15902640.000000,
    0.000000,
    0.000000,
    73051531.250000,
    801368.533300,
    674097.333333,
    45272104.184486,
    45272104.184486,
    '2026-01-05 -> 2026-01-06 상태전이 증명용 T0 fixture. SIM_FIXED. 실제 2026-01-05 영업 실적이 아니고, 일별 carry-forward 정책도 아니다. AR-001(01-02)/AR-002(01-04) 는 미회수(OPEN) 로 둔다.'
)
ON CONFLICT (finance_state_id) DO NOTHING;
