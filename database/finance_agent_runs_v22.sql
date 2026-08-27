CREATE TABLE IF NOT EXISTS haetdeul.finance_agent_runs_v22 (
    run_id UUID PRIMARY KEY,
    request_id TEXT NOT NULL,
    agent TEXT NOT NULL CHECK (agent = 'finance'),
    mode TEXT NOT NULL CHECK (mode IN ('PRE_PURCHASE', 'SCENARIO_VALIDATION')),
    as_of DATE NOT NULL,
    policy_version TEXT NOT NULL,
    trigger TEXT NOT NULL CHECK (trigger IN ('ML_COMPLETE', 'USER_REQUEST')),
    call_seq INTEGER NOT NULL CHECK (call_seq >= 1),
    runtime_status TEXT NOT NULL CHECK (runtime_status IN ('READY', 'RUNTIME_NOT_READY', 'ERROR')),
    business_status TEXT NOT NULL CHECK (business_status IN ('ok', 'conditional', 'reject', 'skipped')),
    request_payload JSONB NOT NULL,
    response_payload JSONB NOT NULL,
    used_tools JSONB NOT NULL,
    tool_order JSONB NOT NULL,
    observations JSONB NOT NULL,
    rules_applied JSONB NOT NULL,
    replans INTEGER NOT NULL,
    llm_status TEXT NOT NULL,
    llm_model TEXT NOT NULL,
    llm_attempts INTEGER NOT NULL,
    llm_fallback_used BOOLEAN NOT NULL,
    elapsed_ms INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE haetdeul.finance_agent_runs_v22
    ADD COLUMN IF NOT EXISTS policy_version TEXT NOT NULL DEFAULT 'UNKNOWN',
    ADD COLUMN IF NOT EXISTS trigger TEXT NOT NULL DEFAULT 'USER_REQUEST'
        CHECK (trigger IN ('ML_COMPLETE', 'USER_REQUEST')),
    ADD COLUMN IF NOT EXISTS call_seq INTEGER NOT NULL DEFAULT 1
        CHECK (call_seq >= 1);

CREATE INDEX IF NOT EXISTS idx_finance_runs_v22_request
    ON haetdeul.finance_agent_runs_v22 (request_id, created_at DESC);
