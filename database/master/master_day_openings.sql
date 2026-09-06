-- master_day_openings.sql — 개장 정본. **마스터가 소유한다.**
--
-- 🔴 정본 키는 (as_of, sim_run_id) 다 (재무·물류 2026-09-06 합의).
--
--    Master 공통 정본     (as_of, sim_run_id)
--    Finance 실제 상태     (sim_run_id, as_of, financing_mode)
--    Logistics 실제 상태   (sim_run_id, as_of, usage_scope)
--
--    ★ financing_mode · usage_scope 는 **파트 고유 축**이라 마스터가 안 가진다.
--      가지기 시작하면 파트가 늘 때마다 정본 키가 바뀐다.
--
-- 🔴 이 표는 **파트 트랜잭션 밖에서** 쓴다.
--
--    실패도 기록해야 시도 횟수를 셀 수 있는데, 파트 트랜잭션 안에 넣으면 롤백될 때
--    **실패했다는 사실까지 사라진다.** persistence.record 가 응답 이력을 따로 남기는
--    것과 같은 자리다.
--
-- ⚠️ 이 표가 없으면 day_gate 가 RETRY_OPEN_DAY 와 CONTACT_OPERATOR 를 못 가른다.
--    계약(260904_마스터_통보_개장Gate_응답모양_next_action §2)이 "횟수로 가른다" 고
--    적었고, 셀 자리가 여기다.

BEGIN;

SET LOCAL search_path TO haetdeul, public;

CREATE TABLE IF NOT EXISTS haetdeul.master_day_openings (
    as_of              date        NOT NULL,
    sim_run_id         text        NOT NULL,

    -- 전체 어휘 넷. 파트 어휘(PART_*)는 parts_json 안에 있다.
    result             text        NOT NULL,

    -- ★ **이 날에 대해 몇 번 불렀나.** 성공·실패를 다 센다.
    --   계약이 이름을 이렇게 정했고 그대로 쓴다.
    attempt_count      integer     NOT NULL DEFAULT 1,

    -- 🔴 **연속 실패 횟수.** 성공하면 0 으로 돌아간다.
    --
    --    계약은 next_action 을 "실패 1회째는 재시도, 2회 이상은 사람" 으로 가르는데,
    --    그것은 **연속 실패**여야 맞다 — 어제 성공하고 오늘 처음 실패한 것을 "2번째"로
    --    세면 재시도 한 번 없이 사람을 부른다.
    --
    --    ⚠️ 계약에 attempt_count 하나만 적었던 것이 얕았다. 두 칸을 둔다.
    failure_count      integer     NOT NULL DEFAULT 0,

    -- 사람이 읽는 한 줄. 화면까지 나간다.
    reason             text,

    -- ★ **파트별 결과 원문.** PART_FAILED 의 내부 상세는 여기에만 남고 화면에 안 간다
    --   (계약 §6). 마스터가 그것을 해석하지 않는다 — 담아 두기만 한다.
    parts_json         jsonb,

    first_attempt_at   timestamptz NOT NULL DEFAULT now(),
    last_attempt_at    timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT master_day_openings_pkey PRIMARY KEY (as_of, sim_run_id),
    CONSTRAINT master_day_openings_result_check
        CHECK (result = ANY (ARRAY['OPENED', 'ALREADY_OPENED', 'NOT_OPENED', 'REJECTED_GAP'])),
    CONSTRAINT master_day_openings_attempt_positive CHECK (attempt_count >= 1),
    CONSTRAINT master_day_openings_failure_nonneg   CHECK (failure_count >= 0),
    -- 🔴 성공한 날은 연속 실패가 0 이어야 한다. 둘이 어긋나면 next_action 이 틀린다.
    CONSTRAINT master_day_openings_success_resets
        CHECK (result NOT IN ('OPENED', 'ALREADY_OPENED') OR failure_count = 0)
);

COMMENT ON TABLE haetdeul.master_day_openings IS
    '개장 정본. 마스터 소유. 정본 키는 (as_of, sim_run_id) — 파트 고유 축은 안 가진다.';
COMMENT ON COLUMN haetdeul.master_day_openings.attempt_count IS
    '이 날에 대해 부른 횟수. 성공·실패를 다 센다.';
COMMENT ON COLUMN haetdeul.master_day_openings.failure_count IS
    '연속 실패 횟수. 성공하면 0. day_gate 의 next_action 이 이 값으로 재시도와 사람을 가른다.';
COMMENT ON COLUMN haetdeul.master_day_openings.parts_json IS
    '파트별 결과 원문. PART_FAILED 의 내부 상세는 여기에만 남고 화면에 안 간다.';

-- 넓힌 어휘가 실제로 통과하는지 그 자리에서 확인한다.
--
-- ★ "표를 만들었다" 와 "만든 대로 동작한다" 는 다르다. 롤백하는 삽입으로 잰다.
DO $$
BEGIN
    INSERT INTO haetdeul.master_day_openings (as_of, sim_run_id, result, attempt_count, failure_count)
    VALUES ('1900-01-01', 'MIGRATION-PROBE', 'REJECTED_GAP', 3, 2);

    -- 성공인데 연속 실패가 남아 있으면 막혀야 한다.
    BEGIN
        INSERT INTO haetdeul.master_day_openings (as_of, sim_run_id, result, failure_count)
        VALUES ('1900-01-02', 'MIGRATION-PROBE', 'OPENED', 1);
        RAISE EXCEPTION 'success_resets CHECK 가 안 걸린다 — 성공인데 연속 실패가 남는다';
    EXCEPTION WHEN check_violation THEN
        NULL;  -- 기대한 대로 막혔다
    END;

    DELETE FROM haetdeul.master_day_openings WHERE sim_run_id = 'MIGRATION-PROBE';
END $$;

COMMIT;
