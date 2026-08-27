-- 마스터 에이전트 실행이력 — `orchestrator_agent_runs` 확장 (2026-08-27)
--
-- ★ 새 표를 만들지 않는다.
--   마스터의 한 실행도 "API 한 번 · 요청/응답 원문"이라 기존 표와 모양이 같다.
--   표를 나누면 "그날 무슨 일이 있었나"를 두 곳에서 합쳐 봐야 한다.
--
-- 바뀌는 것 셋:
--   ① agent 에 'master' 추가
--   ② request_id 컬럼 — 마스터는 UUID 가 아니라 업무 키(REQ-20260827-0001)로 조회한다
--   ③ plan JSONB — 실행 계획(누구를 어떤 순서로 불렀나). 정의서 §1.2-11
--
-- ★ plan 을 response_payload 안에 두지 않고 컬럼으로 뺀 이유
--   검증 Tool 의 ④ 실행 계획 온전성 검사(M-16)가 이것만 읽는다. 응답 원문 안에 묻어 두면
--   JSONB 경로를 파고들어야 하고, 응답 스키마가 바뀔 때마다 검증이 따라 흔들린다.

BEGIN;

-- ① agent 어휘 확장
ALTER TABLE haetdeul.orchestrator_agent_runs
    DROP CONSTRAINT IF EXISTS orchestrator_agent_runs_agent_check;

ALTER TABLE haetdeul.orchestrator_agent_runs
    ADD CONSTRAINT orchestrator_agent_runs_agent_check
    CHECK (agent IN ('orchestrator', 'critic', 'master'));

-- ② 업무 키로 조회 — 같은 날 재실행(run_seq 2)을 구분한다
ALTER TABLE haetdeul.orchestrator_agent_runs
    ADD COLUMN IF NOT EXISTS request_id TEXT NULL;

CREATE INDEX IF NOT EXISTS idx_orchestrator_agent_runs_request_id
    ON haetdeul.orchestrator_agent_runs (request_id)
    WHERE request_id IS NOT NULL;

-- ③ 실행 계획 — 마스터 행에만 채워진다
ALTER TABLE haetdeul.orchestrator_agent_runs
    ADD COLUMN IF NOT EXISTS plan JSONB NULL;

COMMENT ON COLUMN haetdeul.orchestrator_agent_runs.request_id IS
    '마스터 업무 키 (REQ-YYYYMMDD-NNNN). 오케·Critic 행에서는 NULL.';
COMMENT ON COLUMN haetdeul.orchestrator_agent_runs.plan IS
    '실행 계획 — 누구를 어떤 목적으로 몇 번째로 불렀나 (정의서 §1.2-11). 시각은 담지 않는다.';

COMMIT;
