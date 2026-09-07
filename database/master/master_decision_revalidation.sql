-- master_decision_revalidation.sql — 최종 승인 시점 재검증의 **기록 자리** (2026-09-07)
--
-- ══════════════════════════════════════════════════════════════════════════
-- 무엇을 여는가 — 칸 둘이다. 도는 것은 다음 조각(M-4)이다.
--
--   revalidation_request_id  재검증이 도는 **새 실행**의 업무 키
--   revalidation_outcome     PASSED · CONDITIONAL · FAILED · ERROR
-- ══════════════════════════════════════════════════════════════════════════
--
-- 2026-09-04 에 판매와 확정한 것이다. 최종 승인 클릭 시점에 **그때 선택된 1안을
-- 다시 검증한다.** 지금 표에는 그 결과를 적을 칸이 없어서, 재검증이 돌기 시작하면
-- 결과가 갈 곳이 없다.
--
-- 🔴 **`decision` 에 값을 더하지 않는다.**
--
--     decision                 사용자가 무엇을 눌렀나        APPROVE · REJECT_ALL · REQUEST_CHANGE · CANCEL
--     revalidation_outcome     그 뒤 재검증이 어떻게 됐나    PASSED · CONDITIONAL · FAILED · ERROR
--
--   *"사용자가 APPROVE 를 눌렀는데 재검증에서 막혔다"* 와 *"사용자가 승인하지
--   않았다"* 는 **다른 사건**이다. 한 어휘에 담으면 앞엣것이 사라진다 — 이력에는
--   "승인 안 함" 만 남고 **승인하려 했다는 사실**이 지워진다.
--
--   섞으면 번복 규칙까지 흔들린다. `decision_seq` 최대가 유효하다는 규칙과
--   `mark_current` 가 전부 `decision` 값으로 판단하는데, 거기에 "사용자 의도가
--   아닌 값" 이 끼면 *"현재 유효한 결정"* 이 사람이 안 누른 것을 가리킬 수 있다.
--
-- 🔴 **`follow_up_request_id` 를 재사용하지 않는다.**
--
--     follow_up_request_id     REQUEST_CHANGE 가 낳은 재실행 (조건부 재요청 체인)
--     revalidation_request_id  APPROVE 직전 재검증이 낳은 재실행
--
--   둘 다 "이 결정이 낳은 다른 실행" 이지만 **가리키는 방향이 반대다.** 후속
--   재요청은 결정 **뒤에** 사람이 다시 돌린 것이고, 재검증은 결정을 **적기 전에**
--   서버가 돌린 것이다. 게다가 재검증은 **새 `as_of` · 새 `request_id`** 로 도는
--   반면 승인 자체는 원 실행(`run_id`)에 달려 있다 — 한 칸에 담으면 그 값이 어느
--   쪽인지 DB 도 코드도 모른다. 이 표는 이미 같은 이유로 `run_id` 를 따로 냈다
--   (`master_decisions_run_id.sql`).
--
-- ★ 새 표를 만들지 않는다 — 컬럼 둘이다.
--   재검증은 **결정 1건에 붙는 1:1 사실**이다. 한 결정에 재검증이 여럿 붙지
--   않는다 (붙는다면 그것은 새 결정 회차다). 1:1 을 표로 나누면 조회마다 조인이
--   붙고, 무엇보다 **결정은 있는데 재검증 행이 없는 상태**가 두 가지 뜻
--   ("안 했다" / "행을 못 넣었다")을 갖게 된다. `master_decisions.sql` 이
--   `run_id` 때 한 판단과 같다.
--
-- ★ 둘 다 NULL 을 허용한다 — 기존 30행을 채우지 않는다.
--
--   실측 (2026-09-06 · `master_decision_cancel.sql` 헤더):
--     decision 분포   APPROVE 26 · REQUEST_CHANGE 4
--
--   **그 26건은 재검증을 안 거쳤다.** 그때는 재검증이 없었다. 없던 검사를
--   'PASSED' 로 채우면 **추측이 사실로 둔갑한다** — 나중에 "재검증을 통과한
--   승인" 을 세면 26이 딸려 나오고, 그 26은 아무도 검증하지 않은 것이다.
--
--   🔴 **NULL 은 "재검증에 실패했다" 가 아니라 "재검증을 하지 않았다" 이다.**
--   `run_id` 의 NULL 과 같은 결이다.
--
-- ★ FK 를 걸지 않는다 — 걸 수가 없다.
--   `revalidation_request_id` 는 업무 키(TEXT)이고, `orchestrator_agent_runs` ·
--   `master_agent_runs` 양쪽 다 **재실행마다 행이 늘어 `request_id` 가 UNIQUE 가
--   아니다.** 본 DDL 주석이 2026-08-27 에 적어 둔 그대로다. `run_id` 처럼 PK 를
--   가리키게 만들 수도 있었지만, 재검증 실행은 **아직 존재하지 않는다** — M-4 가
--   어느 표에 어떤 모양으로 남길지 정하지 않은 상태에서 FK 를 박으면 그 결정을
--   DDL 이 대신 내리는 것이 된다. 업무 키만 들고, 실행 표가 정해지면 그때 `run_id`
--   처럼 정밀도를 올린다.
--
-- ★ 두 번 돌려도 안전하다 (`IF NOT EXISTS` · `pg_constraint` 조회).
--   되돌리는 법은 맨 아래에 있다.

BEGIN;

SET LOCAL search_path TO haetdeul, public;

-- ── 1. 칸 둘 ──────────────────────────────────────────────────────────────
ALTER TABLE haetdeul.master_decisions
    ADD COLUMN IF NOT EXISTS revalidation_request_id TEXT NULL;

ALTER TABLE haetdeul.master_decisions
    ADD COLUMN IF NOT EXISTS revalidation_outcome TEXT NULL;

-- ── 2. 결과 어휘를 닫는다 ─────────────────────────────────────────────────
--
-- `decision` 이 CHECK 로 어휘를 닫아 둔 것과 같은 결이다. 어휘가 안 닫히면
-- 부르는 쪽이 'ok' · 'pass' · 'PASS' 를 섞어 넣고, 세는 쪽은 그것을 모른다.
--
-- ★ 네 값의 뜻 (2026-09-04 · 마스터가 정해 판매에 통보 · 판매 수용)
--
--     PASSED       재검증 통과. 승인 기록 · Write 진행
--     CONDITIONAL  통과했으나 **새 조건이 붙었다** → 승인 기록 안 함
--     FAILED       재검증에서 막혔다              → 승인 기록 안 함
--     ERROR        재검증 자체를 못 돌렸다        → 승인 기록 안 함
--
-- 🔴 **`CONDITIONAL` 은 통과가 아니다.** 사용자가 승인한 대상은 **그때 화면에
--   있던 그 안**이다. 새 조건이 붙으면 그것은 다른 안이라, 통과로 접으면
--   *사용자가 본 적 없는 조건이 사용자 승인으로 기록된다.* 그래서 `PASSED` 와
--   따로 둔다 — 둘을 한 값으로 만들면 이 구분이 DB 에서 사라진다.
--
-- ⚠️ NULL 은 CHECK 를 그냥 통과한다 (`NULL IN (...)` 은 NULL 이고 CHECK 는 NULL 을
--   위반으로 보지 않는다). 그것이 의도다 — "재검증 안 함" 은 어휘 밖의 상태다.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'master_decisions_revalidation_outcome_check'
           AND conrelid = 'haetdeul.master_decisions'::regclass
    ) THEN
        ALTER TABLE haetdeul.master_decisions
            ADD CONSTRAINT master_decisions_revalidation_outcome_check
            CHECK (revalidation_outcome IN ('PASSED', 'CONDITIONAL', 'FAILED', 'ERROR'));
    END IF;
END $$;

-- ── 3. 두 칸의 관계를 잠근다 ──────────────────────────────────────────────
--
-- 🔴 **결과만 있고 가리키는 실행이 없으면 *"재검증했는데 어느 실행인지 모른다"*
--   가 된다.** `run_id` 가 없던 시절 승인이 겪은 것과 같은 병이다 — 그때는 실행이
--   68건 중 무엇인지 몰랐고, 여기서는 재검증 실행 자체를 못 찾는다. 이 표는 이미
--   `decision`↔`scenario_label` 관계를 CHECK 로 잠그고 있으므로(`master_decisions
--   _scenario_required`) 같은 근거가 선다. 건다.
--
-- ⚠️ **`ERROR` 만 예외다.** `ERROR` 는 *"재검증 자체를 못 돌렸다"* 이므로
--   (`RUNTIME_NOT_READY` 등) **가리킬 실행이 아예 없을 수 있다.** 여기서 예외를
--   안 두면 M-4 는 못 돌린 재검증에 가짜 업무 키를 지어 넣어야 하고, 그 순간
--   *"실행이 있었다"* 가 사실이 아닌 채로 남는다 (§1.2-10).
--
-- ★ 반대 방향도 같이 잠근다 — **키만 있고 결과가 NULL 인 행을 막는다.**
--   허용하면 NULL 의 뜻이 둘이 된다: "재검증 안 함" 과 "걸어 놓고 결과를 못 적음".
--   위에서 *"NULL 은 재검증을 하지 않았다"* 로 못 박았으므로, 그 뜻을 하나로
--   유지하려면 여기서 막아야 한다. **재검증은 승인 클릭 안에서 동기로 끝나고
--   결정 행은 그 뒤 한 번의 INSERT 로 들어가므로**(이 표는 append-only) 중간
--   상태가 생기지 않는다 — 막아도 M-4 가 좁아지지 않는다.
--
-- 🔴 **`decision` 값과는 묶지 않는다.** "재검증 칸은 APPROVE 행에만" 을 걸고 싶어
--   지지만, `FAILED` 는 *"승인 기록 안 함, 실패 이력은 남긴다"* 라 그 이력 행의
--   `decision` 이 무엇이 될지 **아직 안 정해졌다** (M-4 몫이다). 지금 묶으면 DDL 이
--   그 결정을 대신 내려 버린다. 정해지면 그때 좁힌다.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'master_decisions_revalidation_pairing'
           AND conrelid = 'haetdeul.master_decisions'::regclass
    ) THEN
        ALTER TABLE haetdeul.master_decisions
            ADD CONSTRAINT master_decisions_revalidation_pairing
            CHECK (
                   (revalidation_request_id IS     NULL AND revalidation_outcome IS NULL)
                OR (revalidation_request_id IS NOT NULL AND revalidation_outcome IS NOT NULL)
                OR (revalidation_request_id IS     NULL AND revalidation_outcome = 'ERROR')
            );
    END IF;
END $$;

-- ── 4. "이 재검증 실행에 붙은 결정" 조회 ──────────────────────────────────
-- `idx_master_decisions_run_id` 와 같은 이유·같은 모양이다. NULL 이 대부분이므로
-- 부분 인덱스로 둔다 — 기존 30행은 전부 NULL 이라 인덱스에 안 들어간다.
CREATE INDEX IF NOT EXISTS idx_master_decisions_revalidation_request_id
    ON haetdeul.master_decisions (revalidation_request_id)
    WHERE revalidation_request_id IS NOT NULL;

COMMENT ON COLUMN haetdeul.master_decisions.revalidation_request_id IS
    '최종 승인 시점 재검증이 돈 새 실행의 업무 키. 새 as_of · 새 업무 키로 돈다 — 조건부 재요청 체인과 다른 사건이라 칸을 따로 둔다. NULL 은 재검증을 하지 않았다는 뜻이다.';
COMMENT ON COLUMN haetdeul.master_decisions.revalidation_outcome IS
    'PASSED(통과) · CONDITIONAL(통과했으나 새 조건이 붙어 승인 기록 안 함) · FAILED(막힘) · ERROR(못 돌림). CONDITIONAL 은 통과가 아니다. NULL 은 재검증을 하지 않았다는 뜻이지 실패가 아니다.';

COMMIT;


-- ══════════════════════════════════════════════════════════════════════════
-- 확인 — 적용 후 이 넷을 돌려 본다
-- ══════════════════════════════════════════════════════════════════════════
--
-- ① 칸과 제약이 붙었나
--
--   SELECT column_name, data_type, is_nullable
--     FROM information_schema.columns
--    WHERE table_schema='haetdeul' AND table_name='master_decisions'
--      AND column_name LIKE 'revalidation%';
--   -- 기대: 두 행 모두 is_nullable = YES
--
--   SELECT conname, pg_get_constraintdef(oid)
--     FROM pg_constraint
--    WHERE conrelid='haetdeul.master_decisions'::regclass
--      AND conname LIKE '%revalidation%';
--
-- ② 기존 행이 살아 있나 (30건 전부 두 칸이 NULL 이어야 한다)
--
--   SELECT count(*) AS 전체,
--          count(revalidation_outcome)   AS 재검증_결과가_있는것,
--          count(revalidation_request_id) AS 재검증_실행이_붙은것
--     FROM haetdeul.master_decisions;
--   -- 기대: 30 · 0 · 0
--
-- ③ 🔴 **어휘 밖의 값이 막히나**
--
--   BEGIN;
--   UPDATE haetdeul.master_decisions
--      SET revalidation_request_id='REQ-PROBE', revalidation_outcome='PASS'
--    WHERE decision_id = (SELECT decision_id FROM haetdeul.master_decisions LIMIT 1);
--   -- 기대: ERROR ... "master_decisions_revalidation_outcome_check"
--   ROLLBACK;
--
-- ④ 🔴 **결과만 있고 실행이 없는 행이 막히나** — 이게 이 판의 요점이다
--
--   BEGIN;
--   UPDATE haetdeul.master_decisions
--      SET revalidation_outcome='PASSED', revalidation_request_id=NULL
--    WHERE decision_id = (SELECT decision_id FROM haetdeul.master_decisions LIMIT 1);
--   -- 기대: ERROR ... "master_decisions_revalidation_pairing"
--   ROLLBACK;
--
--   -- 같은 자리에서 ERROR 는 통과해야 한다 (못 돌린 재검증에는 실행이 없다)
--   BEGIN;
--   UPDATE haetdeul.master_decisions
--      SET revalidation_outcome='ERROR', revalidation_request_id=NULL
--    WHERE decision_id = (SELECT decision_id FROM haetdeul.master_decisions LIMIT 1);
--   -- 기대: UPDATE 1
--   ROLLBACK;


-- ══════════════════════════════════════════════════════════════════════════
-- 되돌리기 — 재검증 값만 사라진다. 결정 이력은 그대로다
-- ══════════════════════════════════════════════════════════════════════════
--
--   BEGIN;
--   DROP INDEX IF EXISTS haetdeul.idx_master_decisions_revalidation_request_id;
--   ALTER TABLE haetdeul.master_decisions
--       DROP CONSTRAINT IF EXISTS master_decisions_revalidation_pairing;
--   ALTER TABLE haetdeul.master_decisions
--       DROP CONSTRAINT IF EXISTS master_decisions_revalidation_outcome_check;
--   ALTER TABLE haetdeul.master_decisions
--       DROP COLUMN IF EXISTS revalidation_outcome;
--   ALTER TABLE haetdeul.master_decisions
--       DROP COLUMN IF EXISTS revalidation_request_id;
--   COMMIT;


-- ══════════════════════════════════════════════════════════════════════════
-- 🔴 백필은 없다 — 쓸 문장조차 적어 두지 않는다
-- ══════════════════════════════════════════════════════════════════════════
--
-- `master_decisions_run_id.sql` 은 "정말 필요하면 이것을 쓰되 추측임을 알고
-- 쓰라" 며 UPDATE 문을 남겼다. **여기서는 남기지 않는다.** 거기서는 채울 대상이
-- 적어도 **존재했다** (실행 68건 중 하나였다). 여기서 채울 대상은 **존재하지
-- 않는다** — 2026-09-07 이전에는 재검증이라는 절차 자체가 없었다.
--
-- 없는 사건에 결과를 적는 문장은 추측이 아니라 **창작**이다. 손이 미끄러질 자리를
-- 아예 두지 않는다.
