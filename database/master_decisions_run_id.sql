-- 결정을 "그 실행" 에 묶는다 — master_decisions.run_id (2026-08-30)
--
-- ══════════════════════════════════════════════════════════════════════════
-- 실측 근거 (feat/master-frontend_lhs @ 0e8b4ec · DB_SCHEMA=haetdeul)
--
--   REQ-20251231-0001   실행 75행 · 결정 5건
--
--   결정 1회차 APPROVE 를 적재한 시점에 그 업무 키의 실행이 **68행** 있었다.
--   결정은 request_id 까지만 가리키므로 68 중 어느 것을 승인한 것인지 DB 가
--   모른다. 화면은 "최신 실행" 을 보여주므로, 재실행이 한 번 더 일어나면
--   **승인한 것과 다른 안이 승인된 것처럼 보인다.**
-- ══════════════════════════════════════════════════════════════════════════
--
-- ★ 새 표를 만들지 않는다. 컬럼 하나다.
--   `master_decisions` 의 결이 이미 "결정 1건 = 1행" 이고, 빠진 것은 **가리키는
--   대상의 정밀도**뿐이다. 표를 나누면 1:1 을 두 곳에 나눠 담게 된다.
--
-- ★ 원인은 8/27 DDL 주석에 이미 적혀 있었다.
--
--     "request_id 는 orchestrator_agent_runs 와 같은 값이지만 FK 를 걸지 않는다
--      — 그쪽은 재실행마다 행이 늘어 request_id 가 UNIQUE 가 아니다."
--
--   UNIQUE 가 아니라서 FK 를 못 건 것이지, **가리킬 것이 없어서가 아니었다.**
--   `run_id` 는 PK 라 UNIQUE 다. 그것을 가리키면 된다.
--
-- ★ NULL 을 허용한다 — 기존 6행을 채우지 않는다.
--   시각으로 "직전 실행" 을 골라 넣을 수는 있지만 그건 **추측이지 사실이 아니다**
--   (1회차는 후보가 68건이었다). 모르는 것은 모른다고 두는 쪽이 §1.2-10 이다.
--   **NULL 은 "실행이 없다" 가 아니라 "어느 실행인지 기록되지 않았다" 이다.**
--
-- ★ 복합 FK 로 "결정과 실행의 업무 키가 같다" 를 DB 가 보장하게 한다.
--   (run_id) 단독 FK 는 run_id 만 맞으면 통과라, 코드 실수로 **다른 업무 키의
--   실행**을 가리켜도 DB 가 안 잡는다. (run_id, request_id) 쌍으로 걸면 잡힌다.
--   트리거가 필요 없다 — run_id 가 PK 이므로 참조 대상에 UNIQUE 를 하나 얹으면
--   된다.
--
--   PostgreSQL 기본인 MATCH SIMPLE 이라 **참조 컬럼 중 하나라도 NULL 이면 검사를
--   건너뛴다.** 그래서 `run_id IS NULL` 인 기존 행이 그대로 통과한다.
--
-- ★ ON DELETE RESTRICT.
--   결정이 가리키는 실행은 지울 수 없다. SET NULL 로 두면 실행을 지우는 순간
--   **결정이 조용히 근거를 잃는다** — 감사 대상 표에서 있어서는 안 되는 일이다.
--   실행을 정말 지워야 하면 그 결정부터 사람이 처리하게 만든다.
--
-- ★ 되돌리는 법은 맨 아래에 있다. 두 번 돌려도 안전하다 (IF NOT EXISTS).

BEGIN;

-- ── 1. 참조 대상에 UNIQUE 를 얹는다 ────────────────────────────────────────
-- run_id 는 이미 PK 라 이 UNIQUE 는 **새 제약이 아니라 FK 가 참조할 수 있게
-- 만드는 선언**이다. request_id 가 NULL 인 행(부서 자체 실행)은 NULL 이 서로
-- 구별되므로 UNIQUE 를 방해하지 않는다.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'orchestrator_agent_runs_run_request_unique'
          AND conrelid = 'haetdeul.orchestrator_agent_runs'::regclass
    ) THEN
        ALTER TABLE haetdeul.orchestrator_agent_runs
            ADD CONSTRAINT orchestrator_agent_runs_run_request_unique
            UNIQUE (run_id, request_id);
    END IF;
END $$;

-- ── 2. 결정에 run_id 를 단다 ──────────────────────────────────────────────
ALTER TABLE haetdeul.master_decisions
    ADD COLUMN IF NOT EXISTS run_id UUID NULL;

-- ── 3. 복합 FK — 업무 키까지 맞아야 통과 ──────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'master_decisions_run_fk'
          AND conrelid = 'haetdeul.master_decisions'::regclass
    ) THEN
        ALTER TABLE haetdeul.master_decisions
            ADD CONSTRAINT master_decisions_run_fk
            FOREIGN KEY (run_id, request_id)
            REFERENCES haetdeul.orchestrator_agent_runs (run_id, request_id)
            ON DELETE RESTRICT;
    END IF;
END $$;

-- ── 4. "이 실행에 붙은 결정" 조회 ─────────────────────────────────────────
-- 반대 방향(request_id → 결정)은 idx_master_decisions_request_id 가 이미 있다.
CREATE INDEX IF NOT EXISTS idx_master_decisions_run_id
    ON haetdeul.master_decisions (run_id)
    WHERE run_id IS NOT NULL;

COMMENT ON COLUMN haetdeul.master_decisions.run_id IS
    '이 결정이 보고 있던 실행. NULL 은 "실행이 없다"가 아니라 "어느 실행인지 기록되지 않았다"이다 — 2026-08-30 이전 행이 그렇다.';

COMMIT;


-- ══════════════════════════════════════════════════════════════════════════
-- 확인 — 적용 후 이 셋을 돌려 본다
-- ══════════════════════════════════════════════════════════════════════════
--
-- ① 컬럼과 제약이 붙었나
--
--   SELECT column_name, data_type, is_nullable
--     FROM information_schema.columns
--    WHERE table_schema='haetdeul' AND table_name='master_decisions'
--      AND column_name='run_id';
--
--   SELECT conname, pg_get_constraintdef(oid)
--     FROM pg_constraint WHERE conname='master_decisions_run_fk';
--
-- ② 기존 행이 살아 있나 (6건 전부 run_id IS NULL 이어야 한다)
--
--   SELECT count(*) AS 전체, count(run_id) AS 실행이_붙은것
--     FROM haetdeul.master_decisions;
--
-- ③ 🔴 **엉뚱한 실행을 가리키면 거부되나** — 이게 이 마이그레이션의 요점이다
--
--   BEGIN;
--   INSERT INTO haetdeul.master_decisions
--       (decision_id, request_id, decision_seq, decision, decided_by,
--        end_code_at_decision, scenario_label, run_id)
--   SELECT gen_random_uuid(), 'REQ-20251231-0001', 9999, 'APPROVE', '검증',
--          'E1_APPROVED', '기본', r.run_id
--     FROM haetdeul.orchestrator_agent_runs r
--    WHERE r.request_id <> 'REQ-20251231-0001' AND r.request_id IS NOT NULL
--    LIMIT 1;
--   -- 기대: ERROR ... violates foreign key constraint "master_decisions_run_fk"
--   ROLLBACK;


-- ══════════════════════════════════════════════════════════════════════════
-- 되돌리기 — 데이터를 잃지 않는다 (run_id 값만 사라진다)
-- ══════════════════════════════════════════════════════════════════════════
--
--   BEGIN;
--   DROP INDEX IF EXISTS haetdeul.idx_master_decisions_run_id;
--   ALTER TABLE haetdeul.master_decisions
--       DROP CONSTRAINT IF EXISTS master_decisions_run_fk;
--   ALTER TABLE haetdeul.master_decisions
--       DROP COLUMN IF EXISTS run_id;
--   ALTER TABLE haetdeul.orchestrator_agent_runs
--       DROP CONSTRAINT IF EXISTS orchestrator_agent_runs_run_request_unique;
--   COMMIT;


-- ══════════════════════════════════════════════════════════════════════════
-- 🔴 백필은 하지 않는다 — 정말 필요하면 이것을 쓰되, 추측임을 알고 쓴다
-- ══════════════════════════════════════════════════════════════════════════
--
-- "결정 시각 직전의 실행" 으로 채우는 문장이다. **사실이 아니라 추론이다.**
-- REQ-20251231-0001 1회차는 그 시점에 후보가 68건이었다 — 68분의 1을 고르는 것이고,
-- 맞을 이유가 없다. 채우고 나면 **모른다는 사실 자체가 사라진다.**
--
-- 그래도 돌린다면 note 에 추론이라고 남긴다.
--
--   UPDATE haetdeul.master_decisions d
--      SET run_id = sub.run_id,
--          note   = coalesce(d.note || ' / ', '')
--                   || 'run_id 는 결정 시각 직전 실행으로 추정한 값이다 (2026-08-30 백필)'
--     FROM (
--       SELECT DISTINCT ON (d2.decision_id) d2.decision_id, r.run_id
--         FROM haetdeul.master_decisions d2
--         JOIN haetdeul.orchestrator_agent_runs r
--           ON r.request_id = d2.request_id AND r.created_at <= d2.created_at
--        WHERE d2.run_id IS NULL
--        ORDER BY d2.decision_id, r.created_at DESC
--     ) AS sub
--    WHERE d.decision_id = sub.decision_id;
