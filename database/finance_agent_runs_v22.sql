-- Finance Agent v2.2 실행이력 — **신규 구축용 최종 모양.**
--
-- ★ 이미 만들어진 DB 를 올리는 것은 `finance_agent_runs_v22_sales_validation.sql`
--   이 한다. 여기에 마이그레이션 로직을 겹쳐 적지 않는다 — 이 파일은 "지금의 최종
--   모양", 그 파일은 "옛 DB 를 그 모양으로 옮기는 법" 이다.

CREATE TABLE IF NOT EXISTS haetdeul.finance_agent_runs_v22 (
    run_id UUID PRIMARY KEY,
    request_id TEXT NOT NULL,
    agent TEXT NOT NULL CHECK (agent = 'finance'),
    -- SALES_VALIDATION 은 판매 제안 재무 검증이다 (2026-09-02 Master 회신).
    -- 매입 SCENARIO_VALIDATION 과 다른 책임이라 mode 를 나눠 둔다 — 합치면
    -- (agent, mode, call_seq) 로 매입 검증과 판매 검증을 구분할 수 없다.
    mode TEXT NOT NULL CHECK (
        mode IN ('PRE_PURCHASE', 'SCENARIO_VALIDATION', 'SALES_VALIDATION')
    ),
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
