-- 마스터 실행이력을 `orchestrator_agent_runs` 에서 `master_agent_runs` 로 옮긴다.
--
-- ★ **이미 데이터가 있는 DB 를 옮길 때만 쓴다.** 신규 구축은 `master_agent_runs.sql`
--   하나로 선다.
--
-- ★ 옛 표를 지우지 않는다.
--   Critic 이 `agent='critic'` 으로 쓰고 읽는다 (`app/critic/router.py` ·
--   `app/orchestrator/persistence.py`). 마스터 행만 복사하고 옛 행은 남긴다.
--   마스터 행도 **지우지 않는다** — 지우면 되돌릴 수 없고, 옛 표의 마스터 행은
--   읽는 코드가 사라진 시점부터 그냥 안 읽힌다. 정리는 Critic 이 자기 표를
--   가져간 뒤 한 번에 한다.
--
-- ★ 한 트랜잭션이다. FK 를 옮기는 중간 상태로 멈추면 결정이 실행을 못 가리킨다.
--
-- 실행
--   psql -v ON_ERROR_STOP=1 -f database/master_agent_runs_migration.sql
--
-- 되돌리기
--   §5 의 주석을 참고한다. FK 를 옛 표로 되돌리고 새 표를 지우면 원상태다.

BEGIN;

-- ── 1. 새 표를 만든다 ──────────────────────────────────────────────────────
--
-- 본 DDL 과 같은 내용이다. `master_agent_runs.sql` 을 먼저 실행했다면 IF NOT EXISTS
-- 로 조용히 넘어간다.

\i database/master_agent_runs.sql


-- ── 2. 마스터 행을 옮긴다 ──────────────────────────────────────────────────
--
-- ★ `item` 과 `end_code` 는 payload 에서 꺼낸다. 옛 표에는 컬럼이 없었다.
--   키가 없는 옛 행은 NULL 이 된다 — 없는 것을 지어내지 않는다.
--
-- ★ `coverage_*` 는 옛 표에 컬럼이 있지만 마스터 행에서는 늘 NULL 이었다.
--   그대로 옮긴다. 채워 넣지 않는다.
--
-- ★ 옛 `cycle` 어휘 5값 중 마스터가 쓴 것은 PROCUREMENT 뿐이다.
--   A · B 가 섞여 있으면 CHECK 에 걸려 이 스크립트가 멈춘다 — 조용히 넘어가지
--   않는 편이 낫다.
--
-- ON CONFLICT DO NOTHING: 스크립트를 두 번 돌려도 안전하다.

INSERT INTO haetdeul.master_agent_runs (
    run_id, request_id, as_of, cycle, run_seq,
    item, end_code, runtime_status,
    coverage_ran, coverage_total, elapsed_ms,
    plan, request_payload, response_payload, created_at
)
SELECT
    o.run_id,
    o.request_id,
    o.as_of,
    o.cycle,
    o.run_seq,
    o.request_payload ->> 'item'        AS item,
    o.response_payload ->> 'end_code'   AS end_code,
    o.runtime_status,
    o.coverage_ran,
    o.coverage_total,
    o.elapsed_ms,
    o.plan,
    o.request_payload,
    o.response_payload,
    o.created_at
FROM haetdeul.orchestrator_agent_runs o
WHERE o.agent = 'master'
ON CONFLICT (run_id) DO NOTHING;


-- ── 3. 옮긴 행 수를 확인한다 ───────────────────────────────────────────────
--
-- 옛 표의 마스터 행 수와 새 표의 행 수가 다르면 멈춘다.
-- 조용히 일부만 옮기면 결정이 가리키는 실행이 사라진다.

DO $$
DECLARE
    old_n INTEGER;
    new_n INTEGER;
BEGIN
    SELECT count(*) INTO old_n
      FROM haetdeul.orchestrator_agent_runs WHERE agent = 'master';
    SELECT count(*) INTO new_n FROM haetdeul.master_agent_runs;

    IF old_n <> new_n THEN
        RAISE EXCEPTION
            '이관 행 수가 다르다: 옛 표 master % 행, 새 표 % 행. 이 상태로 FK 를 옮기면 결정이 근거를 잃는다.',
            old_n, new_n;
    END IF;

    RAISE NOTICE '마스터 실행 % 행 이관 확인', new_n;
END $$;


-- ── 4. `master_decisions` 의 FK 를 새 표로 옮긴다 ──────────────────────────
--
-- ★ 순서가 중요하다. 새 FK 를 먼저 걸면 옛 FK 와 둘 다 살아 있는 상태가 되는데,
--   그건 무해하지만 옛 표를 나중에 못 지운다. 옛 것을 떼고 새 것을 건다.
--
-- ★ ON DELETE RESTRICT 를 유지한다. 결정이 가리키는 실행은 지울 수 없다 —
--   SET NULL 이면 실행을 지우는 순간 결정이 조용히 근거를 잃는다.
--
-- ★ MATCH SIMPLE (PostgreSQL 기본) 이라 참조 컬럼 중 하나라도 NULL 이면 검사를
--   건너뛴다. run_id 가 NULL 인 옛 결정 6행은 그대로 통과한다.

ALTER TABLE haetdeul.master_decisions
    DROP CONSTRAINT IF EXISTS master_decisions_run_fk;

ALTER TABLE haetdeul.master_decisions
    ADD CONSTRAINT master_decisions_run_fk
        FOREIGN KEY (run_id, request_id)
        REFERENCES haetdeul.master_agent_runs (run_id, request_id)
        ON DELETE RESTRICT;


-- ── 5. 마지막 확인 ─────────────────────────────────────────────────────────
--
-- FK 가 실제로 새 표를 가리키는지 본다. 이름만 같고 옛 표를 가리키면
-- 다음 배포에서 조용히 깨진다.

DO $$
DECLARE
    target TEXT;
BEGIN
    SELECT confrelid::regclass::text INTO target
      FROM pg_constraint
     WHERE conname = 'master_decisions_run_fk'
       AND conrelid = 'haetdeul.master_decisions'::regclass;

    IF target IS NULL THEN
        RAISE EXCEPTION 'master_decisions_run_fk 가 없다 - FK 재지정이 안 됐다.';
    END IF;

    IF target NOT LIKE '%master_agent_runs' THEN
        RAISE EXCEPTION 'FK 가 % 를 가리킨다 - master_agent_runs 여야 한다.', target;
    END IF;

    RAISE NOTICE 'FK 재지정 확인: master_decisions -> %', target;
END $$;

COMMIT;

-- ── 되돌리기 ───────────────────────────────────────────────────────────────
--
-- 옛 표의 마스터 행을 지우지 않았으므로 되돌리기는 FK 만 옮기면 된다.
--
--   BEGIN;
--   ALTER TABLE haetdeul.master_decisions DROP CONSTRAINT master_decisions_run_fk;
--   ALTER TABLE haetdeul.master_decisions
--       ADD CONSTRAINT master_decisions_run_fk
--           FOREIGN KEY (run_id, request_id)
--           REFERENCES haetdeul.orchestrator_agent_runs (run_id, request_id)
--           ON DELETE RESTRICT;
--   COMMIT;
--
-- 새 표는 남겨 두어도 무해하다. 코드가 안 읽으면 그냥 안 쓰이는 표다.
