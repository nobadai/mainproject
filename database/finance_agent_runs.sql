CREATE TABLE IF NOT EXISTS haetdeul.finance_agent_runs (
    run_id UUID PRIMARY KEY,
    cycle TEXT NOT NULL CHECK (cycle IN ('PROCUREMENT', 'SALES')),
    as_of DATE NOT NULL,
    snapshot_id TEXT NULL,
    runtime_status TEXT NOT NULL
        CHECK (runtime_status IN ('READY', 'RUNTIME_NOT_READY', 'ERROR')),
    verdict TEXT NULL,
    request_payload JSONB NOT NULL,
    response_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE haetdeul.finance_agent_runs
    ADD COLUMN IF NOT EXISTS verdict TEXT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_finance_agent_runs_verdict'
          AND conrelid = 'haetdeul.finance_agent_runs'::regclass
    ) THEN
        ALTER TABLE haetdeul.finance_agent_runs
            ADD CONSTRAINT ck_finance_agent_runs_verdict
            CHECK (verdict IN ('PASS', 'REVIEW_REQUIRED', 'FAIL'));
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_finance_agent_runs_cycle_as_of_created_at
    ON haetdeul.finance_agent_runs (cycle, as_of, created_at DESC);
