-- 기존 여신한도 정본에 입력자 감사 칸을 추가한다.
--
-- 이 변경은 additive 이며 기존 한도 금액·기간·행을 수정하거나 삭제하지 않는다.
-- 과거 행에는 기존 source_ref 외에 입력자를 복원할 근거가 없으므로 명시적으로
-- LEGACY_UNKNOWN 을 기록한다. 새 쓰기는 API가 source_ref 와 recorded_by 를 각각 받는다.

BEGIN;

ALTER TABLE haetdeul.partner_credit_limits
    ADD COLUMN IF NOT EXISTS recorded_by text;

UPDATE haetdeul.partner_credit_limits
SET recorded_by = 'LEGACY_UNKNOWN'
WHERE recorded_by IS NULL;

ALTER TABLE haetdeul.partner_credit_limits
    ALTER COLUMN recorded_by SET NOT NULL;

COMMENT ON COLUMN haetdeul.partner_credit_limits.recorded_by IS
    '한도 값을 시스템에 입력한 기록자. source_ref 와 별개의 감사 정보다.';

COMMIT;
