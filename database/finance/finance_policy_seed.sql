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
--    2026-09-04 관측 시점의 운영 DB 행은 아직 `7` 을 들고 있다. 원장은 그 값과
--    맞지 않는다 — `purchases` 16/16 이 `payment_due_date = purchase_date` 이고,
--    `payables` 16/16 이 `due_date = purchase_date` 다. 승인된 정책은 0 이고
--    DB 행이 뒤처져 있다. 이 파일은 **승인된 정책**을 적는다.
--
--    이 브랜치는 운영 DB 의 값을 바꾸지 않았다. 반영은 운영 반영 절차로 한다.

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
    ('finance', 'purchase_payment_days', 'NUMERIC', 0, NULL, 'day', 'SIM_FIXED', 'FINANCE-DECISION-20260827:N5', 'HUMAN', 'v1.3-PROVISIONAL', 'v1.5', 'AGENT_MVP_DEMO', TRUE, 'Finance MVP 확정 정책. 매입대금 지급일은 매입일 기준 D+0 calendar days — 매입 당일 지급. 분할 매입은 각 회차별 실제 매입일 기준 D+0 적용. 계약 만기일이 토·일이어도 원장 due_date 는 계약일 그대로이고, 실제 현금 유출만 다음 월요일이다. H1에 확정 payment_date가 존재하면 해당 값이 authoritative.')
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
