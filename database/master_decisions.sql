-- 마스터 사용자 결정 기록 — master_decisions (2026-08-27)
--
-- ══════════════════════════════════════════════════════════════════════════
-- 회의 미결정 12번 — "사용자 선택 이후 실제 실행 여부 기록 방법" 에 대한 답이다.
-- ══════════════════════════════════════════════════════════════════════════
--
-- ★ 왜 새 표인가 — `orchestrator_agent_runs` 에 넣지 않는다.
--   그 표의 한 행은 **에이전트 실행 1건**이다. 결정은 실행이 아니라 **사람의 행위**라
--   `plan`·`runtime_status`·`request_payload` 가 전부 뜻을 잃는다. 무엇보다
--   **한 요청에 결정이 여러 번 붙는다** (제시 → 조건부 재요청 → 최종 선택).
--   1:N 은 표를 나누는 자리다.
--
--   `master_runs_migration.sql` 의 "새 표를 만들지 않는다" 는 **실행끼리** 나누지
--   말라는 것이었다. 결정은 다른 종류의 사실이다.
--
-- ★ append-only. UPDATE·DELETE 하지 않는다.
--   번복도 새 행이고 `decision_seq` 최대가 유효하다. 덮어쓰면 "왜 이 결정이 나왔는지"
--   가 사라진다 — 결정 이력은 감사 대상이다.
--
-- ★ 이 표는 마스터 Flow 가 쓰지 않는다.
--   승인 게이트가 **툴 바깥**에 있어야 마스터가 스스로 통과시킬 수 없다 (8/26 회의).
--   `flow.py` 는 이 모듈을 임포트하지 않는다.
--
-- ★ `scenario_label` 을 CHECK 로 열거하지 않는다.
--   '보수·기본·공격' 은 **매입의 계약**이다. DDL 에 박으면 매입이 라벨을 바꿀 때
--   DB 마이그레이션이 따라온다. 대신 코드가 **그 실행이 실제로 내놓은 안** 과
--   대조한다 — 열거보다 강한 검사다 (없는 안을 승인할 수 없다).

-- ★ 이 표는 `orchestrator_agent_runs` 보다 **나중에** 만들어야 한다.
--   run_id 가 그쪽을 참조한다. 실행 순서는 `database/README.md` 를 본다.
--
-- 개정 이력
--   2026-08-30  run_id 신설 — 결정이 어느 실행을 승인했는지 기록한다.
--               새 DB 는 이 파일 하나로 선다. 이미 데이터가 있는 DB 를 옮길 때만
--               `master_decisions_run_id.sql` (ALTER 판) 을 쓴다.

BEGIN;

CREATE TABLE IF NOT EXISTS haetdeul.master_decisions (
    decision_id           UUID        PRIMARY KEY,

    -- 업무 키. `orchestrator_agent_runs.request_id` 와 같은 값이지만 이것만으로는
    -- FK 를 걸 수 없다 — 그쪽은 재실행마다 행이 늘어 request_id 가 UNIQUE 가 아니다.
    -- **그래서 run_id 를 같이 든다** (아래).
    request_id            TEXT        NOT NULL,
    decision_seq          INT         NOT NULL,

    -- ★ 이 결정이 **보고 있던 실행** (2026-08-30 신설).
    --   request_id 만으로는 부족했다 — 실측에서 한 업무 키에 실행이 75행이었고,
    --   1회차 승인이 그중 68건 가운데 무엇을 승인한 것인지 DB 에 없었다.
    --   이력 조회는 최신 실행을 주므로, 재실행이 한 번 더 일어나면 **사람이 승인한
    --   수량과 화면이 승인됐다고 말하는 수량이 달라진다** (라벨은 같아서 눈에 안 띈다).
    --
    --   NULL 은 **"실행이 없다"가 아니라 "어느 실행인지 기록되지 않았다"** 이다 —
    --   2026-08-30 이전 행이 그렇다. 시각으로 추정해 채우지 않는다 (§1.2-10).
    run_id                UUID        NULL,

    decision              TEXT        NOT NULL,
    scenario_label        TEXT        NULL,
    condition_text        TEXT        NULL,

    -- 누가 정했나. 승인자가 없는 승인은 승인이 아니다.
    decided_by            TEXT        NOT NULL,

    -- 조건부 재요청이 낳은 후속 실행. 대화형 재요청(회의 3.6)이 이력에서 끊기지 않게 한다.
    follow_up_request_id  TEXT        NULL,

    -- 결정 시점의 종료 코드. 나중에 재실행으로 종료 코드가 바뀌어도
    -- **무엇을 보고 결정했는지** 는 그대로 남아야 한다.
    end_code_at_decision  TEXT        NOT NULL,

    note                  TEXT        NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT master_decisions_decision_check
        CHECK (decision IN ('APPROVE', 'REJECT_ALL', 'REQUEST_CHANGE')),

    -- 승인에는 반드시 고른 안이 있고, 나머지는 안을 고르지 않는다.
    -- 거절인데 라벨이 붙어 있으면 "무엇을 거절했나"가 두 가지로 읽힌다.
    CONSTRAINT master_decisions_scenario_required
        CHECK (
            (decision =  'APPROVE' AND scenario_label IS NOT NULL)
         OR (decision <> 'APPROVE' AND scenario_label IS NULL)
        ),

    -- 조건 없는 재요청은 재요청이 아니라 그냥 거절이다.
    CONSTRAINT master_decisions_condition_required
        CHECK (decision <> 'REQUEST_CHANGE' OR condition_text IS NOT NULL),

    CONSTRAINT master_decisions_seq_positive
        CHECK (decision_seq >= 1),

    -- 같은 요청에 같은 회차가 둘일 수 없다. 동시 결정은 여기서 걸린다.
    CONSTRAINT master_decisions_unique_seq
        UNIQUE (request_id, decision_seq),

    -- ★ **복합 FK** — run_id 만 맞는 것으로는 부족하다.
    --   (run_id) 단독이면 코드 실수로 **다른 업무 키의 실행**을 가리켜도 통과한다.
    --   쌍으로 걸면 "결정과 실행의 업무 키가 같다" 를 DB 가 보장한다. 트리거가
    --   필요 없다 — run_id 가 PK 라 참조 대상에 UNIQUE 를 하나 얹으면 된다
    --   (`orchestrator_agent_runs_run_request_unique`).
    --
    --   PostgreSQL 기본인 MATCH SIMPLE 이라 **참조 컬럼 중 하나라도 NULL 이면 검사를
    --   건너뛴다** — run_id 가 NULL 인 옛 행이 그대로 통과한다.
    --
    --   ON DELETE RESTRICT: 결정이 가리키는 실행은 지울 수 없다. SET NULL 이면
    --   실행을 지우는 순간 **결정이 조용히 근거를 잃는다.**
    CONSTRAINT master_decisions_run_fk
        FOREIGN KEY (run_id, request_id)
        REFERENCES haetdeul.orchestrator_agent_runs (run_id, request_id)
        ON DELETE RESTRICT
);

-- "그 요청 어떻게 됐나" 는 최신 결정부터 본다.
CREATE INDEX IF NOT EXISTS idx_master_decisions_request_id
    ON haetdeul.master_decisions (request_id, decision_seq DESC);

-- "이 실행에 붙은 결정" — 반대 방향 조회.
CREATE INDEX IF NOT EXISTS idx_master_decisions_run_id
    ON haetdeul.master_decisions (run_id)
    WHERE run_id IS NOT NULL;

COMMENT ON TABLE haetdeul.master_decisions IS
    '마스터가 제시한 시나리오에 대한 사람의 결정. append-only — 번복은 새 행이고 decision_seq 최대가 유효하다.';
COMMENT ON COLUMN haetdeul.master_decisions.end_code_at_decision IS
    '결정 시점의 종료 코드. 재실행으로 값이 바뀌어도 무엇을 보고 결정했는지는 보존된다.';
COMMENT ON COLUMN haetdeul.master_decisions.follow_up_request_id IS
    '조건부 재요청이 낳은 후속 실행의 업무 키. 대화형 재요청 체인을 잇는다.';
COMMENT ON COLUMN haetdeul.master_decisions.run_id IS
    '이 결정이 보고 있던 실행. NULL 은 "실행이 없다"가 아니라 "어느 실행인지 기록되지 않았다"이다 — 2026-08-30 이전 행이 그렇다.';

COMMIT;
