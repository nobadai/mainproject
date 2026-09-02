-- 마스터 에이전트 실행이력. **마스터 소유 표다.**
--
-- ★ 왜 표를 나누는가 (2026-09-02)
--   `orchestrator_agent_runs` 는 오케 · Critic · 마스터 셋이 한 표를 쓰고 `agent` 축으로
--   갈랐다. 그때는 "그날 무슨 일이 있었나" 를 한 곳에서 보려는 판단이었다.
--
--   나누는 이유는 셋이다.
--     ① 어휘의 소유가 없다. 조회(STATUS)를 이력에 남기려 해도 CHECK 어휘를 고치려면
--        오케 · Critic 행의 뜻까지 건드려야 해서, 지금까지 **조회를 안 적는 쪽**을
--        골라 왔다 (`ask_service.py` 주석). 예산을 쓰는 호출이 이력에서 안 보인다.
--     ② 마스터가 안 쓰는 칸이 절반이다. `critic_status` · `coverage_*` · `llm_*` ·
--        `snapshot_id` 는 마스터 행에서 늘 NULL 이다.
--     ③ 마스터가 필요한 축이 없다. 품목과 종료 코드가 JSONB 안에 묻혀 있어
--        "배추가 며칠째 E2 인가" 를 보려면 payload 를 파야 한다.
--
--   `master_decisions` 가 이미 마스터 소유 표이므로 짝이 맞는다.
--
-- ★ 옛 표는 지우지 않는다.
--   Critic 이 `agent='critic'` 으로 쓰고 읽는다 (`app/critic/router.py`).
--   마스터 행만 이 표로 옮기고 `orchestrator_agent_runs` 는 그대로 둔다.
--   기존 DB 이관은 `master_agent_runs_migration.sql` 이 한다 — 이 파일은 신규 구축용이다.
--
-- ★ 코어는 여전히 DB 를 읽지 않는다 (§5.1).
--   이 표는 API 층이 끝난 뒤 요청 · 응답을 그대로 적재하는 감사 기록이고,
--   계산 입력으로 되읽지 않는다.

CREATE TABLE IF NOT EXISTS haetdeul.master_agent_runs (
    run_id UUID PRIMARY KEY,

    -- 업무 키 (REQ-YYYYMMDD-NNNN). 마스터는 UUID 가 아니라 이것으로 조회된다 —
    -- 사용자가 "그 요청 어떻게 됐냐" 고 묻는 단위이기 때문이다.
    --
    -- NULL 을 허용한다: 업무 키 없이 도는 실행(조회 등)이 있고, 그것도 이력이다.
    request_id TEXT NULL,

    as_of DATE NOT NULL,

    -- ★ **STATUS 가 새로 들어왔다.** 옛 표에는 없어서 조회를 이력에 안 적고 있었다.
    --   조회도 예산을 쓰고 부서를 부른다. 안 남기면 그 호출이 이력에서 사라지는데,
    --   검증 6계열의 M-16 이 막으려는 것이 정확히 "안 보이는 호출" 이다.
    --
    --   A · B 는 오케 어휘라 가져오지 않는다. 마스터는 그 사이클을 돌지 않는다.
    cycle TEXT NOT NULL
        CHECK (cycle IN ('PROCUREMENT', 'SALES', 'STATUS', 'DAY')),

    -- 같은 업무 키로 여러 번 돌 때의 순번. append-only 라 행이 여럿 생긴다.
    run_seq INTEGER NOT NULL DEFAULT 1,

    -- ★ 품목 축 (신설). 매입은 품목 하나씩 돈다.
    --   4품목을 한 사이클에 돌리면 실행이 품목마다 1건이라, 업무 키만으로는
    --   어느 행이 배추인지 알 수 없다 (M-26 미결의 전제이기도 하다).
    --
    --   품목 어휘를 CHECK 로 닫지 않는다 — 어휘의 소유는 `master/commitment.py`
    --   의 ITEM_CODES 이고, 같은 규칙을 두 곳에 두면 조용히 갈린다.
    item TEXT NULL,

    -- ★ 종료 코드 (신설). E1~E5 를 response_payload 안에서 꺼냈다.
    --   "배추가 며칠째 E2 인가" 는 운영이 실제로 묻는 질문인데, JSONB 를 파야
    --   답이 나오면 아무도 안 본다.
    --
    --   CHECK 로 닫지 않는다 — 코드 어휘가 늘면(판매) DDL 이 병목이 된다.
    --   대신 아래 runtime_status 가 3값으로 닫혀 있어 이상값은 그쪽에서 걸린다.
    end_code TEXT NULL,

    -- 실행 환경이 섰는가. **회사 상태가 아니다.**
    -- E4(미가동)만 RUNTIME_NOT_READY 다. E2(보류) · E3(반려) · E5(계획 없음)는
    -- 돌긴 돈 날이라 READY 다 — 이 구분이 무너지면 "부서가 죽은 날" 과
    -- "부서가 반대한 날" 이 이력에서 같아 보인다.
    runtime_status TEXT NOT NULL
        CHECK (runtime_status IN ('READY', 'RUNTIME_NOT_READY', 'ERROR')),

    -- Critic 커버리지. "56검사 중 몇 개가 돌았나" 를 날짜로 훑기 위해 컬럼으로 둔다.
    -- 정직 계수(2026-09-01)로 숫자의 뜻이 바뀌었으므로 추이가 의미를 갖는다.
    coverage_ran INTEGER NULL,
    coverage_total INTEGER NULL,

    elapsed_ms INTEGER NULL,

    -- 실행 계획 — 누구를 어떤 목적으로 몇 번째로 불렀나 (정의서 §1.2-11).
    --
    -- ★ response_payload 안에 두지 않고 컬럼으로 뺀 이유
    --   검증 Tool 의 실행 계획 온전성 검사(M-16)가 이것만 읽는다. 응답 원문 안에
    --   묻어 두면 JSONB 경로를 파야 하고, 응답 스키마가 바뀔 때마다 검증이 흔들린다.
    --
    -- ★ 시각을 담지 않는다. 계획은 같은 입력에 같은 값이어야 한다.
    --   언제 돌았는지는 created_at 이 답한다.
    plan JSONB NULL,

    request_payload JSONB NOT NULL,
    response_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- ★ `master_decisions` 가 (run_id, request_id) 로 참조한다.
    --   run_id 가 PK 라 이 쌍은 이미 유일하다 — 제약을 더하는 것이 아니라
    --   FK 가 참조할 수 있게 만드는 선언이다.
    --
    --   왜 쌍인가 — 결정이 run_id 만 맞추면 **다른 업무 키의 실행**을 가리켜도
    --   DB 가 못 잡는다. 쌍으로 걸어야 "결정과 실행의 업무 키가 같다" 가 보장된다.
    CONSTRAINT master_agent_runs_run_request_unique
        UNIQUE (run_id, request_id)
);

-- 업무 키 조회 — GET /master/runs/{request_id}. 가장 잦은 조회다.
-- 최신 행을 먼저 집으므로 created_at 을 내림차순으로 얹는다.
CREATE INDEX IF NOT EXISTS idx_master_agent_runs_request_id
    ON haetdeul.master_agent_runs (request_id, created_at DESC)
    WHERE request_id IS NOT NULL;

-- "그날 무엇을 돌렸나" · 사이클별 훑기.
CREATE INDEX IF NOT EXISTS idx_master_agent_runs_as_of_cycle
    ON haetdeul.master_agent_runs (as_of DESC, cycle, created_at DESC);

-- ★ 품목별 추이 — "배추가 며칠째 E2 인가".
--   품목과 종료 코드를 컬럼으로 뺀 이유가 이 인덱스다.
CREATE INDEX IF NOT EXISTS idx_master_agent_runs_item_end_code
    ON haetdeul.master_agent_runs (item, end_code, as_of DESC)
    WHERE item IS NOT NULL;

COMMENT ON TABLE haetdeul.master_agent_runs IS
    '마스터 에이전트 실행이력. 마스터 소유. 옛 orchestrator_agent_runs 의 agent=master 행을 대체한다.';
COMMENT ON COLUMN haetdeul.master_agent_runs.request_id IS
    '업무 키 (REQ-YYYYMMDD-NNNN). 업무 키 없이 도는 실행에서는 NULL.';
COMMENT ON COLUMN haetdeul.master_agent_runs.cycle IS
    'PROCUREMENT | SALES | STATUS | DAY. STATUS 는 조회 — 옛 표에 없어 이력에 못 남기던 것이다.';
COMMENT ON COLUMN haetdeul.master_agent_runs.item IS
    '이번 실행이 다룬 품목. 매입은 품목 하나씩 돈다. 어휘의 소유는 master/commitment.py 의 ITEM_CODES.';
COMMENT ON COLUMN haetdeul.master_agent_runs.end_code IS
    '종료 코드 E1~E5. response_payload 안에도 있지만 추이 조회를 위해 컬럼으로 뺐다.';
COMMENT ON COLUMN haetdeul.master_agent_runs.plan IS
    '실행 계획 — 누구를 어떤 목적으로 몇 번째로 불렀나. 시각은 담지 않는다.';
