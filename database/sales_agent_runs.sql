CREATE TABLE IF NOT EXISTS haetdeul.sales_agent_runs (
    run_id UUID PRIMARY KEY,
    cycle TEXT NOT NULL CHECK (cycle IN ('PROCUREMENT', 'SALES')),
    as_of DATE NOT NULL,
    snapshot_id TEXT NULL,
    runtime_status TEXT NOT NULL
        CHECK (runtime_status IN ('READY', 'RUNTIME_NOT_READY', 'ERROR')),
    request_payload JSONB NOT NULL,
    response_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sales_agent_runs_cycle_as_of_created_at
    ON haetdeul.sales_agent_runs (cycle, as_of, created_at DESC);
