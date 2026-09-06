-- Finance Payable cancellation ledger contract.
--
-- Existing rows retain their original meaning: cancelled_amount_krw starts at zero and no
-- status or amount is rewritten. Apply this migration before wiring cancellation traffic.

ALTER TABLE haetdeul.payables
    ADD COLUMN IF NOT EXISTS cancelled_amount_krw numeric(18,6) DEFAULT 0 NOT NULL,
    ADD COLUMN IF NOT EXISTS cancelled_date date;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'haetdeul.payables'::regclass
          AND conname = 'payables_check'
          AND pg_get_constraintdef(oid) NOT ILIKE '%cancelled_amount_krw%'
    ) THEN
        ALTER TABLE haetdeul.payables DROP CONSTRAINT payables_check;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'haetdeul.payables'::regclass
          AND conname = 'payables_check'
    ) THEN
        ALTER TABLE haetdeul.payables
            ADD CONSTRAINT payables_check CHECK (
                abs(
                    original_amount_krw
                    - paid_amount_krw
                    - cancelled_amount_krw
                    - outstanding_amount_krw
                ) < 0.1
            );
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'haetdeul.payables'::regclass
          AND conname = 'payables_status_check'
          AND pg_get_constraintdef(oid) NOT ILIKE '%CANCELLED%'
    ) THEN
        ALTER TABLE haetdeul.payables DROP CONSTRAINT payables_status_check;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'haetdeul.payables'::regclass
          AND conname = 'payables_status_check'
    ) THEN
        ALTER TABLE haetdeul.payables
            ADD CONSTRAINT payables_status_check CHECK (
                status = ANY (ARRAY['OPEN', 'PARTIAL', 'SETTLED', 'WRITEOFF', 'CANCELLED'])
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'haetdeul.payables'::regclass
          AND conname = 'payables_cancelled_amount_nonnegative_check'
    ) THEN
        ALTER TABLE haetdeul.payables
            ADD CONSTRAINT payables_cancelled_amount_nonnegative_check
            CHECK (cancelled_amount_krw >= 0);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'haetdeul.payables'::regclass
          AND conname = 'payables_cancelled_state_check'
    ) THEN
        ALTER TABLE haetdeul.payables
            ADD CONSTRAINT payables_cancelled_state_check CHECK (
                status <> 'CANCELLED'
                OR (outstanding_amount_krw = 0 AND cancelled_date IS NOT NULL)
            );
    END IF;
END
$$;

COMMENT ON COLUMN haetdeul.payables.cancelled_amount_krw
    IS '승인/매입 원인 철회로 지급 없이 소멸한 금액(원).';
COMMENT ON COLUMN haetdeul.payables.cancelled_date
    IS '미지급 채무가 승인/매입 원인 철회로 취소된 날짜.';
