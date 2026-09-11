-- MVP 시뮬레이션 거래처 여신한도 seed.
--
-- 🔴 **실제 계약 한도가 아니다.** 저장소·DB·문서 어디에도 팀이 합의한 여신한도 값이
--    없다 (`app/finance/rules.py` 가 "여신한도는 저장소에 없다" 고 적어 두었다).
--    그래서 MVP 자동 실행이 돌 수 있도록 **시뮬레이션 고정값**을 명시적으로 세운다.
--
--    ```text
--    evidence_grade = SIM_FIXED      실제 Vendor/Contract 한도가 아님
--    usage_scope    = AGENT_MVP_DEMO 이 값이 쓰이는 범위를 이름으로 못박는다
--    ```
--
--    실제 계약 한도가 생기면 `effective_to` 로 이 구간을 닫고 새 행을 세운다 —
--    이 행을 덮어쓰지 않는다. 덮어쓰면 그날의 판정을 다시 만들 수 없다.
--
-- ★ **실제로 쓰이는 거래처만 넣는다.** 현재 `partners` 에 있는 CUSTOMER 는
--   `KIMCHI_FACTORY_001` 하나다. 쓰지 않는 거래처를 미리 만들어 두면 없는 계약이
--   장부에 서고, 나중에 누가 그것을 사실로 읽는다.
--
-- ★ **멱등하다.** 같은 (partner_id, effective_from) 이 이미 있으면 아무것도 하지
--   않는다 — 이미 있는 값이 정본이다. 예상과 다른 값이 있어도 덮어쓰지 않는다.

INSERT INTO haetdeul.partner_credit_limits (
    partner_credit_limit_id,
    partner_id,
    credit_limit_krw,
    currency,
    effective_from,
    effective_to,
    evidence_grade,
    source_ref,
    policy_version,
    usage_scope,
    is_active,
    note
)
SELECT
    'PCL-KIMCHI_FACTORY_001-20251201',
    'KIMCHI_FACTORY_001',
    10000000.000000,
    'KRW',
    DATE '2025-12-01',
    NULL,
    'SIM_FIXED',
    'FIN-SALES-CREDIT-V1',
    'v1.0-PROVISIONAL',
    'AGENT_MVP_DEMO',
    true,
    'MVP Simulation 고정 거래처 여신한도. 실제 Vendor/Contract 한도가 아님.'
WHERE EXISTS (
    SELECT 1 FROM haetdeul.partners WHERE partner_id = 'KIMCHI_FACTORY_001'
)
ON CONFLICT (partner_id, effective_from) DO NOTHING;
