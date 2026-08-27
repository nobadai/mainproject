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

BEGIN;

CREATE TABLE IF NOT EXISTS haetdeul.master_decisions (
    decision_id           UUID        PRIMARY KEY,

    -- 업무 키. `orchestrator_agent_runs.request_id` 와 같은 값이지만 FK 를 걸지 않는다
    -- — 그쪽은 재실행마다 행이 늘어 request_id 가 UNIQUE 가 아니다.
    request_id            TEXT        NOT NULL,
    decision_seq          INT         NOT NULL,

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
        UNIQUE (request_id, decision_seq)
);

-- "그 요청 어떻게 됐나" 는 최신 결정부터 본다.
CREATE INDEX IF NOT EXISTS idx_master_decisions_request_id
    ON haetdeul.master_decisions (request_id, decision_seq DESC);

COMMENT ON TABLE haetdeul.master_decisions IS
    '마스터가 제시한 시나리오에 대한 사람의 결정. append-only — 번복은 새 행이고 decision_seq 최대가 유효하다.';
COMMENT ON COLUMN haetdeul.master_decisions.end_code_at_decision IS
    '결정 시점의 종료 코드. 재실행으로 값이 바뀌어도 무엇을 보고 결정했는지는 보존된다.';
COMMENT ON COLUMN haetdeul.master_decisions.follow_up_request_id IS
    '조건부 재요청이 낳은 후속 실행의 업무 키. 대화형 재요청 체인을 잇는다.';

COMMIT;
