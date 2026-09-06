-- inventory_lots.grade · derivation_status 의 NOT NULL 해제 (2026-09-05 · 물류)
--
-- ══════════════════════════════════════════════════════════════════════════
-- ⚠️  이 파일은 **이미 데이터가 있는 DB 를 옮길 때만** 쓴다.
--
--     신규 구축  →  database/10_domain_schema.sql (본 DDL 이 이미 nullable 이다)
--     운영 중 DB →  이 파일
--
-- 🔴 **같은 변경이 두 곳에 있다.** 어느 하나만 고치면 두 스키마가 조용히 갈린다
--    (README §2). `tests/logistics/test_inventory_lots_nullable_agree.py` 가 두 파일을
--    대조한다.
-- ══════════════════════════════════════════════════════════════════════════
--
-- ★ 왜 푸는가 — 실제 도착·검수를 거친 Lot 을 **지어내지 않고** 적기 위해서다.
--
--   grade
--     승인이 만든 매입 줄의 등급이 이미 NULL 이다 (`purchase_items.grade`).
--     물류 정규화표는 `상품 → 상` 같은 임의 치환을 **의도적으로 비워 두었고**
--     (`app/logistics/repository.py` `_RAW_GRADE_NORMALIZATION`), 런타임 계약은
--     이미 `grade: str | None` 이다. 현행 raw `상품` 도 스냅샷에서는 이미 `None` 으로
--     정규화된다 — 그래서 DB NULL 을 허용해도 **런타임 의미가 새로 생기지 않는다.**
--     "권위 있는 등급을 모른다" 를 DB 가 정직하게 적을 수 있게 될 뿐이다.
--
--   derivation_status
--     이 칸의 COMMENT 가 뜻을 좁게 못박고 있다 —
--     *"Burn-in Lot이 어떤 파생규칙으로 생성됐는지 나타내는 상태."*
--     실제로 받은 Lot 은 Burn-in 파생 재고가 **아니다.** 따라서 비-Burn-in Lot 의
--     올바른 값은 NULL 이다.
--     ⚠️ 이 단계에서 이 칸을 일반 provenance 축으로 **재정의하지 않는다.**
--
-- 🔴 **대체값을 만들지 않는다.** `UNKNOWN` · `UNSPECIFIED` · `상품` 되채우기 ·
--    `INBOUND_RECEIPT_ACCEPTED` 같은 새 어휘를 여기서 짓지 않는다. 없는 것은 NULL 이다.
--
-- 🔴 **행을 하나도 건드리지 않는다.** UPDATE · DELETE · INSERT · TRUNCATE 가 없다.
--    기존 80개 Burn-in Lot 의 `grade='상품'` · `derivation_status=
--    'OPERATIONAL_DERIVED_ROTATING_SAFETY_STOCK'` 은 **그대로 남는다.**
--    이 이관이 바꾸는 것은 *"앞으로 어떤 값이 허용되는가"* 뿐이다.
--
-- ★ 두 번 돌려도 안전하다. PostgreSQL 의 `ALTER COLUMN … DROP NOT NULL` 은 이미
--   nullable 인 칸에 대해 아무 일도 하지 않는다 — 그래서 PL/pgSQL 가드를 두지 않았다.
--   COMMENT 도 덮어쓰기라 반복 실행이 같은 결과를 낸다.
--
-- ★ 인덱스 · FK · CHECK · 뷰에 영향이 없다 (2026-09-05 카탈로그 실측):
--   두 칸을 참조하는 인덱스 0건 · CHECK 0건이고, FK 대상도 아니다.
--   `v_current_inventory` 만 `grade` 를 읽는데 `CASE WHEN l.grade = '중'` 이라
--   NULL 이면 ELSE 로 가서 그대로 돈다.

BEGIN;

ALTER TABLE haetdeul.inventory_lots
    ALTER COLUMN grade DROP NOT NULL;

ALTER TABLE haetdeul.inventory_lots
    ALTER COLUMN derivation_status DROP NOT NULL;

-- ★ 본 DDL 과 **같은 문구**여야 한다 — 신규 구축 DB 와 이관된 DB 의 메타데이터 계약이
--   갈리면 "어느 쪽이 맞나" 를 아무도 말해 주지 않는다.
COMMENT ON COLUMN haetdeul.inventory_lots.grade IS '권위 있는 품질등급. 미확정이면 NULL.';

COMMENT ON COLUMN haetdeul.inventory_lots.derivation_status IS 'Burn-in Lot이 어떤 파생규칙으로 생성됐는지 나타내는 상태. Burn-in이 아닌 Lot은 NULL.';

COMMIT;
