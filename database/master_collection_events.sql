-- 수금 사건을 놓을 자리 — master_collection_events (2026-09-08)
--
-- ══════════════════════════════════════════════════════════════════════════
-- 실측 근거 (feat/master-wire-collection_lhs @ dbf52d4 · DB_SCHEMA=haetdeul)
--
--   haetdeul.receivables   15행
--     COLLECTED  6   원금 19,010,294   미수          0
--     PARTIAL    2   원금  5,896,967   미수  2,948,483
--     OPEN       7   원금 18,974,071   미수 18,974,071
--     due_date 범위  2026-01-02 ~ 2026-01-30
--
--   🟢 **얼마** 들어왔나   receivables.received_amount_krw 에 있다 (사실)
--   🔴 **언제** 들어왔나   어디에도 없다 — receivables 에 수금일 칸이 없다
--   🔴 수금 사건 표        **없다** (%collect% · %cash% · %payment% 전수 0건)
-- ══════════════════════════════════════════════════════════════════════════
--
-- ★ 그래서 이 파일이 하는 일은 **사건을 파생하는 것이 아니라** 「없는 칸(수금일)을
--   어디에 둘지 정하고 그 자리를 만드는 것」이다.
--
-- 🔴 **`receivables` 에 칸을 더하지 않는다.**
--   ① 그 표는 재무 소유의 채권 원장이고,
--   ② `PARTIAL` 이 실제로 2건 있다 — **한 채권이 여러 번 나눠 들어온다.**
--      수금일 한 칸으로는 못 적는다. 1:N 은 표를 나누는 자리다
--      (`master_decisions.sql` 이 같은 이유로 실행 표에 안 들어갔다).
--
-- ★ **금액의 주인은 여전히 재무다.** 이 표는 *"어느 날 그 채권의 누적 수금액이
--   얼마가 됐다고 두기로 했나"* 만 적는다. 실제 잔액·상태는
--   `receivables` · `finance_states` 가 갖는다 — 여기에 복사해 두지 않는다.
--
-- 🔴 **이 표는 비어서 나간다.** 자리를 만들 뿐 사건을 만들지 않는다.
--   무엇을 사실로 둘지는 팀 결정이고, 재무가 *"due_date 경과를 수금으로 읽지
--   않는다"* 로 그은 선이 그 이유다. **낸 것과 도는 것은 다르다.**
--
-- ★ **판을 나누지 않는다.** 새 표라 신규 구축과 이관이 같은 문장이다
--   (`30_logistics_wms_schema.sql` 이 판을 안 나눈 것과 같은 사정이고, 여기는
--   더 단순하다 — ALTER 가 한 줄도 없다). 두 번 돌려도 안전하다.

BEGIN;

CREATE TABLE IF NOT EXISTS haetdeul.master_collection_events (
    -- 정본 축. 재무 축 (sim_run_id, as_of, financing_mode) 의 앞뒤 둘이다.
    -- 🔴 `financing_mode` 를 빼면 안 된다 — 실측으로 `finance_states` 에
    --   LOAN_BASELINE 252행과 BASE_NO_LOAN 2행이 **공존한다.** 축이 없으면
    --   무차입 장부의 수금이 대출 baseline 장부에 조용히 들어간다.
    sim_run_id                 TEXT      NOT NULL,
    financing_mode             TEXT      NOT NULL,

    -- **달력일이다.** 입금은 토요일에도 찍힌다 — 실행일 달력이 아니다.
    collection_date            DATE      NOT NULL,

    -- haetdeul.receivables.receivable_id 를 가리킨다.
    receivable_id              TEXT      NOT NULL,

    -- 🔴 **누적(cumulative) target 이다. 증분이 아니다.**
    --
    --   그날 얼마가 더 들어왔나가 아니라 **그날까지 통틀어 얼마가 들어온 것으로
    --   두는가**이다. 재무 `build_collection_transition` 이
    --   `target - received_amount_krw` 를 delta 로 잡고,
    --     target < received_amount_krw   → "cumulative collection cannot regress"
    --     target > original_amount_krw   → "cumulative collection cannot exceed ..."
    --   로 막는다.
    --
    --   ⚠️ 증분으로 오해해 적으면 그 검사가 **엉뚱한 곳에서** 터진다. 두 번째
    --     분할 수금을 증분으로 적는 순간 target 이 직전 누적보다 작아져
    --     "역행" 으로 거부되고, 화면에는 수금이 막혔다는 말만 남는다.
    --
    --   ★ 누적이라서 **멱등하다.** 같은 행을 두 번 반영해도 delta 가 0 이다.
    target_received_total_krw  NUMERIC   NOT NULL,

    -- 🔴 **장식이 아니다.** *"이 사건을 왜 사실로 두었나"* 를 적는 자리다.
    --
    --   `app/master/inputs.py` 가 파생분에 파생식을 note 로 실어 내보내는 것과
    --   같은 규율이다 — **값만 넘기면 사람도 매입도 그것을 확정 사실로 읽는다.**
    --   입금표 번호인지, 시연용으로 손으로 넣은 것인지, 어느 회의에서 그렇게
    --   두기로 했는지가 여기 없으면 나중에 **아무도 근거를 되찾을 수 없다.**
    note                       TEXT      NULL,

    -- 한 축의 한 날에 한 채권은 한 번이다. 같은 날 두 줄이면 어느 것이 누적
    -- target 인지 DB 가 모른다.
    CONSTRAINT master_collection_events_pkey
        PRIMARY KEY (sim_run_id, financing_mode, collection_date, receivable_id)
);

COMMENT ON TABLE haetdeul.master_collection_events IS
    '수금 사건 — 어느 날 그 채권의 누적 수금액을 얼마로 두기로 했나. receivables 에 수금일 칸이 없어 만든 표이고, 금액·상태의 정본은 여전히 receivables·finance_states 다.';

COMMENT ON COLUMN haetdeul.master_collection_events.sim_run_id IS
    '어느 실행의 장부인가. 마스터가 정하는 값이다 (BURN_IN_SIM_RUN_ID).';

COMMENT ON COLUMN haetdeul.master_collection_events.financing_mode IS
    '재무 축의 값이다. 마스터가 고르지 않는다 — LOAN_BASELINE 과 BASE_NO_LOAN 이 실제로 공존한다.';

COMMENT ON COLUMN haetdeul.master_collection_events.collection_date IS
    '수금일. 달력일이다 (토·일·공휴일 포함) — 입금은 시장이 안 서는 날에도 찍힌다.';

COMMENT ON COLUMN haetdeul.master_collection_events.receivable_id IS
    'haetdeul.receivables 의 채권. 한 채권이 여러 날에 걸쳐 나눠 들어올 수 있어 이 표가 1:N 이다.';

COMMENT ON COLUMN haetdeul.master_collection_events.target_received_total_krw IS
    '🔴 누적 target 이다. 증분이 아니다. 그날까지 통틀어 이 채권에 들어온 것으로 두는 총액이고, 재무가 (target - received_amount_krw) 를 delta 로 반영한다. 증분으로 적으면 두 번째 분할 수금이 역행으로 거부된다.';

COMMENT ON COLUMN haetdeul.master_collection_events.note IS
    '🔴 이 사건을 왜 사실로 두었나. 입금 근거·출처·결정한 자리를 적는다. 비워 두면 값만 남고, 값만 남으면 사람도 매입 판단도 그것을 확정 사실로 읽는다.';

COMMIT;


-- ══════════════════════════════════════════════════════════════════════════
-- 확인 — 적용 후 이 셋을 돌려 본다
-- ══════════════════════════════════════════════════════════════════════════
--
-- ① 표와 PK 가 섰나
--
--   SELECT column_name, data_type, is_nullable
--     FROM information_schema.columns
--    WHERE table_schema='haetdeul' AND table_name='master_collection_events'
--    ORDER BY ordinal_position;
--
--   SELECT conname, pg_get_constraintdef(oid)
--     FROM pg_constraint
--    WHERE conrelid='haetdeul.master_collection_events'::regclass;
--
-- ② 🔴 **비어 있나** — 이 판은 자리를 만들 뿐 사건을 만들지 않는다
--
--   SELECT count(*) FROM haetdeul.master_collection_events;   -- 기대: 0
--
-- ③ 🔴 **채권 원장이 안 바뀌었나** — 새 표를 만드는 것뿐이라 그대로여야 한다
--
--   SELECT count(*) FROM haetdeul.receivables;                -- 기대: 15 (2026-09-08 실측)


-- ══════════════════════════════════════════════════════════════════════════
-- 되돌리기
-- ══════════════════════════════════════════════════════════════════════════
--
--   DROP TABLE IF EXISTS haetdeul.master_collection_events;
--
-- ⚠️ 행이 들어간 뒤에 지우면 **왜 그 수금을 사실로 두었는지(note)가 같이 사라진다.**
--   receivables 의 금액은 남고 근거만 없어지는 상태가 된다.


-- ══════════════════════════════════════════════════════════════════════════
-- 🔴 시드는 없다 — 없는 것이 이 파일의 내용이다
-- ══════════════════════════════════════════════════════════════════════════
--
-- 아래 셋은 **하면 안 되는 것**이고, 적어 두는 이유는 셋 다 그럴듯해 보이기
-- 때문이다.
--
--   ❌ due_date 가 지났으니 자동 수금
--   ❌ 미수금 잔액이 있으니 자동 수금
--   ❌ 결제기일 = 실제 입금일로 간주
--
-- 셋 다 **실제로 들어오지 않은 돈을 만드는 것**이고, 그 현금으로 다음 매입 판단이
-- 돈다. 재무가 그은 선이 그것이다 (`app/master/collection.py` · 재무 §2.4).
--
-- 시연이 필요하면 사람이 한 줄 넣는다. **note 를 채워서** 넣는다.
--
--   INSERT INTO haetdeul.master_collection_events
--       (sim_run_id, financing_mode, collection_date, receivable_id,
--        target_received_total_krw, note)
--   VALUES ('SIM-BURNIN-202512', 'LOAN_BASELINE', DATE '2026-01-10', '<채권ID>',
--           1234567.000000, '시연용 수기 입력 — 실제 입금 근거 아님 (2026-09-08)');
