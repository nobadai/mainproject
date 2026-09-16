-- 사용자 입력 자금 조정 원장. 현금 잔액을 직접 덮어쓰지 않고 이유와 방향을 남긴다.
BEGIN;

CREATE TABLE IF NOT EXISTS haetdeul.finance_cash_adjustments (
    cash_adjustment_id TEXT PRIMARY KEY,
    sim_run_id TEXT NOT NULL,
    financing_mode TEXT NOT NULL,
    adjustment_date DATE NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('INFLOW', 'OUTFLOW')),
    category TEXT NOT NULL CHECK (category IN ('OWNER_INJECTION', 'OWNER_WITHDRAWAL', 'OTHER')),
    amount_krw NUMERIC NOT NULL CHECK (amount_krw > 0),
    source_ref TEXT NOT NULL,
    note TEXT NULL,
    recorded_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS finance_cash_adjustments_axis_date_idx
    ON haetdeul.finance_cash_adjustments (sim_run_id, financing_mode, adjustment_date, created_at);

COMMENT ON TABLE haetdeul.finance_cash_adjustments IS
    '사용자 기록 자금 입금·출금 원장. Finance State 현금 변경의 근거와 감사 정보를 보존한다.';

COMMIT;
