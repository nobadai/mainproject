-- inbound_schedules 신설 + 기존 fixture JSON 5건 Backfill (2026-09-09 · 물류 · W3-1)
--
-- ══════════════════════════════════════════════════════════════════════════
-- ⚠️  이 파일은 **이미 데이터가 있는 DB 를 옮길 때만** 쓴다.
--
--     신규 구축  →  database/30_logistics_wms_schema.sql §3-0 (같은 표를 만든다)
--     운영 중 DB →  이 파일 (표 + Backfill)
--
-- 🔴 **같은 스키마 변경이 두 곳에 있다.** 어느 하나만 고치면 두 스키마가 조용히
--    갈린다 (README §2). 표 정의를 바꾸면 반드시 둘 다 고친다.
--
-- ★ **Backfill 은 이쪽에만 있다.** `30_` 은 스키마만 만드는 파일이고 시드·이관
--   데이터를 담지 않는다 (README §1 끝).
-- ══════════════════════════════════════════════════════════════════════════
--
-- ★ 왜 만드는가 — **입고 예정이 날짜별 Snapshot 안에 살고 있어서다.**
--
--   종전 입고 예정의 정본은 `logistics_runtime_fixture` 의 두 JSON 칸이었다.
--   그 칸은 하루가 넘어갈 때 carry-forward 로 **다음 날 행에 복제**되어 유지됐다.
--   그래서 *"미래 날짜 행이 먼저 열려 있으면"* 그 행은 나중에 난 승인을 모른 채 굳는다.
--
--     2026-01-15 fixture 생성 (in_transit = [])      ← 먼저 열렸다
--     2026-01-14 승인 → 01-14 행에만 기록
--     2026-01-15 도착 조회 → 볼 것이 없다
--     ⇒ Receipt 0 · Lot 0 · IN Move 0
--
--   실측(2026-09-09)이 정확히 그 상태다.
--
--     INB-H1-REQ-FIRSTINB-20260113-1-1   ETA 2026-01-15   Receipt 없음
--     AP-H1-REQ-FIRSTINB-20260113-1-S1   OPEN 3,066,885원 ← 채무만 남았다
--
--   전방 전파(`master.day_opening_repository.opened_days_after`)가 그 날을 못 본
--   이유는 `master_day_openings` 에 2026-01-10 ~ 01-19 가 **한 행도 없기** 때문이다.
--
--   ⇒ 이 표는 **한 번 INSERT 하고 날짜로 질의한다.** 미래 날짜 행으로 복제하지
--     않으므로 같은 사고가 구조적으로 재현되지 않는다.
--
-- 🔴 **이번 판은 정본을 바꾸지 않았다 (W3-1 · 이 파일을 적용하던 시점).**
--
--     Reader  아직 Legacy JSON
--     Writer  Legacy JSON + inbound_schedules  (Dual Write)
--
--   그래서 이 파일은 **기존 칸을 하나도 안 건드린다** —
--   `logistics_runtime_fixture` 에 UPDATE 도 DELETE 도 없다. 이 성질은 지금도 같다.
--
--   ★ **그 뒤 정본이 옮겨 갔다.** W3-2 가 Reader 를, W3-3 이 Writer 와 carry-forward
--     복제를 옮겼다 — 지금은 두 JSON 칸을 아무도 읽지도 쓰지도 않는다.
--
-- 🔴 **DROP 이 한 줄도 없다.** 기존 표를 다시 만들지 않고, 기존 데이터를 지우지
--    않으며, 기존 값을 바꾸지 않는다.
--
-- 🔴 **`logistics_drop_inbound_json.sql` 보다 반드시 먼저 돌린다.**
--    아래 §2 Backfill 이 `in_transit_json` 을 읽는다 — 그 칸이 걷힌 뒤에는 이 파일이
--    **재실행되지 않는다**(`column does not exist`). 이미 적용된 DB 에서는 다시 돌릴
--    이유가 없다(§3 검증으로 확인).
--
--    ★ **신규 구축에서는 이 파일을 안 돌린다** (README §1). 옮길 JSON 이 한 줄도 없고
--      표는 `30_logistics_wms_schema.sql` §3-0 이 만든다 — 순서 문제 자체가 없다.
--
-- ★ **두 번 돌려도 안전하다.** `CREATE TABLE IF NOT EXISTS` ·
--   `CREATE INDEX IF NOT EXISTS` · Backfill 은 `WHERE NOT EXISTS` 다.
--   🔴 `ON CONFLICT DO UPDATE` 를 쓰지 않는다 — 재실행이 **과거 사실을 덮으면**
--      그 덮어쓰기가 조용한 역사 위조가 된다.
--
-- ⚠️ **적용 전 확인 — Backfill 대상이 정확히 5건이고 매입 줄이 1:1 인가.**
--    아래 §2 가 `HAVING count(*) = 1` 로 그것을 강제하므로, 어느 한 건이라도
--    매입 줄이 0 이거나 2 이상이면 **그 행만 조용히 빠진다.** 그래서 적용 뒤
--    §3 검증 질의로 5건을 반드시 센다.
--
--      SELECT count(*) FROM haetdeul.inbound_schedules;          -- 5 여야 한다
--
--    0 또는 2행인 건이 있으면 그 매입부터 설명돼야 한다 — 이 파일은 데이터를
--    고치지 않는다 (`logistics_allocation_basis_fefo_auto.sql` 과 같은 태도).

BEGIN;

-- ═══════════════════════════════════════════════════════════════════════════
-- §1  표 · 인덱스 — `30_logistics_wms_schema.sql` §3-0 과 **같은 정의**
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS haetdeul.inbound_schedules (
    inbound_id            TEXT NOT NULL,
    sim_run_id            TEXT NOT NULL,
    purchase_item_id      TEXT NOT NULL,
    quantity_kg           NUMERIC(18,6) NOT NULL,
    expected_arrival_date DATE NOT NULL,
    created_as_of         DATE NOT NULL,
    cancelled_as_of       DATE,
    source_ref            TEXT NOT NULL,
    note                  TEXT,
    recorded_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT inbound_schedules_pkey PRIMARY KEY (sim_run_id, inbound_id),
    CONSTRAINT inbound_schedules_sim_run_id_fkey
        FOREIGN KEY (sim_run_id) REFERENCES haetdeul.sim_runs(sim_run_id),
    CONSTRAINT ck_inbound_schedules_qty CHECK (quantity_kg > 0),
    CONSTRAINT ck_inbound_schedules_cancelled_after_created
        CHECK (cancelled_as_of IS NULL OR cancelled_as_of >= created_as_of)
);

CREATE INDEX IF NOT EXISTS idx_inbound_schedules_arrival
    ON haetdeul.inbound_schedules (sim_run_id, expected_arrival_date);

COMMENT ON TABLE haetdeul.inbound_schedules IS
    '입고 예정 업무 Entity (W2.1). 날짜별 fixture JSON 복제를 대체한다 — 한 번 INSERT 하고 날짜로 질의한다. 완료 컬럼이 없다: Receipt 생성과 재고 반영은 다른 사건이고 소비자마다 종료점이 다르다.';
COMMENT ON COLUMN haetdeul.inbound_schedules.inbound_id IS
    '물류가 셈하는 입고 건 (INB-{approval_id}-{seq}). inbound_receipts.inbound_id 와 같은 값이고 그 표에 UNIQUE(sim_run_id, inbound_id) 가 이미 있다.';
COMMENT ON COLUMN haetdeul.inbound_schedules.purchase_item_id IS
    '매입 줄 PK. 🔴 이것 하나가 purchase_id · item_id · grade · 단가를 결정한다 — 그 셋을 여기 복제하지 않는다. FK 는 purchases CASCADE 삭제 정책이 정해질 때까지 보류.';
COMMENT ON COLUMN haetdeul.inbound_schedules.quantity_kg IS
    '이 회차의 입고 예정량. 🔴 purchase_items.quantity_kg 와 같은 값이어야 하는 것이 아니다 — 분할 회차는 매입 줄 하나를 여러 회차로 나눈다.';
COMMENT ON COLUMN haetdeul.inbound_schedules.created_as_of IS
    '이 일정이 장부에 선 시뮬레이션 날짜 (승인 전이의 target_state_date). 🔴 벽시각이 아니다 — 과거 재현이 이 값으로 선다.';
COMMENT ON COLUMN haetdeul.inbound_schedules.cancelled_as_of IS
    'NULL 이면 살아 있다. 값이 있으면 그날부터 취소다 (as_of < cancelled_as_of 인 날에는 여전히 존재). status 컬럼을 따로 두지 않는 이유가 이 한 칸이다.';
COMMENT ON COLUMN haetdeul.inbound_schedules.recorded_at IS
    '감사용 벽시각. 🔴 Historical 판정에 쓰지 않는다 — 시뮬레이션 날짜는 created_as_of · cancelled_as_of 다.';


-- ═══════════════════════════════════════════════════════════════════════════
-- §2  Backfill — 살아 있는 fixture 의 `in_transit_json` 항목
--
--     🔴 **`created_as_of` 는 그 항목이 실린 `fixture.as_of` 다.**
--        `inbound_id` 문자열(`INB-H1-{request_id}-{seq}`)을 뜯어 승인일을 되짚지
--        않는다 — 그 형식의 주인은 마스터이고, 물류가 파싱하면 형식이 바뀌는 날
--        조용히 어긋난다.
--
--     🔴 **`purchase_item_id` 는 `LIMIT 1` 로 고르지 않는다.**
--        `HAVING count(*) = 1` 로 **정확히 한 줄일 때만** 가져온다. 실측상 승인 경로
--        매입 5건은 전부 1품목이지만(씨앗 매입은 5품목짜리가 16건 있다),
--        그 전제가 깨지는 날 조용히 남의 줄을 붙이면 그 단가가 로트 원가로 굳는다.
--
--     ★ **`confirmed_inbound_json` 을 따로 읽지 않는다.** 두 칸은 같은 승인분을
--       같은 함수(`transition._merge_schedule`)가 함께 적어 실측 5/5 가 동일하다.
--       한 Entity 로 합치는 것이 이 표의 목적이라 한쪽만 읽는다.
--
--     ★ **FIRSTINB 를 지우거나 취소로 만들지 않는다.** `cancelled_as_of = NULL` 로
--       그대로 들어간다 — *"승인됐으나 도착 처리되지 않은 일정"* 이 그 건의
--       역사 사실이고, 그것을 고치는 것은 Backfill 의 일이 아니다.
-- ═══════════════════════════════════════════════════════════════════════════

INSERT INTO haetdeul.inbound_schedules (
    inbound_id, sim_run_id, purchase_item_id, quantity_kg,
    expected_arrival_date, created_as_of, cancelled_as_of, source_ref, note
)
SELECT
    src.inbound_id,
    src.sim_run_id,
    src.purchase_item_id,
    src.quantity_kg,
    src.expected_arrival_date,
    src.created_as_of,
    NULL,
    'BACKFILL:logistics_runtime_fixture/' || src.fixture_id,
    'W3-1 Backfill 2026-09-09 · 원천 in_transit_json · 정본 전환은 W3-2'
FROM (
    SELECT
        f.fixture_id,
        f.sim_run_id,
        f.as_of                                        AS created_as_of,
        x ->> 'inbound_id'                             AS inbound_id,
        (x ->> 'quantity_kg')::NUMERIC(18,6)           AS quantity_kg,
        (x ->> 'expected_arrival_date')::DATE          AS expected_arrival_date,
        -- 🔴 정확히 한 줄일 때만 값이 나온다. 0행이나 2행 이상이면 HAVING 이
        --    행 자체를 없애 서브쿼리가 NULL 이 되고, 아래 WHERE 가 그 건을 뺀다.
        --    `LIMIT 1` 이었다면 2행일 때 **아무 줄이나** 붙었을 것이다.
        (
            SELECT max(pi.purchase_item_id)
              FROM haetdeul.purchase_items pi
             WHERE pi.purchase_id = x ->> 'purchase_id'
            HAVING count(*) = 1
        )                                              AS purchase_item_id
      FROM haetdeul.logistics_runtime_fixture f
      CROSS JOIN LATERAL jsonb_array_elements(f.in_transit_json) AS x
     WHERE f.is_active
       AND f.usage_scope = 'AGENT_MVP_DEMO'
       AND x ->> 'inbound_id' IS NOT NULL
       AND x ->> 'purchase_id' IS NOT NULL
       AND x ->> 'expected_arrival_date' IS NOT NULL
) src
WHERE src.purchase_item_id IS NOT NULL
  AND NOT EXISTS (
        SELECT 1
          FROM haetdeul.inbound_schedules s
         WHERE s.sim_run_id = src.sim_run_id
           AND s.inbound_id = src.inbound_id
      );

COMMIT;

-- ═══════════════════════════════════════════════════════════════════════════
-- §3  적용 뒤 검증 — 이 셋이 다 맞아야 끝난 것이다
-- ═══════════════════════════════════════════════════════════════════════════
--
--   ① 건수 — 5 여야 한다
--     SELECT count(*) FROM haetdeul.inbound_schedules;
--
--   ② JSON ↔ schedule 대조 — 0행이어야 한다 (빠짐 · 값 어긋남 둘 다 잡는다)
--     SELECT x ->> 'inbound_id' AS inbound_id
--       FROM haetdeul.logistics_runtime_fixture f
--       CROSS JOIN LATERAL jsonb_array_elements(f.in_transit_json) x
--      WHERE f.is_active AND f.usage_scope = 'AGENT_MVP_DEMO'
--        AND NOT EXISTS (
--              SELECT 1 FROM haetdeul.inbound_schedules s
--               WHERE s.sim_run_id = f.sim_run_id
--                 AND s.inbound_id = x ->> 'inbound_id'
--                 AND s.quantity_kg = (x ->> 'quantity_kg')::NUMERIC(18,6)
--                 AND s.expected_arrival_date = (x ->> 'expected_arrival_date')::DATE
--                 AND s.created_as_of = f.as_of);
--
--   ③ FIRSTINB — 살아 있는 채로 들어갔나
--     SELECT inbound_id, created_as_of, expected_arrival_date, cancelled_as_of
--       FROM haetdeul.inbound_schedules
--      WHERE inbound_id = 'INB-H1-REQ-FIRSTINB-20260113-1-1';
--     -- created_as_of 2026-01-14 · ETA 2026-01-15 · cancelled_as_of NULL
