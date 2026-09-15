-- master_purchase_records.sql — 실매입 기록. **마스터가 소유한다.**
--
-- 설계: 04 팀공유/260915_설계_실매입기록_단계_추가_안A_변경명세.md §4-1 · §4-6 ③
--
-- 🔴 사람이 매입안을 승인하면 선정만 적힌다. 사람이 **실제로 산 값**을 이 표에 적는 순간
--    그 값으로 기존 전이(purchases · payables · inbound_schedules)가 돈다.
--    자동 승인(decided_by = 'AUTO-BACKFILL')은 이 표를 쓰지 않는다 — 지금처럼 승인 즉시.
--
-- ★ 한 승인에 한 번이다 (PK). 고쳐 쓰기 없음 · 다시 기록하면 PK 가 막고 API 가 409.
-- ★ PK 에 실행 축(sim_run_id)이 있다 — 다른 실행의 같은 업무 키는 별개 기록이다
--   (9/15 재무 회신 ③).
-- 🟢 기존 표 변경 없음 · 기존 행 무영향.
--
-- ⚠️ 이 파일은 **적용 전에 사용자 확인**을 받는다. 적용 전에는 백엔드 PR 을 머지하지 않는다
--    (재시도 · 약정 재조립이 이 표를 읽는다).
--
-- ── 적용 전 확인 ─────────────────────────────────────────────────────────
--   표가 아직 없어야 한다 (0 행).
--
--   SELECT count(*) FROM information_schema.tables
--    WHERE table_schema = 'haetdeul' AND table_name = 'master_purchase_records';
--
-- ── 되돌리기 ─────────────────────────────────────────────────────────────
--   신설 표 하나라 DROP 으로 되돌린다. 기록이 쌓인 뒤라면 그 기록도 함께 사라진다.
--
--   DROP TABLE haetdeul.master_purchase_records;

BEGIN;

SET LOCAL search_path TO haetdeul, public;

CREATE TABLE haetdeul.master_purchase_records (
    sim_run_id       text        NOT NULL,
    request_id       text        NOT NULL,
    decision_seq     integer     NOT NULL,     -- 어떤 승인에 대한 기록인가
    leg_seq          integer     NOT NULL,     -- arrival_schedule[].seq
    quantity_kg      numeric     NOT NULL CHECK (quantity_kg > 0),
    amount_krw       numeric     NOT NULL CHECK (amount_krw > 0),
    purchase_date    date        NOT NULL,
    arrival_date     date        NOT NULL CHECK (arrival_date >= purchase_date),
    grade            text        NOT NULL,
    recorded_by      text        NOT NULL,
    recorded_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (sim_run_id, request_id, decision_seq, leg_seq)
);

COMMENT ON TABLE haetdeul.master_purchase_records IS
    '실매입 기록. 마스터 소유. 사람 승인 1건에 회차마다 1행 · 이 값으로 매입 원장 · 채무 · 입고 일정 전이가 선다.';
COMMENT ON COLUMN haetdeul.master_purchase_records.decision_seq IS
    '어떤 승인에 대한 기록인가 (master_decisions.decision_seq).';
COMMENT ON COLUMN haetdeul.master_purchase_records.leg_seq IS
    '선정안 회차 (arrival_schedule[].seq). 회차 수와 seq 는 선정안 그대로다.';

-- 만든 대로 동작하는지 그 자리에서 확인한다 (롤백하는 삽입으로 잰다).
DO $$
BEGIN
    INSERT INTO haetdeul.master_purchase_records
        (sim_run_id, request_id, decision_seq, leg_seq, quantity_kg, amount_krw,
         purchase_date, arrival_date, grade, recorded_by)
    VALUES ('MIGRATION-PROBE', 'MIGRATION-PROBE', 1, 1, 10, 1000,
            '1900-01-01', '1900-01-02', '특', 'migration');

    -- 같은 승인 · 같은 회차를 다시 적으면 막혀야 한다.
    BEGIN
        INSERT INTO haetdeul.master_purchase_records
            (sim_run_id, request_id, decision_seq, leg_seq, quantity_kg, amount_krw,
             purchase_date, arrival_date, grade, recorded_by)
        VALUES ('MIGRATION-PROBE', 'MIGRATION-PROBE', 1, 1, 10, 1000,
                '1900-01-01', '1900-01-02', '특', 'migration');
        RAISE EXCEPTION 'PK 가 안 걸린다 — 같은 승인에 기록이 두 번 선다';
    EXCEPTION WHEN unique_violation THEN
        NULL;  -- 기대한 대로 막혔다
    END;

    -- 다른 실행 축의 같은 업무 키는 별개다.
    INSERT INTO haetdeul.master_purchase_records
        (sim_run_id, request_id, decision_seq, leg_seq, quantity_kg, amount_krw,
         purchase_date, arrival_date, grade, recorded_by)
    VALUES ('MIGRATION-PROBE-2', 'MIGRATION-PROBE', 1, 1, 10, 1000,
            '1900-01-01', '1900-01-02', '특', 'migration');

    -- 도착일이 매입일보다 앞서면 막혀야 한다.
    BEGIN
        INSERT INTO haetdeul.master_purchase_records
            (sim_run_id, request_id, decision_seq, leg_seq, quantity_kg, amount_krw,
             purchase_date, arrival_date, grade, recorded_by)
        VALUES ('MIGRATION-PROBE', 'MIGRATION-PROBE', 1, 2, 10, 1000,
                '1900-01-02', '1900-01-01', '특', 'migration');
        RAISE EXCEPTION 'arrival_date CHECK 가 안 걸린다';
    EXCEPTION WHEN check_violation THEN
        NULL;
    END;

    -- 수량 0 이면 막혀야 한다.
    BEGIN
        INSERT INTO haetdeul.master_purchase_records
            (sim_run_id, request_id, decision_seq, leg_seq, quantity_kg, amount_krw,
             purchase_date, arrival_date, grade, recorded_by)
        VALUES ('MIGRATION-PROBE', 'MIGRATION-PROBE', 1, 3, 0, 1000,
                '1900-01-01', '1900-01-02', '특', 'migration');
        RAISE EXCEPTION 'quantity_kg CHECK 가 안 걸린다';
    EXCEPTION WHEN check_violation THEN
        NULL;
    END;

    DELETE FROM haetdeul.master_purchase_records WHERE request_id = 'MIGRATION-PROBE';
END $$;

COMMIT;

-- ── 적용 후 확인 ─────────────────────────────────────────────────────────
--   ① 표가 있다 (1 행) · 행이 비어 있다 (0)
--
--   SELECT count(*) FROM information_schema.tables
--    WHERE table_schema = 'haetdeul' AND table_name = 'master_purchase_records';
--   SELECT count(*) FROM haetdeul.master_purchase_records;
--
--   ② PK 가 실행 축을 포함한다
--
--   SELECT pg_get_constraintdef(oid) FROM pg_constraint
--    WHERE conrelid = 'haetdeul.master_purchase_records'::regclass AND contype = 'p';
--   -- 기대: PRIMARY KEY (sim_run_id, request_id, decision_seq, leg_seq)
