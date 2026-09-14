--
-- partner_banchan_factory_001_seed.sql — 2026-09-14 신규 거래처 한 곳 (초안)
--
-- 🔴🔴 **적용 금지 — 판매·재무 확인 · 스키마 적용 · 리허설 검증(2026-09-15 21시) 전.**
--      값은 재무·판매가 2026-09-14 에 확정해 전달한 것이다. 확인이 끝나기 전에 돌리지 않는다.
--
-- 🔴 **`partner_effective_from.sql` 이 먼저다.** 이 파일은 `active_from` ·
--    `effective_from` 칸을 쓴다. 칸이 없으면 INSERT 가 터진다.
--
-- 시나리오: 기존 단일 김치공장 거래 구조에서 2026-09-14 경기권 반찬제조공장이
-- 신규 거래처로 유입되어, 기존 거래처의 약 30% 규모의 추가 수요가 발생한 상황을
-- 시뮬레이션한다.
--
-- ══════════════════════════════════════════════════════════════════════════
-- 무엇을 넣는가 — 세 표 · 다섯 행
--
--   partners               BANCHAN_FACTORY_001 1행 · active_from 2026-09-14
--   partner_item_demands   배추 215.2 · 무 46.3 · 양파 4.3 (합 265.8 kg/일) · effective_from 2026-09-14
--   partner_credit_limits  3,000,000원 · effective_from 2026-09-14 · SIM_FIXED
-- ══════════════════════════════════════════════════════════════════════════
--
-- 🔴 **실제 계약이 아니다.** 전부 시뮬레이션 고정값이다 (`provisional = true` ·
--    `evidence_grade = SIM_FIXED`). 기존 거래처 여신 seed 와 같은 태도다
--    (`database/finance/partner_credit_limit_seed.sql`).
--
-- ★ **값이 안 온 칸은 NULL 이다.** `production_tier` · `annual_production_ton` ·
--   `operating_days_per_year` · `factory_area` · `cold_storage_capacity_ton` ·
--   `relationship_days` 는 전달되지 않았고, 표 정의상 NULL 을 허용한다
--   (`database/10_domain_schema.sql` `CREATE TABLE haetdeul.partners` · NOT NULL 은
--   partner_id · partner_name · partner_type · active · provisional 뿐). 지어내지 않는다.
--
-- ★ **건고추·피마늘 등 다른 품목은 행이 없다.** 이 거래처는 계약 세 품목만 산다.
--
-- ★ **여신 행은 재무 표다.** 값·관습(`PCL-{partner}-{yyyymmdd}` · `source_ref`
--   `FIN-SALES-CREDIT-V1` · `v1.0-PROVISIONAL` · `AGENT_MVP_DEMO`)은 기존 행
--   (`partner_credit_limit_seed.sql`)을 그대로 따랐다. 재무 확인 대상이다.
--
-- ⚠️ **적용하면 `v_current_partner_demand` 가 두 행이 된다.** 그 뷰는 날짜를 안 보고,
--   `v_dashboard_state` 가 그 뷰를 `CROSS JOIN` 하므로 대시보드 상태 뷰도 두 행이 된다.
--   저장소 앱 코드에서 두 뷰를 읽는 곳은 없다 (2026-09-14 grep). 마스터 수요 계산은
--   이 뷰를 안 읽는다.
--
-- ★ **멱등하다.** 이미 있으면 아무것도 안 한다 — 있는 값이 정본이고 덮어쓰지 않는다.

BEGIN;

SET LOCAL search_path TO haetdeul, public;

-- ── 1. 거래처 ─────────────────────────────────────────────────────────────
INSERT INTO haetdeul.partners (
    partner_id,
    partner_name,
    partner_type,
    client_type,
    production_tier,
    annual_production_ton,
    operating_days_per_year,
    factory_region,
    factory_city,
    factory_area,
    cold_storage_capacity_ton,
    relationship_days,
    order_cycle_days,
    sales_collection_days,
    pricing_contract_type,
    active,
    provisional,
    active_from,
    note
)
VALUES (
    'BANCHAN_FACTORY_001',
    '반찬제조공장',
    'CUSTOMER',
    '반찬제조공장',
    NULL,  -- 값 안 옴
    NULL,  -- 값 안 옴
    NULL,  -- 값 안 옴
    '경기도',
    '평택시',
    NULL,  -- 값 안 옴
    NULL,  -- 값 안 옴
    NULL,  -- 값 안 옴
    2,
    30,
    'MARKET_LINKED_COST_PLUS_CM',
    true,
    true,
    DATE '2026-09-14',
    '2026-09-14 신규 거래처 시나리오. 시뮬레이션 고정값이며 실제 계약이 아님.'
)
ON CONFLICT (partner_id) DO NOTHING;

-- ── 2. 품목별 일수요 ──────────────────────────────────────────────────────
INSERT INTO haetdeul.partner_item_demands (
    partner_id, item_id, daily_demand_kg, demand_basis, provisional, effective_from
)
VALUES
    ('BANCHAN_FACTORY_001', 'ITEM-BAECHU', 215.2, '신규 거래처 SIM_FIXED · 김치공장 약 30%', true, DATE '2026-09-14'),
    ('BANCHAN_FACTORY_001', 'ITEM-MU',      46.3, '신규 거래처 SIM_FIXED · 김치공장 약 30%', true, DATE '2026-09-14'),
    ('BANCHAN_FACTORY_001', 'ITEM-YANGPA',   4.3, '신규 거래처 SIM_FIXED · 김치공장 약 30%', true, DATE '2026-09-14')
ON CONFLICT (partner_id, item_id) DO NOTHING;

-- ── 3. 여신 한도 ──────────────────────────────────────────────────────────
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
    'PCL-BANCHAN_FACTORY_001-20260914',
    'BANCHAN_FACTORY_001',
    3000000.000000,
    'KRW',
    DATE '2026-09-14',
    NULL,
    'SIM_FIXED',
    'FIN-SALES-CREDIT-V1',
    'v1.0-PROVISIONAL',
    'AGENT_MVP_DEMO',
    true,
    'MVP Simulation 신규 거래처 여신한도 (2026-09-14). 실제 Vendor/Contract 한도가 아님.'
WHERE EXISTS (
    SELECT 1 FROM haetdeul.partners WHERE partner_id = 'BANCHAN_FACTORY_001'
)
ON CONFLICT (partner_id, effective_from) DO NOTHING;

COMMIT;


-- ══════════════════════════════════════════════════════════════════════════
-- 확인 — 적용 후
-- ══════════════════════════════════════════════════════════════════════════
--
-- ① 세 표에 섰나
--
--   SELECT partner_id, active_from, order_cycle_days, sales_collection_days, provisional
--     FROM haetdeul.partners WHERE partner_id = 'BANCHAN_FACTORY_001';
--   SELECT item_id, daily_demand_kg, effective_from
--     FROM haetdeul.partner_item_demands WHERE partner_id = 'BANCHAN_FACTORY_001';
--   -- 기대: 3행 · 합 265.8
--   SELECT credit_limit_krw, effective_from, effective_to
--     FROM haetdeul.partner_credit_limits WHERE partner_id = 'BANCHAN_FACTORY_001';
--
-- ② 🔴 2026-09-13 에 유효한 거래처가 기존 하나뿐인가
--
--   SELECT partner_id FROM haetdeul.partners
--    WHERE active = true AND partner_type = 'CUSTOMER' AND active_from <= DATE '2026-09-13';
--   -- 기대: KIMCHI_FACTORY_001 하나


-- ══════════════════════════════════════════════════════════════════════════
-- 되돌리기 — 🔴 이 거래처로 판매·채권이 이미 섰으면 FK 로 막힌다
-- ══════════════════════════════════════════════════════════════════════════
--
-- 막히면 지우지 말고 `partners.active = false` 로 닫는다. 장부를 지우면 그날의 판정을
-- 다시 만들 수 없다.
--
--   BEGIN;
--   DELETE FROM haetdeul.partner_credit_limits
--    WHERE partner_credit_limit_id = 'PCL-BANCHAN_FACTORY_001-20260914';
--   DELETE FROM haetdeul.partner_item_demands WHERE partner_id = 'BANCHAN_FACTORY_001';
--   DELETE FROM haetdeul.partners WHERE partner_id = 'BANCHAN_FACTORY_001';
--   COMMIT;
