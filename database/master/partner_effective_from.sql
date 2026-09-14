--
-- partner_effective_from.sql — 거래처와 거래처 수요에 **언제부터** 칸을 연다 (2026-09-14)
--
-- 🔴🔴 **적용 금지 — 리허설 검증(2026-09-15 21시) 전.** 이 파일은 초안이다.
--      판매·재무 확인과 스키마 적용 결정이 끝난 뒤에 돌린다.
--
-- ══════════════════════════════════════════════════════════════════════════
-- 무엇을 여는가 — 칸 둘이다
--
--   partners.active_from               그 거래처가 거래에 들어온 날
--   partner_item_demands.effective_from 그 품목 일수요가 유효해진 날
-- ══════════════════════════════════════════════════════════════════════════
--
-- 시나리오: 기존 단일 김치공장 거래 구조에서 2026-09-14 경기권 반찬제조공장이
-- 신규 거래처로 유입되어, 기존 거래처의 약 30% 규모의 추가 수요가 발생한 상황을
-- 시뮬레이션한다.
--
-- ★★ **왜 날짜 칸이 필요한가.** 두 표에 날짜 칸이 없어서 새 거래처 행을 넣는 순간
--   과거 모든 날의 수요가 같이 늘어난다. 9/13 까지의 걷기 결과가 행 하나로 바뀐다.
--   날짜가 있어야 마스터가 **그날 유효한 거래처만** 센다
--   (`backend/app/master/inputs.py` `_in_force` · `sales_terms.active_partner_ids`).
--
-- ★ **기본값 2025-12-01 은 번인 시작일이다.** 기존 행이 기존 기간 전체를 덮어야
--   걷기 결과가 안 바뀐다. 근거:
--     `database/finance/partner_credit_limit_seed.sql`  KIMCHI_FACTORY_001 여신 effective_from 2025-12-01
--     `SIM-BURNIN-202512`                               번인 실행 이름이 2025년 12월이다
--   ⚠️ DB 의 `company_personas.burn_in_start` 는 저장소에서 읽을 수 없어 확인 ① 에서
--   적용 전에 대조한다. 다르면 **적용하지 말고** 기본값을 그 값으로 고친다.
--
-- ★ **NOT NULL 이다.** NULL 을 허용하면 *"언제부터인지 모른다"* 와 *"처음부터"* 가 한
--   칸에 들어가고, 읽는 쪽이 매번 다시 정해야 한다. `ADD COLUMN ... NOT NULL DEFAULT`
--   는 기존 행을 기본값으로 채운다.
--
-- ★ **기본 키를 안 바꾼다.** `partner_item_demands` 의 PK 는 `(partner_id, item_id)`
--   그대로다. 한 거래처·품목의 수요가 날짜별로 여러 행이 되는 일은 이번 시나리오에 없다.
--   마스터 코드는 여러 행이 와도 `effective_from` 이 가장 늦은 행을 쓰도록 짜여 있다.
--
-- ★ **뷰를 안 바꾼다.** 마스터는 더 이상 `v_current_partner_demand` 를 읽지 않는다
--   (주문 주기를 `partners.order_cycle_days` 에서 거래처마다 읽는다). 뷰 정의가
--   `p.*` · `d.*` 를 안 쓰므로 칸이 늘어도 뷰는 그대로 선다.
--   ⚠️ 그러나 두 번째 거래처 행이 들어가면 그 뷰는 **날짜와 무관하게 거래처 둘을 다
--   돌려준다** — 그리고 `v_dashboard_state` 가 그 뷰를 `CROSS JOIN` 하므로 행이 둘이
--   된다. 이것은 시드(`partner_banchan_factory_001_seed.sql`) 적용의 문제이고, 이
--   파일은 칸만 연다. 저장소 앱 코드에서 두 뷰를 읽는 곳은 없다 (2026-09-14 grep).
--
-- ★ **두 번 돌려도 안전하다** (`IF NOT EXISTS`). 되돌리는 법은 맨 아래에 있다.

BEGIN;

SET LOCAL search_path TO haetdeul, public;

ALTER TABLE haetdeul.partners
    ADD COLUMN IF NOT EXISTS active_from date NOT NULL DEFAULT DATE '2025-12-01';

ALTER TABLE haetdeul.partner_item_demands
    ADD COLUMN IF NOT EXISTS effective_from date NOT NULL DEFAULT DATE '2025-12-01';

COMMENT ON COLUMN haetdeul.partners.active_from IS
    '거래처가 거래에 들어온 날. 이 날 이전에는 수요·판매에 안 센다. 기본값 2025-12-01 은 번인 시작일로 기존 거래처가 기존 기간 전체를 덮게 한다.';
COMMENT ON COLUMN haetdeul.partner_item_demands.effective_from IS
    '품목 일수요가 유효해진 날. 이 날 이전에는 파생 수요에 안 센다. 기본값 2025-12-01 은 번인 시작일이다.';

COMMIT;


-- ══════════════════════════════════════════════════════════════════════════
-- 확인 — 적용 전 ① · 적용 후 ②③
-- ══════════════════════════════════════════════════════════════════════════
--
-- ① 적용 전: 기본값이 번인 시작일과 같은가
--
--   SELECT burn_in_start FROM haetdeul.company_personas;
--   -- 기대: 2025-12-01 (다르면 적용하지 않는다)
--
-- ② 적용 후: 기존 행이 전부 기본값으로 채워졌나
--
--   SELECT partner_id, active_from FROM haetdeul.partners ORDER BY partner_id;
--   SELECT partner_id, item_id, effective_from FROM haetdeul.partner_item_demands
--    ORDER BY partner_id, item_id;
--   -- 기대: 전부 2025-12-01
--
-- ③ 적용 후: 칸이 NOT NULL 로 붙었나
--
--   SELECT table_name, column_name, is_nullable, column_default
--     FROM information_schema.columns
--    WHERE table_schema = 'haetdeul'
--      AND column_name IN ('active_from', 'effective_from')
--      AND table_name IN ('partners', 'partner_item_demands');
--   -- 기대: 두 행 모두 is_nullable = NO · column_default '2025-12-01'::date


-- ══════════════════════════════════════════════════════════════════════════
-- 되돌리기 — 🔴 마스터 코드(`inputs.py` · `sales_terms.py`)가 이 칸을 읽는다
-- ══════════════════════════════════════════════════════════════════════════
--
-- 칸을 지우기 전에 마스터 코드를 이 판 이전 커밋으로 되돌려야 한다. 코드가 남은
-- 채 칸만 지우면 파생 수요가 조회 실패로 `MISSING` 이 되고 목록 규칙의 판매가
-- 전부 `FAILED` 가 된다 (지어내지 않고 멈춘다).
--
-- 시드(`partner_banchan_factory_001_seed.sql`)를 적용했다면 그 파일의 되돌리기를 먼저 돌린다.
--
--   BEGIN;
--   ALTER TABLE haetdeul.partner_item_demands DROP COLUMN IF EXISTS effective_from;
--   ALTER TABLE haetdeul.partners DROP COLUMN IF EXISTS active_from;
--   COMMIT;
