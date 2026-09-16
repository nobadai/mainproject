-- Finance 일반 운영비의 생명주기(ACCRUED → PAID / CANCELLED)를 원장에 세운다.
--
-- 한 날짜 칸에 세 의미를 겹치지 않는다.
--
--     expense_date  비용이 발생한 날
--     due_date      지급하기로 한 날   ← 미래 현금유출 투영의 기준
--     paid_date     실제로 지급한 날   ← 현금 차감과 일마감의 기준
--
--
-- 🔴 **이 파일은 «없으면 만든다» 이지 «만든다» 가 아니다** (2026-09-16 실측).
--
--    세 칸은 공용 DB 에 **이미 있다.** 앞선 작업이 적용해 두었고, 이 판에서 다시 만들지
--    않는다. 그래서 전부 `IF NOT EXISTS` 이고, 이 스크립트를 몇 번 돌려도 이미 있는
--    환경에서는 아무 일도 일어나지 않는다.
--
--    ⚠️ 공용 DB 의 `daily_closings.operating_expense_cash_out_krw` 는 이미
--       `NOT NULL DEFAULT 0` 으로 서 있고, 그래서 과거 마감 2,946행이 전부 0 이다.
--       아래는 **NULL 허용**으로 만든다 — 새 환경에서는 «기록하지 않았다»(NULL)와
--       «세어 보니 0원이다»(0)가 갈려야 하기 때문이다. 이미 만들어진 공용 DB 쪽은
--       그 구분을 잃은 채로 있고, 되돌리려면 별도 판단이 필요하다. **여기서 고치지
--       않는다** — 되돌리는 ALTER 는 과거 행의 의미를 또 한 번 바꾸는 일이다.
--
--
-- 기존 행은 그대로 둔다. 이미 적힌 PAID 17행은 지급일을 모르고(실측 `paid_date` 전부
-- NULL), 그것을 추측해서 채우지 않는다 — 마감은 그런 행에 한해 expense_date 를 지급
-- 기준일로 읽는다 (LEGACY READ COMPATIBILITY ONLY).
--
-- ★ **`source_ref` · `recorded_by` 는 건드리지 않는다.** 두 칸도 공용 DB 에 이미 있다.
--   지우지 않고, 그렇다고 이번 생명주기의 쓰기 계약으로 올리지도 않는다 — 비용 원장의
--   근거 정본은 `evidence_id` 하나다.
--
-- additive 전용이다. DROP 없음 · 행 삭제 없음 · status 변경 없음 · backfill 없음.
BEGIN;

ALTER TABLE haetdeul.expenses
    ADD COLUMN IF NOT EXISTS due_date DATE NULL,
    ADD COLUMN IF NOT EXISTS paid_date DATE NULL;

-- 🔴 **DEFAULT 도 NOT NULL 도 걸지 않는다.** 기본값을 걸면 그 순간 과거 마감 전부가
--    «운영비 0원» 이 되고, 기록하지 않은 축과 세어 보니 없던 축이 같은 값이 된다.
ALTER TABLE haetdeul.daily_closings
    ADD COLUMN IF NOT EXISTS operating_expense_cash_out_krw NUMERIC(18,6) NULL;

-- 미래 현금유출 투영은 «그 실행의 ACCRUED 를 지급 예정일 순서로» 읽는다.
CREATE INDEX IF NOT EXISTS expenses_projection_axis_idx
    ON haetdeul.expenses (sim_run_id, status, due_date);

-- 일마감은 «그 실행의 PAID 를 지급일로» 읽는다.
CREATE INDEX IF NOT EXISTS expenses_paid_axis_idx
    ON haetdeul.expenses (sim_run_id, status, paid_date);

COMMENT ON COLUMN haetdeul.expenses.due_date IS
    '지급 예정일. ACCRUED 비용의 미래 현금유출 투영 기준일이다. 신규 비용은 반드시 채운다.';
COMMENT ON COLUMN haetdeul.expenses.paid_date IS
    '실제 지급일. PAID 비용의 현금 차감·일마감 기준일이다. 값이 없는 기존 PAID 행은 지급일 미상이다.';
COMMENT ON COLUMN haetdeul.daily_closings.operating_expense_cash_out_krw IS
    '일별 일반 운영비 현금유출(원). NULL 은 그 실행이 이 축을 기록하지 않았다는 뜻이고, 0원과 다르다.';

COMMIT;
