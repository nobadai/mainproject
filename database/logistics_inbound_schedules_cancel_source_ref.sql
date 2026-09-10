-- inbound_schedules 에 cancel_source_ref 한 칸을 더한다 (2026-09-10 · 물류)
--
-- 🔴 **이 파일은 아직 실 DB 에 적용하지 않았다.** 적용 시점은 따로 정한다.
--
--     신규 구축  →  database/30_logistics_wms_schema.sql §3-0 (같은 칸을 만든다)
--     운영 중 DB →  이 파일 (ADD COLUMN)
--
-- ★ 승인 취소 근거(MASTER-CANCEL:…)를 생성 근거와 **분리해** 적는다.
--   Backfill 은 없다 — 이미 취소된 행의 근거는 어디에도 남아 있지 않다.
--
-- 🔴 **migration 이 먼저, 코드가 나중이다.** 칸이 없는 DB 에 새 코드를 먼저 올리면
--    마스터 승인 취소가 `column does not exist` 로 전이 전체를 롤백한다.

BEGIN;

ALTER TABLE haetdeul.inbound_schedules
    ADD COLUMN IF NOT EXISTS cancel_source_ref TEXT;

COMMENT ON COLUMN haetdeul.inbound_schedules.cancel_source_ref IS
    '입고 일정 취소 근거 (MASTER-CANCEL:{approval_id}@{취소일}). source_ref 는 생성 근거로 그대로 두고 덮지 않는다. 감사용이라 Historical 판정에는 쓰지 않는다 — 그날 살아 있었나는 cancelled_as_of 가 답한다.';

COMMIT;
