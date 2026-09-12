-- 재고·물류 AI Agent Core — 운영 Exception 표 (2026-09-12 · #628 Commit 2)
--
-- ══════════════════════════════════════════════════════════════════════════
-- 🔴 **표 하나뿐이다.**
--
--   Core 신규 표는 둘(`logistics_exceptions` · `logistics_action_proposals`)이지만
--   뒤엣것은 **Commit 5** 의 것이다 — 제안·승인 기능이 없는 판에 빈 표를 먼저
--   세우면 "있는데 아무도 안 쓴다" 가 사실로 굳는다. 조사 실행 기록도 새 표를
--   만들지 않는다: 기존 `agent_runs` 를 쓴다 (상세설계 §11).
--
--   ```text
--   logistics_exceptions        ← 이 파일 (Commit 2)
--   logistics_action_proposals    Commit 5
--   agent_runs                    기존 표 재사용 (Commit 4~)
--   ```
--
-- 🔴 **Core 에 없는 것** — 여기 만들지 않는다 (상세설계 §11 · §19.3 · §20):
--   `logistics_tasks` · `warehouse_events` · `inventory_lots.disposition_since`
--   (Simulation Fixture Phase 4.5) · `environment_observations` · `stock_units`
--   (Future).
--
-- ★ 실행 위치 — `30_logistics_wms_schema.sql` **다음**이다.
--
--   ```text
--   00_init_schema           스키마
--   10_domain_schema         items · sim_runs · inventory_lots · agent_runs
--   30_logistics_wms_schema  WMS 표 21 · 뷰 2
--   40_logistics_agent_schema  ← 이 파일. 위의 sim_runs 를 FK 로 참조한다
--   ```
--
-- 🔴 **다른 파트 표를 안 건드린다.** 이 파일이 기존 표에 하는 일은 `sim_runs` 를
--    FK 로 가리키는 것뿐이다. ALTER 도 DROP 도 한 줄 없다.
--
-- ★ 멱등이다 — `CREATE TABLE IF NOT EXISTS` · `CREATE INDEX IF NOT EXISTS`.
--   신규 구축 DB 와 운영 DB 에 같은 파일을 그대로 돌린다 (`30_` 과 같은 규율).
-- ══════════════════════════════════════════════════════════════════════════

BEGIN;

-- ═══════════════════════════════════════════════════════════════════════════
-- §1  운영 Exception — "운영 판단이 필요한 조건" 한 줄
--
--     🔴 **사건 로그가 아니다.** 같은 문제를 날마다 새 행으로 쌓지 않는다 —
--        살아 있는(OPEN · PROPOSED) Exception 은 dedupe 축마다 **하나**이고,
--        조건이 계속 참이면 그 행을 갱신한다(`last_detected_as_of` 가 "N일째").
--        조건이 사라지면 RESOLVED 로 닫고, 다시 생기면 **새 행 +
--        `previous_exception_id`** 로 잇는다. 재오픈은 없다.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS haetdeul.logistics_exceptions (
    -- EX-{sim_run_id}-{code}-{subject_id}-{opened_as_of:YYYYMMDD}. 업무 키다.
    exception_id          TEXT NOT NULL,
    -- 🔴 리셋 축. 이 칸이 있어서 `reset_sim_run_ledger` 가 표를 **자동으로 발견**해
    --    `--reset` 때 지운다 (`master/sim_run_open.py` `_axis_tables`).
    sim_run_id            TEXT NOT NULL,
    -- 탐지기 어휘. Core 는 둘을 만들고 `FRESHNESS_EXPIRED` 는 **예약**이다
    -- (상세설계 §6.2) — 자동 유지보수가 켜진 실행에서는 개장 때 폐기되어 열릴 틈이 없다.
    code                  TEXT NOT NULL,
    subject_type          TEXT NOT NULL,
    subject_id            TEXT NOT NULL,
    severity              TEXT NOT NULL,
    status                TEXT NOT NULL,
    -- 시뮬레이션 영업일. 🔴 `created_at`(벽시계)과 다른 축이다.
    opened_as_of          DATE NOT NULL,
    last_detected_as_of   DATE NOT NULL,
    -- 🔴 **"안 쟀다" 를 적는 칸이다.** 근거 입력들의 원천 관측일 중 가장 늦은 것이고,
    --    하나라도 관측일이 없으면 NULL 이다. `as_of` 로 메우지 않는다 (상세설계 §18).
    --    정책 표(`agent_policy_config` · `item_storage_policies`)에 유효일 칸이 없어
    --    지금은 대부분 NULL 이다 — 그것이 정직한 값이다.
    observed_as_of        DATE,
    resolved_as_of        DATE,
    -- 무엇이 닫았나: REDETECT · LOT_EMPTY · COMMITTED · ESCALATED:FRESHNESS_EXPIRED ·
    -- DISMISSED:{사람}. 🔴 사람 이름을 탐지기가 지어내지 않는다.
    resolved_by           TEXT,
    -- ACCEPT_RISK 승인 기록 (Commit 5). 🔴 **닫지 않는다** — 재탐지는 계속 돈다.
    risk_accepted_as_of   DATE,
    -- [{fact, value, unit, source, source_id, observed_as_of}, …]
    -- 🔴 **근거 없는 Exception 을 만들지 않는다** — 빈 배열은 CHECK 가 막는다.
    evidence_json         JSONB NOT NULL,
    -- 임계를 바꾸면 올린다. 같은 code 라도 다른 기준으로 열린 행을 가른다.
    detector_version      TEXT NOT NULL,
    -- RESOLVED 뒤 같은 조건이 다시 잡히면 새 행이 이전 행을 가리킨다.
    previous_exception_id TEXT,
    note                  TEXT,
    -- 🔴 DB 벽시계. 감사용이고 **영업 판단에 쓰지 않는다.**
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT logistics_exceptions_pkey PRIMARY KEY (exception_id),

    CONSTRAINT logistics_exceptions_sim_run_id_fkey
        FOREIGN KEY (sim_run_id) REFERENCES haetdeul.sim_runs(sim_run_id),
    CONSTRAINT logistics_exceptions_previous_fkey
        FOREIGN KEY (previous_exception_id)
        REFERENCES haetdeul.logistics_exceptions(exception_id),

    CONSTRAINT ck_logistics_exceptions_code
        CHECK (code IN ('FRESHNESS_PRESSURE', 'CAPACITY_PRESSURE', 'FRESHNESS_EXPIRED')),
    CONSTRAINT ck_logistics_exceptions_subject_type
        CHECK (subject_type IN ('LOT', 'WAREHOUSE')),
    CONSTRAINT ck_logistics_exceptions_severity
        CHECK (severity IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
    CONSTRAINT ck_logistics_exceptions_status
        CHECK (status IN ('OPEN', 'PROPOSED', 'RESOLVED', 'DISMISSED')),

    -- 🔴 근거가 하나도 없는 Exception 은 **행이 될 수 없다.**
    CONSTRAINT ck_logistics_exceptions_evidence
        CHECK (jsonb_typeof(evidence_json) = 'array' AND jsonb_array_length(evidence_json) > 0),

    -- 처음 잡힌 날보다 앞서 다시 잡힐 수 없다.
    CONSTRAINT ck_logistics_exceptions_detected
        CHECK (last_detected_as_of >= opened_as_of),
    -- 살아 있는 Exception 에는 닫힌 날이 없고, RESOLVED 에는 반드시 있다.
    CONSTRAINT ck_logistics_exceptions_resolved
        CHECK ((status IN ('OPEN', 'PROPOSED') AND resolved_as_of IS NULL)
               OR (status = 'RESOLVED' AND resolved_as_of IS NOT NULL)
               OR status = 'DISMISSED'),
    CONSTRAINT ck_logistics_exceptions_resolved_order
        CHECK (resolved_as_of IS NULL OR resolved_as_of >= opened_as_of)
);

-- 🔴 **중복 방지의 주인이 여기다.** 같은 실행·같은 코드·같은 대상에 살아 있는
--    Exception 은 하나뿐이다. 탐지기가 매일 INSERT 를 시도해도 DB 가 막는다 —
--    응용 코드의 조회가 한 번 빠지는 날에도 장부는 안 갈린다.
CREATE UNIQUE INDEX IF NOT EXISTS uq_logistics_exceptions_live
    ON haetdeul.logistics_exceptions (sim_run_id, code, subject_type, subject_id)
    WHERE status IN ('OPEN', 'PROPOSED');

CREATE INDEX IF NOT EXISTS idx_logistics_exceptions_run_status
    ON haetdeul.logistics_exceptions (sim_run_id, status);

CREATE INDEX IF NOT EXISTS idx_logistics_exceptions_subject
    ON haetdeul.logistics_exceptions (sim_run_id, subject_type, subject_id);

COMMENT ON TABLE haetdeul.logistics_exceptions IS
    '재고·물류 Agent 가 결정론으로 탐지한 운영 판단 대상 (#628 Core). 사건 로그가 아니라 지속되는 문제 한 줄이다 — 살아 있는(OPEN·PROPOSED) 행은 (sim_run_id, code, subject_type, subject_id) 마다 하나이고, 조건이 사라지면 RESOLVED 로 닫는다.';
COMMENT ON COLUMN haetdeul.logistics_exceptions.observed_as_of IS
    '근거 입력들이 알 수 있었던 가장 늦은 날. 🔴 하나라도 관측일이 없으면 NULL 이고 as_of 로 메우지 않는다 — 정책 표에 유효일 칸이 없어 지금은 대부분 NULL 이며 그것이 "안 쟀다" 라는 사실이다.';
COMMENT ON COLUMN haetdeul.logistics_exceptions.evidence_json IS
    '[{fact, value, unit, source, source_id, observed_as_of}]. value 는 문자열로 적는다 — JSON 수치로 적으면 Decimal 이 float 을 지나며 조용히 흔들린다.';
COMMENT ON COLUMN haetdeul.logistics_exceptions.resolved_by IS
    '무엇이 닫았나. REDETECT(조건 소멸) · LOT_EMPTY(잔량 0) · COMMITTED(할당이 잔량을 덮음) · ESCALATED:FRESHNESS_EXPIRED(신선도 만료로 넘어감) · DISMISSED:{사람}.';
COMMENT ON COLUMN haetdeul.logistics_exceptions.previous_exception_id IS
    'RESOLVED 뒤 같은 조건이 다시 잡혔을 때 이전 행. 🔴 재오픈하지 않는다 — 닫힌 날과 다시 열린 날이 한 행에 겹치면 "며칠째" 를 셀 수 없다.';

COMMIT;
