-- inventory_reservations 에 released_as_of 한 칸을 더한다 (2026-09-09 · 물류 · WP-3 M3)
--
-- ══════════════════════════════════════════════════════════════════════════
-- 🔴 **이 파일은 아직 실 DB 에 적용하지 않았다.** 적용 시점은 따로 정한다.
--
--     신규 구축  →  database/30_logistics_wms_schema.sql §6 (같은 칸을 만든다)
--     운영 중 DB →  이 파일 (ADD COLUMN)
--
-- 🔴 **같은 스키마 변경이 두 곳에 있다.** 어느 하나만 고치면 두 스키마가 조용히
--    갈린다 (README §2). 칸 정의를 바꾸면 반드시 둘 다 고친다.
-- ══════════════════════════════════════════════════════════════════════════
--
-- ★ 왜 만드는가 — **과거 예약 상태를 벽시각으로 추정하고 있었다.**
--
--   `inventory_reservations` 에는 시뮬레이션 날짜 칸이 하나도 없다.
--
--     created_at   TIMESTAMPTZ DEFAULT now()   벽시각 (감사용)
--     updated_at   TIMESTAMPTZ DEFAULT now()   벽시각 (감사용)
--     status       TEXT                        **지금** 값
--
--   그래서 *"2026-01-15 에 이 예약이 살아 있었나"* 를 물으면 답할 근거가 없다.
--   `status` 는 지금 상태라 과거 정본이 아니고, `updated_at` 으로 자르면
--   **DB 를 손본 실제 시각**이 시뮬레이션 사실일 행세를 한다 — 같은 데이터를
--   내일 다시 조회하면 어제와 다른 과거가 나온다.
--
--   ⇒ 놓아준 **시뮬레이션 날짜**를 한 칸에 적는다. 그러면
--
--     released_as_of IS NULL  또는 > as_of   →  그날 살아 있던 예약
--     released_as_of <= as_of                →  그날 이미 놓아준 예약
--
--   가 되어 벽시계를 아무리 돌려도 같은 답이 나온다.
--
-- 🔴 **`status` 를 걷지 않는다.** 그 칸은 Current 축(`RESERVED` ·
--    `PARTIALLY_ALLOCATED` · `ALLOCATED` · `RELEASED` · `CANCELLED`)의 정본으로
--    계속 쓰인다. 이 칸이 대신하는 것은 *"언제"* 하나뿐이다.
--
-- 🔴 **`reserved_qty_kg` 도 안 건드린다 — 그리고 코드가 그 값을 보존한다.**
--
--   세 칸이 각자 다른 질문에 답한다.
--
--     reserved_qty_kg   이 예약이 **확보했던** 양      놓아준 뒤에도 안 줄어든다
--     status            지금 잡고 있나
--     released_as_of    언제부터 안 잡고 있나
--
--   ⚠️ 종전 `release_reservation` 은 `reserved_qty_kg = 0` 을 함께 썼는데, 그 한 줄이
--      **놓아주기 전의 과거를 지웠다** (01-10 에 60kg · 01-20 에 해제 → 01-15 조회가
--      0kg). 이 칸이 세우려는 Historical 이 그래서 반쪽이었다. WP-3 보정 2 가 그 0 을
--      걷었고, *"그날 잡고 있었나"* 는 위 두 칸이 답한다.
--
-- 🔴 **CHECK 를 걸지 않는다.** *"RELEASED 면 released_as_of 가 있어야 한다"* 같은
--    제약은 매력적이지만, 이 칸이 생기기 전에 놓아준 행이 있으면 그 제약이 기존
--    데이터를 거짓으로 만든다. 실측(2026-09-09)에서는 8행 전부 `ALLOCATED` 라
--    지금 걸어도 통과하지만, **지금 통과한다는 이유로 제약을 거는 것**은 나중에
--    Backfill 없이 값을 채우는 경로를 막는다. 규율은 코드가 지킨다
--    (`outbound.release_reservation` 이 `released_as_of` 를 필수 인자로 받는다).
--
-- 🔴 **INDEX 를 만들지 않는다.** 이 칸만으로 거르는 질의가 없다 — Historical 은
--    늘 `sim_run_id` 로 먼저 좁히고 그 축에는 이미 FK 가 있다. 안 쓰는 인덱스는
--    쓰기마다 비용만 낸다.
--
-- ⚠️ **Backfill 이 없다.** 놓아준 날짜를 되살릴 근거가 어디에도 없어서다 —
--    `updated_at` 으로 채우면 이 칸을 만든 이유를 그대로 배반한다. 기존 행은
--    `NULL`(= 아직 살아 있다)로 남고, 실측상 그것이 사실이다 (8행 전부 `ALLOCATED`).
--
-- ★ **두 번 돌려도 안전하다** (`IF NOT EXISTS`).
--
-- ★ **additive 다.** 기존 칸을 바꾸지도 지우지도 않는다.
--
-- 🔴 **배포 순서 — migration 이 먼저, 코드가 나중이다.**
--
--   이 칸을 읽는 production 경로가 셋이다.
--
--     outbound._reservation                     예약 조회 (예약·할당·출고·해제 전부)
--     historical_repository.reservation_state_at  Historical 예약·할당
--     console_service.get_outbound_console       위를 그대로 쓴다 · /api/logistics/outbound
--
--   칸이 없는 DB 에 새 코드를 먼저 올리면 그 셋이 `column does not exist` 로 깨진다.
--   반대 순서(칸 먼저)는 안전하다 — 옛 코드는 이 칸을 안 읽으므로 아무 영향이 없다.

BEGIN;

ALTER TABLE haetdeul.inventory_reservations
    ADD COLUMN IF NOT EXISTS released_as_of DATE;

COMMENT ON COLUMN haetdeul.inventory_reservations.released_as_of IS
    '이 예약을 놓아준 시뮬레이션 날짜 (WP-3). NULL = 아직 살아 있다. 🔴 as_of < 이 값인 날에는 여전히 살아 있던 예약이다 — Historical 이 이 칸으로 그날 상태를 유도한다. status 는 지금 값이라 과거 정본이 아니고, updated_at 은 벽시각이라 시뮬레이션 날짜가 아니다.';

COMMIT;

-- ═══════════════════════════════════════════════════════════════════════════
-- 적용 뒤 검증
-- ═══════════════════════════════════════════════════════════════════════════
--
--   ① 칸이 생겼나 — 1행이어야 한다
--     SELECT column_name, data_type, is_nullable
--       FROM information_schema.columns
--      WHERE table_schema = 'haetdeul' AND table_name = 'inventory_reservations'
--        AND column_name = 'released_as_of';
--
--   ② 기존 행이 안 바뀌었나 — 8행 · 전부 ALLOCATED · released_as_of 전부 NULL
--     SELECT status, count(*), count(released_as_of) AS 값있음
--       FROM haetdeul.inventory_reservations GROUP BY status;
--
--   ③ 놓아준 예약이 그날부터만 빠지나 (production 함수로 확인 · rollback)
--     BEGIN;
--       -- outbound.release_reservation(conn, reservation_id=…, released_as_of=DATE 'D')
--       -- historical_repository.reservation_state_at(as_of=D-1) → 살아 있음
--       -- historical_repository.reservation_state_at(as_of=D)   → 놓아줌
--     ROLLBACK;
