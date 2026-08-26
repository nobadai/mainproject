-- 오케스트레이터 · Critic 실행이력.
--
-- ★ 코어는 여전히 DB 를 읽지 않는다 (§5.1). 이 표는 **API 층이 끝난 뒤** 요청·응답을
--   그대로 적재하는 감사 기록이다. 계산 입력으로 되읽지 않는다.
--
-- finance_agent_runs · logistics_agent_runs 와 같은 모양이되 agent 축이 하나 더 있다 —
-- 오케와 Critic 이 같은 표를 쓰고 cycle 어휘도 다르기 때문이다.

CREATE TABLE IF NOT EXISTS haetdeul.orchestrator_agent_runs (
    run_id UUID PRIMARY KEY,
    agent TEXT NOT NULL CHECK (agent IN ('orchestrator', 'critic')),
    cycle TEXT NOT NULL CHECK (cycle IN ('PROCUREMENT', 'SALES', 'DAY', 'A', 'B')),
    as_of DATE NOT NULL,
    run_seq INTEGER NOT NULL DEFAULT 1,
    snapshot_id TEXT NULL,
    runtime_status TEXT NOT NULL
        CHECK (runtime_status IN ('READY', 'RUNTIME_NOT_READY', 'ERROR')),

    -- Critic 전용 판정 축. 오케 행에서는 NULL 이다.
    critic_status TEXT NULL CHECK (critic_status IN ('PASS', 'CONCERN', 'FAIL')),
    coverage_ran INTEGER NULL,
    coverage_total INTEGER NULL,

    -- LLM 관측 축 — Finance / Logistics 와 같은 어휘를 쓴다.
    llm_status TEXT NULL
        CHECK (llm_status IN ('SUCCESS', 'SKIPPED_TEMPLATE', 'FALLBACK', 'DISABLED')),
    llm_model TEXT NULL,
    llm_attempts INTEGER NULL,
    llm_fallback_used BOOLEAN NULL,
    elapsed_ms INTEGER NULL,

    request_payload JSONB NOT NULL,
    response_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_orchestrator_agent_runs_agent_cycle_as_of
    ON haetdeul.orchestrator_agent_runs (agent, cycle, as_of, created_at DESC);

-- "LLM 이 며칠째 FALLBACK 인가" 를 바로 볼 수 있게 한다. 관측이 없으면 조용히 나빠진다.
CREATE INDEX IF NOT EXISTS idx_orchestrator_agent_runs_llm_status
    ON haetdeul.orchestrator_agent_runs (llm_status, created_at DESC);
