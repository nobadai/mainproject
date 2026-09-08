-- 판단 기록에 실행 축을 세운다 — master_agent_runs.sim_run_id (2026-09-08)
--
-- ══════════════════════════════════════════════════════════════════════════
-- 실측 근거 (dev @ 92b7ac6 · DB_SCHEMA=haetdeul)
--
--   sim_run_id 를 가진 표      22개
--     inventory_lots · logistics_runtime_fixture · purchases · payables ·
--     receivables · sales · finance_states · master_day_openings ·
--     proposals · forecasts · …
--
--   🔴 master_agent_runs        칸이 없다
-- ══════════════════════════════════════════════════════════════════════════
--
-- 축이 22곳에 있는데 **판단 기록에만 없다.** 그래서 E2E 검증으로 만든 상태와
-- 장기 걷기 상태가 한 통에 섞이고, 판단 행을 실행별로 못 가른다. 판매·재무가
-- "⑨ E2E 검증용 fixture 와 장기 Simulation 상태 분리" 로 올린 항목이 이것이다.
--
-- ★ 새 표를 만들지 않는다. 컬럼 하나다.
--   `master_agent_runs` 의 결이 이미 "실행 1건 = 1행" 이고, 빠진 것은 **그 실행이
--   어느 장부 위에서 돌았는가** 하나뿐이다.
--
-- ★ TEXT 다 — 다른 22개 표와 같은 타입이다. 새 어휘를 짓지 않는다.
--
-- ★ NULL 을 허용한다. NOT NULL 로 만들면 기존 1,206행이 막힌다.
--
-- ★ 🔴 **기본값을 넣지 않는다. 기존 행을 채우지 않는다.**
--   `BURN_IN_SIM_RUN_ID` 로 채우면 *"확인하지 않은 것을 안다고 적는"* 것이 된다.
--   그 1,206행이 어느 실행이었는지 아무도 확인하지 않았다.
--   **없는 것과 모르는 것은 다르다** — NULL 은 "실행이 없다" 가 아니라
--   "어느 실행인지 기록되지 않았다" 이다.
--
-- ★ 🔴 **이 마이그레이션은 칸을 세울 뿐, 검증 상태와 장기 상태를 가르지 않는다.**
--   가르는 것은 새 sim_run_id 로 다시 걷는 별건이다.
--
-- ★ FK 를 걸지 않는다. `sim_run_id` 를 소유한 표가 없다 — 22개 표가 저마다 값으로
--   들고 있는 축이고, 가리킬 부모 행이 어디에도 없다. 없는 부모를 만들어 걸면
--   그 표의 주인이 마스터가 되어 버린다.
--
-- ★ 되돌리는 법은 맨 아래에 있다. 두 번 돌려도 안전하다 (IF NOT EXISTS).
--
-- 🔴 **적용 순서** — `#353` 에서 "마이그레이션 없이 코드만 머지" 로 승인 32건이
--   전부 죽은 적이 있다.
--     ㄱ 버릴 수 있는 Docker 로 먼저 검증
--     ㄴ 공유 DB 에 적용 (적용 전/후 행 수가 같은지 확인)
--     ㄷ 그 다음에 코드

BEGIN;

-- ── 1. 칸 ────────────────────────────────────────────────────────────────
ALTER TABLE haetdeul.master_agent_runs
    ADD COLUMN IF NOT EXISTS sim_run_id TEXT NULL;

-- ── 2. "그 실행의 그 날" 조회 ────────────────────────────────────────────
-- 이 칸이 생기면 늘 따라오는 물음이 `(sim_run_id, as_of)` 다 — 장기 걷기가 며칠째
-- 어떤 판단을 했는가. 값이 없는 행은 색인에 넣지 않는다: 지금 1,206행 전부가
-- NULL 이라 부분 색인이 아니면 색인의 대부분이 NULL 이다.
CREATE INDEX IF NOT EXISTS idx_master_agent_runs_sim_run_as_of
    ON haetdeul.master_agent_runs (sim_run_id, as_of)
    WHERE sim_run_id IS NOT NULL;

COMMENT ON COLUMN haetdeul.master_agent_runs.sim_run_id IS
    '이 실행이 어느 장부 위에서 돌았나. NULL 은 "실행이 없다"가 아니라 "어느 실행인지 기록되지 않았다"이다 — 2026-09-08 이전 행이 그렇다. 빈 문자열은 들어오지 않는다: 코드가 NULL 로 접는다.';

COMMIT;


-- ══════════════════════════════════════════════════════════════════════════
-- 확인 — 적용 후 이 셋을 돌려 본다
-- ══════════════════════════════════════════════════════════════════════════
--
-- ① 컬럼이 붙었나 (is_nullable 이 YES 여야 한다)
--
--   SELECT column_name, data_type, is_nullable, column_default
--     FROM information_schema.columns
--    WHERE table_schema='haetdeul' AND table_name='master_agent_runs'
--      AND column_name='sim_run_id';
--
-- ② 기존 행이 살아 있나 — 적용 전 행 수와 같아야 하고, 전부 NULL 이어야 한다
--
--   SELECT count(*) AS 전체, count(sim_run_id) AS 축이_붙은것
--     FROM haetdeul.master_agent_runs;
--
-- ③ 색인이 붙었나
--
--   SELECT indexname FROM pg_indexes
--    WHERE schemaname='haetdeul' AND tablename='master_agent_runs';


-- ══════════════════════════════════════════════════════════════════════════
-- 되돌리기 — 데이터를 잃지 않는다 (sim_run_id 값만 사라진다)
-- ══════════════════════════════════════════════════════════════════════════
--
--   BEGIN;
--   DROP INDEX IF EXISTS haetdeul.idx_master_agent_runs_sim_run_as_of;
--   ALTER TABLE haetdeul.master_agent_runs DROP COLUMN IF EXISTS sim_run_id;
--   COMMIT;


-- ══════════════════════════════════════════════════════════════════════════
-- 🔴 백필은 하지 않는다
-- ══════════════════════════════════════════════════════════════════════════
--
-- "그 시기에 걷던 실행이었을 것" 으로 채우는 문장을 여기 적어 두지 않는다.
-- 적어 두면 언젠가 누가 돌린다. 기존 1,206행이 어느 실행이었는지는 **확인되지
-- 않았고**, 채우고 나면 모른다는 사실 자체가 사라진다.
--
-- 검증 상태와 장기 상태를 실제로 가르는 길은 하나다 — **새 sim_run_id 로 다시
-- 걷는다.** 그때 생기는 행은 처음부터 자기 축을 갖는다.
