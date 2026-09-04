-- 재무 정책 Seed — `agent_policy_config` 의 domain = 'finance' 행.
--
-- ★ 이 파일은 **행만** 넣는다. `agent_policy_config` 테이블 정의는 공유 스키마
--   (`database/10_domain_schema.sql`) 가 소유한다 — 여기서 다시 만들지 않는다.
--
-- ★ 값은 **승인된 재무 정책**이다. 없는 값을 채우지 않는다. 아래 N5 한 건을 빼면
--   2026-09-04 운영 DB 행과 같다.
--
-- ★ `evidence_grade` 는 공유 CHECK 가 정한 어휘만 쓴다. 팀이 고정한 시뮬레이션
--   값은 `SIM_FIXED` 다 — 재무 전용 등급을 새로 만들지 않는다.
--
-- 재실행 안전: `uq_agent_policy_version (policy_version, domain, policy_key)`.
--
-- 🔴 **`purchase_payment_days` 는 0 이다** (D+0 · 매입 당일 지급).
--
--    2026-09-04 관측 시점의 운영 DB 행은 `7` 을 들고 있다. 세 갈래 근거가 전부
--    0 을 가리킨다.
--
--    ```text
--    Persona   company_personas PERSONA-V1.3 (active) . purchase_payment_days = 0
--              note "D+0 지급 / D+30 회수"
--    근거      persona_evidences.purchase_payment_days → EV-SRC-FIN-PERSONA
--              (evidences.source_ref = 'SRC-FIN-PERSONA', PROJECT_SOURCE)
--    원장      purchases  16/16  payment_due_date = purchase_date
--              payables   16/16  due_date        = purchase_date
--    ```
--
--    반면 운영 행의 `7` 이 달고 있던 `FINANCE-DECISION-20260827:N5` 는 `evidences`
--    에 **행이 없다.** 저장소 SQL 어디에도 그 값을 넣는 구문이 없고, 기존 Finance
--    테스트가 문자열로만 들고 있다. 즉 저장소 밖에서 손으로 바꾼 값이고 근거를
--    따라갈 수 없다. 그래서 이 파일은 `SRC-FIN-PERSONA` 를 단다 — 실제로 0 을
--    받치는 근거이고, `monthly_labor_cost_krw` 가 이미 쓰는 것과 같은 출처다.
--
--    이 브랜치는 운영 DB 를 읽기만 했다. 운영 행 반영은 별도 절차다.
--
-- ★ **`persona_version` 은 출처 Persona 행을 가리키지 않는다.** 그래서 이 행이
--    `SRC-FIN-PERSONA`(→ PERSONA-V1.3)를 달고도 `v1.5` 로 남는 것이 모순이 아니다.
--
--    ```text
--    source_ref       이 값을 받치는 출처        → SRC-FIN-PERSONA (PERSONA-V1.3)
--    persona_version  통합 Persona 계약 세대      → v1.5 (재무·물류 공통 라벨)
--    ```
--
--    근거: `company_personas.persona_version` 은 UNIQUE 이고 지금껏 `v1.3` 한 행뿐이라
--    `v1.5` 라는 Persona 행은 존재한 적이 없다. `agent_policy_config` 에서 `v1.5` 는
--    재무 21행뿐 아니라 **물류 8행**도 같이 쓰고, 두 표 사이에 FK 도 컬럼 COMMENT 도
--    없으며 읽는 코드도 없다. 기존 `monthly_labor_cost_krw` 주석이 이 값을
--    *"통합 Persona v1.5"* 라고 부른다 — DB Persona 행이 아니라 문서 세대다.
--
--    여기서만 `v1.3` 으로 바꾸면 표 전체에서 이 한 행만 달라지고, 이 컬럼이 Persona
--    행을 가리키는 것처럼 읽히게 된다. 바꾸지 않는다.

INSERT INTO haetdeul.agent_policy_config (
    domain,
    policy_key,
    value_kind,
    value_numeric,
    value_text,
    unit,
    evidence_grade,
    source_ref,
    approved_by,
    policy_version,
    persona_version,
    usage_scope,
    is_active,
    note
) VALUES
    ('finance', 'cashflow_projection_days', 'NUMERIC', 30, NULL, 'day', 'SIM_FIXED', 'MVP-DECISION-20260825:FIN-CASH-01', 'HUMAN', 'v1.3-PROVISIONAL', 'v1.5', 'AGENT_MVP_DEMO', TRUE, 'MVP projected_cash_min 계산 Horizon.'),
    ('finance', 'cash_priority_high_ratio', 'NUMERIC', 1.0, NULL, 'ratio', 'SIM_FIXED', 'MVP-DECISION-20260825:FIN-CASH-02', 'HUMAN', 'v1.3-PROVISIONAL', 'v1.5', 'AGENT_MVP_DEMO', TRUE, 'projected_cash_min / minimum_operating_cash < 1.0 이면 HIGH.'),
    ('finance', 'cash_priority_medium_ratio', 'NUMERIC', 1.5, NULL, 'ratio', 'SIM_FIXED', 'MVP-DECISION-20260825:FIN-CASH-02', 'HUMAN', 'v1.3-PROVISIONAL', 'v1.5', 'AGENT_MVP_DEMO', TRUE, '1.0 이상 1.5 미만 MEDIUM, 1.5 이상 LOW.'),
    ('finance', 'cash_priority_reference', 'TEXT', NULL, 'minimum_cash_balance_krw', NULL, 'SIM_FIXED', 'MVP-DECISION-20260825:FIN-CASH-02', 'HUMAN', 'v1.3-PROVISIONAL', 'v1.5', 'AGENT_MVP_DEMO', TRUE, 'Cash Priority 기준은 프로젝트 정의서의 minimum_cash_balance_krw Policy를 사용한다.'),
    ('finance', 'debt_annual_rate', 'NUMERIC', 0.025, NULL, 'ratio', 'SIM_FIXED', 'MVP-DECISION-20260825:N9-DEMO', 'HUMAN', 'v1.3-PROVISIONAL', 'v1.5', 'AGENT_MVP_DEMO', TRUE, 'Historical financing baseline의 연 2.5%를 MVP Demo Contract에 고정.'),
    ('finance', 'debt_execution_date', 'TEXT', NULL, '2025-12-02', 'ISO_DATE', 'SIM_FIXED', 'MVP-DECISION-20260825:N9-DEMO', 'HUMAN', 'v1.3-PROVISIONAL', 'v1.5', 'AGENT_MVP_DEMO', TRUE, 'Historical Burn-in에서 가상 loan execution이 반영된 날짜와 일치.'),
    ('finance', 'debt_first_payment_rule', 'TEXT', NULL, 'EXECUTION_MONTH_END', NULL, 'SIM_FIXED', 'MVP-DECISION-20260825:N9-DEMO', 'HUMAN', 'v1.3-PROVISIONAL', 'v1.5', 'AGENT_MVP_DEMO', TRUE, '실행월 말일부터 월 Debt Service 시작. Historical 2025-12-31 이자와 정합.'),
    ('finance', 'debt_grace_months', 'NUMERIC', 36, NULL, 'month', 'SIM_FIXED', 'MVP-DECISION-20260825:N9-DEMO', 'HUMAN', 'v1.3-PROVISIONAL', 'v1.5', 'AGENT_MVP_DEMO', TRUE, 'MVP Demo 거치기간 36개월.'),
    ('finance', 'debt_grace_payment_mode', 'TEXT', NULL, 'INTEREST_ONLY', NULL, 'SIM_FIXED', 'MVP-DECISION-20260825:N9-DEMO', 'HUMAN', 'v1.3-PROVISIONAL', 'v1.5', 'AGENT_MVP_DEMO', TRUE, '거치기간에는 원금을 상환하지 않고 월 이자만 지급.'),
    ('finance', 'debt_interest_method', 'TEXT', NULL, 'OUTSTANDING_PRINCIPAL_ANNUAL_RATE_DIV_12', NULL, 'SIM_FIXED', 'MVP-DECISION-20260825:N9-DEMO', 'HUMAN', 'v1.3-PROVISIONAL', 'v1.5', 'AGENT_MVP_DEMO', TRUE, '월 이자 = 해당 지급 시점 직전 미상환 원금 × 연이율 ÷ 12.'),
    ('finance', 'debt_payment_day_rule', 'TEXT', NULL, 'MONTH_END', NULL, 'SIM_FIXED', 'MVP-DECISION-20260825:N9-DEMO', 'HUMAN', 'v1.3-PROVISIONAL', 'v1.5', 'AGENT_MVP_DEMO', TRUE, '매월 말일 지급. 기존 2025-12-31 Burn-in 이자 반영일과 정합.'),
    ('finance', 'debt_payment_frequency', 'TEXT', NULL, 'MONTHLY', NULL, 'SIM_FIXED', 'MVP-DECISION-20260825:N9-DEMO', 'HUMAN', 'v1.3-PROVISIONAL', 'v1.5', 'AGENT_MVP_DEMO', TRUE, '월 1회 Debt Service.'),
    ('finance', 'debt_principal_krw', 'NUMERIC', 45272104.184486, NULL, 'KRW', 'SIM_FIXED', 'MVP-DECISION-20260825:N9-DEMO', 'HUMAN', 'v1.3-PROVISIONAL', 'v1.5', 'AGENT_MVP_DEMO', TRUE, 'SIM-BURNIN-202512의 recommended financing과 정합성을 유지한 MVP Demo 실행원금.'),
    ('finance', 'debt_repayment_method', 'TEXT', NULL, 'EQUAL_PRINCIPAL_AFTER_GRACE', NULL, 'SIM_FIXED', 'MVP-DECISION-20260825:N9-DEMO', 'HUMAN', 'v1.3-PROVISIONAL', 'v1.5', 'AGENT_MVP_DEMO', TRUE, '거치 종료 후 남은 36개월 동안 원금균등상환.'),
    ('finance', 'debt_runtime_status', 'TEXT', NULL, 'SIM_FIXED_EXECUTED', NULL, 'SIM_FIXED', 'MVP-DECISION-20260825:N9-DEMO', 'HUMAN', 'v1.3-PROVISIONAL', 'v1.5', 'AGENT_MVP_DEMO', TRUE, 'Finance A/B MVP 실행용 Debt Contract. 실제 금융기관 승인 상태를 의미하지 않음.'),
    ('finance', 'debt_term_months', 'NUMERIC', 72, NULL, 'month', 'SIM_FIXED', 'MVP-DECISION-20260825:N9-DEMO', 'HUMAN', 'v1.3-PROVISIONAL', 'v1.5', 'AGENT_MVP_DEMO', TRUE, 'MVP Demo 총 대출기간 72개월.'),
    ('finance', 'margin_defense_floor_rate', 'NUMERIC', 0.267, NULL, 'ratio', 'SIM_FIXED', 'PROJECT-DEFINITION-V1.2:MARGIN-DEFENSE-GRACE', 'HUMAN', 'v1.3-PROVISIONAL', 'v1.5', 'AGENT_MVP_DEMO', TRUE, '거치기 손익분기 CM 24.66% + 2%p 기준 = 0.267. 현재 MVP LOAN_BASELINE 거치구간 적용값. N9 완료 후 재산정 대상.'),
    ('finance', 'minimum_cash_balance_krw', 'NUMERIC', 12941280, NULL, 'KRW', 'SIM_FIXED', 'PROJECT-DEFINITION-V1.2:minimum_cash_balance', 'HUMAN', 'v1.3-PROVISIONAL', 'v1.5', 'AGENT_MVP_DEMO', TRUE, '프로젝트 정의서 기준 최소 현금 잔고. 월 기본 인건비 1개월 Reserve 12,941,280원.'),
    ('finance', 'monthly_labor_cost_krw', 'NUMERIC', 12941280, NULL, 'KRW/month', 'SIM_FIXED', 'SRC-FIN-PERSONA', 'HUMAN', 'v1.3-PROVISIONAL', 'v1.5', 'AGENT_MVP_DEMO', TRUE, '통합 Persona v1.5의 월 기본 인건비를 MVP Cashflow Event 기준으로 사용.'),
    ('finance', 'payroll_date', 'NUMERIC', 10, NULL, 'day-of-month', 'SIM_FIXED', 'SRC-FIN-N6', 'HUMAN', 'v1.3-PROVISIONAL', 'v1.5', 'AGENT_MVP_DEMO', TRUE, 'Finance v2.2.1 / 프로젝트 v2.3 후속 결정. 급여 지급일 매월 10일 확정. 기존 25일 Seed를 supersede.'),
    ('finance', 'purchase_payment_days', 'NUMERIC', 0, NULL, 'day', 'SIM_FIXED', 'SRC-FIN-PERSONA', 'HUMAN', 'v1.3-PROVISIONAL', 'v1.5', 'AGENT_MVP_DEMO', TRUE, '매입대금 지급일은 매입일 기준 D+0 calendar days — 매입 당일 지급. 출처는 재무 Persona (PERSONA-V1.3.purchase_payment_days = 0, persona_evidences → EV-SRC-FIN-PERSONA). 분할 매입은 각 회차별 실제 매입일 기준 D+0 적용. 계약 만기일이 토·일이어도 원장 due_date 는 계약일 그대로이고, 실제 현금 유출만 다음 월요일이다. H1에 확정 payment_date가 존재하면 해당 값이 authoritative.')
ON CONFLICT (policy_version, domain, policy_key) DO UPDATE SET
    value_kind = EXCLUDED.value_kind,
    value_numeric = EXCLUDED.value_numeric,
    value_text = EXCLUDED.value_text,
    unit = EXCLUDED.unit,
    evidence_grade = EXCLUDED.evidence_grade,
    source_ref = EXCLUDED.source_ref,
    approved_by = EXCLUDED.approved_by,
    persona_version = EXCLUDED.persona_version,
    usage_scope = EXCLUDED.usage_scope,
    is_active = EXCLUDED.is_active,
    note = EXCLUDED.note,
    updated_at = CURRENT_TIMESTAMP;
