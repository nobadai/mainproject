-- inventory_allocations.allocation_basis 에 FEFO_AUTO_SELECTED 를 더한다 (2026-09-08 · 물류)
--
-- ══════════════════════════════════════════════════════════════════════════
-- ⚠️  이 파일은 **이미 데이터가 있는 DB 를 옮길 때만** 쓴다.
--
--     신규 구축  →  database/30_logistics_wms_schema.sql (본 DDL 이 이미 셋을 허용한다)
--     운영 중 DB →  이 파일
--
-- 🔴 **같은 변경이 두 곳에 있다.** 어느 하나만 고치면 두 스키마가 조용히 갈린다
--    (README §2). `tests/logistics/test_logistics_allocation_basis_agree.py` 가 두 파일과
--    `app/logistics/outbound.AllocationBasis` 셋을 대조한다.
-- ══════════════════════════════════════════════════════════════════════════
--
-- ★ 왜 더하는가 — **시뮬레이션 자동 할당에는 사람이 없기 때문이다.**
--
--   기존 두 값은 둘 다 *"사람이 무엇을 했나"* 를 적는다.
--
--     FEFO_TOOL_CONFIRMED   사람이 Tool 후보를 보고 그대로 확정했다
--     HUMAN_OVERRIDE        사람이 후보와 다르게 정했다
--
--   `app/logistics/fefo_allocation.allocate_reserved_stock_fefo` 는 사람 없이
--   FEFO 규칙이 Lot 을 고른다. 이것을 기존 두 값 중 하나로 적으면
--
--     FEFO_TOOL_CONFIRMED 로  →  "사람이 추천을 따랐다" 가 **거짓으로 선다**
--     HUMAN_OVERRIDE 로       →  **없던 사람이 생긴다**
--
--   그래서 셋째 값이 필요하다. 이름은 Master ↔ Logistics 합의값이다.
--
-- 🔴 **기존 행을 하나도 건드리지 않는다.** UPDATE · DELETE · INSERT · TRUNCATE 가 없다.
--    이미 있는 `FEFO_TOOL_CONFIRMED` · `HUMAN_OVERRIDE` 행은 그대로 유효하고, 이
--    이관이 바꾸는 것은 *"앞으로 어떤 값이 허용되는가"* 뿐이다.
--
-- 🔴 **기존 두 값의 뜻을 바꾸지 않는다.** 넓히기만 하고 좁히지 않으므로 사람 경로
--    (`outbound.allocate_stock` 직접 호출)의 의미는 그대로다.
--
-- ⚠️ **`DROP CONSTRAINT` 가 있는 이유.** PostgreSQL 은 CHECK 를 제자리에서 넓히지
--    못한다 — 지우고 다시 거는 것이 유일한 길이다. 본 DDL(`30_`)이 *"DROP 이 한 줄도
--    없다"* 를 지키는 파일이라 이 문장은 **이관 판에만** 둔다.
--
--    ★ 두 문장 사이에 제약이 없는 순간이 생기지만 **한 트랜잭션 안이라 밖에서 안
--      보인다.** `ALTER TABLE` 이 ACCESS EXCLUSIVE 를 쥐므로 그동안 다른 세션이
--      이 표에 쓰지도 못한다.
--
-- ★ 두 번 돌려도 안전하다. `DROP CONSTRAINT IF EXISTS` 로 없는 제약을 지나가고,
--   `ADD CONSTRAINT` 는 바로 앞에서 지운 자리에 건다. COMMENT 도 덮어쓰기라
--   반복 실행이 같은 결과를 낸다.
--
-- ★ 적용 전 확인 — 어휘 밖 값이 이미 있으면 ADD 가 실패한다 (그때는 그 행이 먼저
--   설명돼야 한다. 이 파일이 데이터를 고치지 않는 이유다).
--
--     SELECT allocation_basis, count(*)
--       FROM haetdeul.inventory_allocations
--      GROUP BY 1;

BEGIN;

ALTER TABLE haetdeul.inventory_allocations
    DROP CONSTRAINT IF EXISTS ck_inventory_allocations_basis;

ALTER TABLE haetdeul.inventory_allocations
    ADD CONSTRAINT ck_inventory_allocations_basis
    CHECK (allocation_basis IN ('FEFO_TOOL_CONFIRMED', 'HUMAN_OVERRIDE',
                                'FEFO_AUTO_SELECTED'));

-- ★ 본 DDL 과 **같은 문구**여야 한다 — 신규 구축 DB 와 이관된 DB 의 메타데이터 계약이
--   갈리면 "어느 쪽이 맞나" 를 아무도 말해 주지 않는다.
COMMENT ON COLUMN haetdeul.inventory_allocations.allocation_basis IS
    'FEFO_TOOL_CONFIRMED = Tool 후보를 사람이 그대로 확정 / HUMAN_OVERRIDE = 사람이 다르게 정함 / FEFO_AUTO_SELECTED = 사람 없이 FEFO 규칙이 고름 (시뮬레이션). 기본값을 두지 않는다 — 호출자가 반드시 말한다.';

COMMIT;
