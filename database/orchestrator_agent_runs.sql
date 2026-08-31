-- 오케스트레이터 · Critic · 마스터 실행이력.
--
-- ★ 코어는 여전히 DB 를 읽지 않는다 (§5.1). 이 표는 **API 층이 끝난 뒤** 요청·응답을
--   그대로 적재하는 감사 기록이다. 계산 입력으로 되읽지 않는다.
--
-- finance_agent_runs · logistics_agent_runs 와 같은 모양이되 agent 축이 하나 더 있다 —
-- 오케 · Critic · 마스터가 같은 표를 쓰고 cycle 어휘도 다르기 때문이다.
--
-- ★ 마스터가 새 표를 쓰지 않는 이유
--   마스터의 한 실행도 "API 한 번 · 요청/응답 원문"이라 모양이 같다. 표를 나누면
--   "그날 무슨 일이 있었나"를 두 곳에서 합쳐 봐야 한다.
--
-- 개정 이력
--   2026-08-27  마스터 에이전트 신설분을 본 DDL 에 반영 (agent='master' · request_id · plan).
--               같은 날 ALTER 스크립트로 먼저 적용했고, 새 DB 는 이 파일 하나로 선다.
--               ALTER 판은 `master_runs_migration.sql` 에 남아 있다 — **이미 데이터가
--               있는 DB 를 옮길 때만** 쓰고, 신규 구축에는 이 파일을 쓴다.
--   2026-08-30  (run_id, request_id) UNIQUE 추가 — `master_decisions.run_id` 가
--               복합 FK 로 참조한다. **새 제약이 아니다**: run_id 가 이미 PK 라
--               쌍도 이미 유일하고, FK 가 참조할 수 있게 선언만 얹는 것이다.
--               쓰기 경로에 영향이 없다. ALTER 판은 `master_decisions_run_id.sql`.

CREATE TABLE IF NOT EXISTS haetdeul.orchestrator_agent_runs (
    run_id UUID PRIMARY KEY,

    -- ★ 'master' 는 2026-08-27 회의로 신설됐다. 오케스트레이터가 마스터 에이전트가 되면서
    --   기존 두 어휘를 지우지 않고 더한다 — 과거 행의 판정을 나중에 바꾸지 않는다.
    agent TEXT NOT NULL CHECK (agent IN ('orchestrator', 'critic', 'master')),
    cycle TEXT NOT NULL CHECK (cycle IN ('PROCUREMENT', 'SALES', 'DAY', 'A', 'B')),
    as_of DATE NOT NULL,
    run_seq INTEGER NOT NULL DEFAULT 1,
    snapshot_id TEXT NULL,
    runtime_status TEXT NOT NULL
        CHECK (runtime_status IN ('READY', 'RUNTIME_NOT_READY', 'ERROR')),

    -- Critic 전용 판정 축. 오케 · 마스터 행에서는 NULL 이다.
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
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- ── 마스터 신설분 (2026-08-27) ────────────────────────────────────────────
    --
    -- 마스터는 UUID 가 아니라 **업무 키**로 조회된다. 사용자가 "그 요청 어떻게 됐냐"고
    -- 묻는 단위가 request_id 이기 때문이다. 오케 · Critic 행에서는 NULL 이다.
    request_id TEXT NULL,

    -- 실행 계획 — 누구를 어떤 목적으로 몇 번째로 불렀나 (정의서 §1.2-11).
    --
    -- ★ response_payload 안에 두지 않고 컬럼으로 뺀 이유
    --   검증 Tool 의 ④ 실행 계획 온전성 검사(M-16)가 이것만 읽는다. 응답 원문 안에
    --   묻어 두면 JSONB 경로를 파고들어야 하고, 응답 스키마가 바뀔 때마다 검증이
    --   따라 흔들린다.
    plan JSONB NULL,

    -- ★ `master_decisions` 가 (run_id, request_id) 로 참조한다 (2026-08-30).
    --   run_id 가 PK 라 이 쌍은 이미 유일하다 — **제약을 더하는 것이 아니라 FK 가
    --   참조할 수 있게 만드는 선언**이다. request_id 가 NULL 인 행(업무 키 없이 돈
    --   실행)은 NULL 이 서로 구별되므로 UNIQUE 를 방해하지 않는다.
    --
    --   왜 쌍인가 — 결정이 (run_id) 만 맞추면 **다른 업무 키의 실행**을 가리켜도
    --   DB 가 못 잡는다. 쌍으로 걸어야 "결정과 실행의 업무 키가 같다" 가 보장된다.
    CONSTRAINT orchestrator_agent_runs_run_request_unique
        UNIQUE (run_id, request_id)
);

CREATE INDEX IF NOT EXISTS idx_orchestrator_agent_runs_agent_cycle_as_of
    ON haetdeul.orchestrator_agent_runs (agent, cycle, as_of, created_at DESC);

-- "LLM 이 며칠째 FALLBACK 인가" 를 바로 볼 수 있게 한다. 관측이 없으면 조용히 나빠진다.
CREATE INDEX IF NOT EXISTS idx_orchestrator_agent_runs_llm_status
    ON haetdeul.orchestrator_agent_runs (llm_status, created_at DESC);

-- 업무 키 조회 — GET /master/runs/{request_id}.
-- 부분 인덱스다: 오케 · Critic 행은 request_id 가 NULL 이라 색인에 들어갈 이유가 없다.
CREATE INDEX IF NOT EXISTS idx_orchestrator_agent_runs_request_id
    ON haetdeul.orchestrator_agent_runs (request_id)
    WHERE request_id IS NOT NULL;

COMMENT ON COLUMN haetdeul.orchestrator_agent_runs.request_id IS
    '마스터 업무 키 (REQ-YYYYMMDD-NNNN). 오케·Critic 행에서는 NULL.';
COMMENT ON COLUMN haetdeul.orchestrator_agent_runs.plan IS
    '실행 계획 — 누구를 어떤 목적으로 몇 번째로 불렀나 (정의서 §1.2-11). 시각은 담지 않는다.';
