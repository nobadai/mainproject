-- master_decision_cancel.sql — `master_decisions` 에 CANCEL 어휘를 연다.
--
-- 🔴 CANCEL 은 REJECT_ALL 과 다르다.
--
--    REJECT_ALL   "이 안을 안 쓴다"      — 승인 **전** 판단. 장부를 안 건드린다
--    CANCEL       "승인했던 것을 물린다"  — 승인 **후** 사실. 장부 다섯을 되돌린다
--
--    둘을 한 어휘로 적으면 "거절해서 장부가 없는 것" 과 "취소해서 장부가 물린 것" 이
--    같아진다.
--
-- ⚠️ 이 마이그레이션은 **단독으로 적용하지 않는다.** 재무
--    (`database/finance/payable_cancellation.sql`) · 물류 · 마스터가 한 번에 선다 —
--    한쪽만 적용하면 어휘는 있는데 되돌릴 수 없는 상태가 된다.
--
-- ★ 기존 행은 건드리지 않는다. CHECK 를 넓히기만 한다.
--
-- 실측 (2026-09-06):
--   master_decisions_decision_check  APPROVE · REJECT_ALL · REQUEST_CHANGE
--   decision 분포                    APPROVE 26 · REQUEST_CHANGE 4
--   purchases_settlement_status_check  SETTLED · OPEN · CANCELLED   ← 이미 있다

BEGIN;

SET LOCAL search_path TO haetdeul, public;

-- ① 결정 어휘에 CANCEL 을 더한다.
ALTER TABLE haetdeul.master_decisions
    DROP CONSTRAINT IF EXISTS master_decisions_decision_check;

ALTER TABLE haetdeul.master_decisions
    ADD CONSTRAINT master_decisions_decision_check
    CHECK (decision = ANY (ARRAY['APPROVE', 'REJECT_ALL', 'REQUEST_CHANGE', 'CANCEL']));

-- ② `scenario_label` 규칙은 그대로 산다.
--
--    기존 제약이 "APPROVE 면 label 필수 · 아니면 NULL" 이라 CANCEL 은 자동으로
--    NULL 쪽이다. **취소는 안을 고르는 것이 아니므로 그것이 맞다** — 무엇을
--    취소했는지는 `master_decisions` 가 아니라 `purchases.settlement_status` 와
--    `payables.cancelled_date` 가 말한다 (재무 회신 §5 · 취소 사건 ID 를 따로 두지
--    않기로 한 결정).
--
--    ⚠️ 그래서 여기서 그 제약을 건드리지 않는다. 확인만 한다.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conrelid = 'haetdeul.master_decisions'::regclass
           AND conname = 'master_decisions_scenario_required'
    ) THEN
        RAISE EXCEPTION
            'master_decisions_scenario_required 가 없다 — CANCEL 이 label 을 달고 들어올 수 있다';
    END IF;
END $$;

-- ③ 넓힌 어휘가 실제로 통과하는지 그 자리에서 확인한다.
--
--    ★ "제약을 바꿨다" 와 "바꾼 대로 동작한다" 는 다르다. 롤백하는 삽입으로 잰다.
DO $$
DECLARE
    ok boolean := false;
BEGIN
    BEGIN
        INSERT INTO haetdeul.master_decisions
            (request_id, decision_seq, decision, decided_by, end_code_at_decision)
        VALUES ('MIGRATION-PROBE', 1, 'CANCEL', 'migration', 'E1_APPROVED');
        ok := true;
    EXCEPTION WHEN check_violation THEN
        ok := false;
    END;
    IF NOT ok THEN
        RAISE EXCEPTION 'CANCEL 이 여전히 CHECK 에 걸린다 — 어휘가 안 열렸다';
    END IF;
    DELETE FROM haetdeul.master_decisions WHERE request_id = 'MIGRATION-PROBE';
END $$;

COMMIT;
