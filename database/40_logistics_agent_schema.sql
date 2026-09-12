-- 재고·물류 AI Agent Core — 운영 Exception · 대응안 표
--   (2026-09-12 · #628 Commit 2 / 2026-09-13 · #628 Commit 5)
--
-- ══════════════════════════════════════════════════════════════════════════
-- 🔴 **표가 둘이다.**
--
--   Commit 2 는 **하나만** 세웠다 — 제안·승인 기능이 없는 판에 빈 표를 먼저 세우면
--   "있는데 아무도 안 쓴다" 가 사실로 굳어서다. 이제 그 기능이 서므로 둘째 표를
--   같은 파일에 잇는다.
--
--   ```text
--   logistics_exceptions        §1  Commit 2   운영 판단이 필요한 조건
--   logistics_action_proposals  §3  Commit 5   그 조건에 대한 **대응안 한 건**
--   ```
--
-- 🔴 **조사 실행 표는 여전히 안 만든다.** Commit 4 의 조사는 DB 에 아무것도 안 쓰고,
--    범용 `agent_runs` 도 아직 물류가 쓰지 않는다 (상세설계 §11). 그래서 제안의
--    `investigation_id` 는 **가리킬 곳이 없는 지금 NULL 이고 FK 도 없다** — 없는 표를
--    가리키는 FK 를 먼저 박으면 제안을 아예 못 만든다.
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
-- 🔴 **다른 파트 표를 안 건드린다.** 이 파일이 남의 표에 하는 일은 `sim_runs` 를
--    FK 로 가리키는 것뿐이다. DROP 은 한 줄도 없다.
--
--   ⚠️ **ALTER 는 한 줄 생겼다 (§2 · Commit 5).** 대상은 이 파일이 만든 자기 표
--      (`logistics_exceptions`) 이고, 하는 일은 칸 하나 추가다 — 남의 표가 아니다.
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

-- ═══════════════════════════════════════════════════════════════════════════
-- §2  Exception 이 «대응안을 기다리는 상태» 가 된 날  (#628 Commit 5)
--
--     🔴 **상태만 있고 날짜가 없으면 과거를 못 센다.**
--
--     ```text
--     D1   OPEN
--     D10  PROPOSED
--
--     "D5 에 이 문제는 어떤 상태였나" → 전이 날짜가 없으면 대답이 «지금 값» 뿐이다
--     ```
--
--     ⚠️ **이 칸은 상태의 거울이 아니라 «처음» 대응안이 선 날이다.** 제안이 거절되어
--        Exception 이 OPEN 으로 돌아가도 **지우지 않는다** — 그날 제안이 섰다는 것은
--        일어난 사실이고, 상태가 되돌아갔다고 사실을 지울 이유가 없다.
--        `opened_as_of` 와 같은 규율이다(한 번 적히면 불변).
--
--     🔴 **이 한 칸이 상태 이력 전체는 아니다.** OPEN↔PROPOSED 를 여러 번 오간
--        이력은 이 칸으로 복원되지 않는다 — 그것이 필요해지면 전이 이력 표가 따로
--        있어야 한다(Commit 6 이후 판단). 지금 세우는 사실은 하나다:
--        *"이 문제에 대응안이 처음 선 날"*.
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE haetdeul.logistics_exceptions
    ADD COLUMN IF NOT EXISTS proposed_as_of DATE;

-- 🔴 PROPOSED 인데 그날을 못 대는 행을 막는다. `ADD CONSTRAINT IF NOT EXISTS` 가
--    없는 문법이라 멱등은 DO 블록으로 만든다 (`logistics_agent_runs.sql` 과 같은 꼴).
DO $ck_proposed$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_logistics_exceptions_proposed'
          AND conrelid = 'haetdeul.logistics_exceptions'::regclass
    ) THEN
        ALTER TABLE haetdeul.logistics_exceptions
            ADD CONSTRAINT ck_logistics_exceptions_proposed
            CHECK (status <> 'PROPOSED' OR proposed_as_of IS NOT NULL);
    END IF;
END
$ck_proposed$;

COMMENT ON COLUMN haetdeul.logistics_exceptions.proposed_as_of IS
    '이 문제에 대응안이 **처음** 선 시뮬레이션 영업일. 🔴 한 번 적히면 불변이다 — 제안이 거절되어 status 가 OPEN 으로 돌아가도 지우지 않는다(일어난 사실이다). ⚠️ 상태 이력 전체가 아니다: OPEN↔PROPOSED 를 여러 번 오간 자취는 이 칸으로 복원되지 않는다.';


-- ═══════════════════════════════════════════════════════════════════════════
-- §3  대응안 — "그 문제에 무엇을 할까" 한 줄  (#628 Commit 5)
--
--     🔴 **APPROVED 는 EXECUTED 가 아니다.**
--
--     ```text
--     APPROVED   "이 대응안으로 진행해도 좋다" 는 **사람의 결정**
--     ≠          판매가 일어났다 · 매입 일정이 바뀌었다 · 재고가 폐기됐다
--     ```
--
--        승인 뒤에도 `inventory_lots` · `inventory_moves` · `sales` · 매입 일정은
--        한 줄도 안 바뀐다. 실제 실행은 **Commit 6** 이고, 그때까지 이 표가 드는 것은
--        *"사람이 무엇을 승인했나"* 라는 사실 하나뿐이다.
--
--     🔴 **숫자를 여기서 만들지 않는다.** `impact_json` 은 Commit 3 의
--        `estimate_action_impact` 가 낸 답 그대로이고, 그 계산은 기존 결정론
--        계산기가 한다. 제안이 저장되면서 다시 셈하는 값은 하나도 없다.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS haetdeul.logistics_action_proposals (
    -- PRP-{exception_id}-{n}. 업무 키다 — 어느 문제의 몇 번째 대응안인가.
    proposal_id           TEXT NOT NULL,
    -- 🔴 리셋 축. 이 칸이 있어서 `reset_sim_run_ledger` 가 표를 **자동으로 발견**한다.
    sim_run_id            TEXT NOT NULL,
    exception_id          TEXT NOT NULL,
    -- 🔴 **아직 가리킬 표가 없다** — Commit 4 의 조사는 DB 에 안 남는다. FK 도 안 건다.
    --    Commit 7 에서 조사 실행이 기록되면 그때 값이 찬다.
    investigation_id      TEXT,
    -- 재시도 식별용 지문 = f(sim_run_id, exception_id, action_type, 정규화된 parameters).
    -- 🔴 **유일 제약이 아니다** — 거절된 뒤 같은 안을 다시 올리는 것은 정상이다.
    proposal_key          TEXT NOT NULL,

    action_type           TEXT NOT NULL,
    -- 🔴 **모델이 고른 값이 아니다.** `ACTION_DECISION_OWNERS` 에서 다시 계산한다.
    --    ⚠️ `approved_by`(승인한 사람) 와 다른 축이다 — 승인은 사람이 하고, 그 뒤
    --       실제 실행 책임은 이 부서가 진다.
    decision_owner        TEXT NOT NULL,
    parameters_json       JSONB NOT NULL,
    -- `estimate_action_impact` 의 답 그대로. feasibility ∈ FEASIBLE·UNRESOLVED.
    -- 🔴 `UNRESOLVED` 를 숫자로 메워 `FEASIBLE` 로 바꾸지 않는다 (상세설계 §27).
    impact_json           JSONB NOT NULL,
    -- [{sequence, tool_name, arguments, observed_as_of}, …]
    -- 🔴 번호만 적지 않는다 — 조사 실행이 저장되지 않으므로 번호는 가리킬 곳이 없다.
    evidence_refs_json    JSONB NOT NULL,
    -- 모델이 적은 문장. **업무 사실이 아니라 기록이다** — 숫자의 주인은 impact_json.
    rationale             TEXT NOT NULL DEFAULT '',
    -- 이 제안을 낸 조사가 어떻게 끝났나 (FINISHED · BUDGET_EXCEEDED · …) / AI 가 실제로
    -- 판단했나 (SUCCESS · FALLBACK · DISABLED · SKIPPED_TEMPLATE).
    source_finish_reason  TEXT,
    source_llm_status     TEXT,

    status                TEXT NOT NULL,

    -- 시뮬레이션 영업일. 🔴 `created_at`(벽시계)과 다른 축이다.
    proposed_as_of        DATE NOT NULL,
    approved_as_of        DATE,
    rejected_as_of        DATE,
    expired_as_of         DATE,
    superseded_as_of      DATE,
    -- 🔴 조사 결과의 값을 **그대로** 옮긴다. 제안일·승인일·현재시각으로 메우지 않는다.
    observed_as_of        DATE,

    proposed_by           TEXT NOT NULL,
    approved_by           TEXT,
    rejected_by           TEXT,
    approval_note         TEXT,
    rejection_reason      TEXT,

    -- 이 제안이 대체한 이전 제안.
    previous_proposal_id  TEXT,

    -- 🔴 DB 벽시계. 감사용이고 **영업 판단에 쓰지 않는다.**
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT logistics_action_proposals_pkey PRIMARY KEY (proposal_id),

    CONSTRAINT logistics_action_proposals_sim_run_id_fkey
        FOREIGN KEY (sim_run_id) REFERENCES haetdeul.sim_runs(sim_run_id),
    CONSTRAINT logistics_action_proposals_exception_fkey
        FOREIGN KEY (exception_id)
        REFERENCES haetdeul.logistics_exceptions(exception_id),
    CONSTRAINT logistics_action_proposals_previous_fkey
        FOREIGN KEY (previous_proposal_id)
        REFERENCES haetdeul.logistics_action_proposals(proposal_id),

    -- 🔴 카탈로그는 닫혀 있다 (상세설계 §10.1). 모델이 이름을 지어내도 행이 안 된다.
    CONSTRAINT ck_logistics_action_proposals_action
        CHECK (action_type IN ('SALES_PRIORITY_REQUEST', 'PURCHASE_ADJUST_REQUEST',
                               'ACCEPT_RISK', 'DISPOSAL_REQUEST')),
    CONSTRAINT ck_logistics_action_proposals_owner
        CHECK (decision_owner IN ('LOGISTICS', 'SALES', 'PURCHASE')),
    -- ⚠️ EXECUTED · FAILED 는 **어휘로만** 둔다 — Commit 5 의 Production 코드는 이
    --    둘로 전이시키지 않는다. 실행은 Commit 6 이다.
    CONSTRAINT ck_logistics_action_proposals_status
        CHECK (status IN ('PROPOSED', 'APPROVED', 'REJECTED', 'EXPIRED', 'SUPERSEDED',
                          'EXECUTED', 'FAILED')),

    -- 🔴 **결정에는 날과 사람이 있다.** 둘 중 하나가 비면 "누가 언제 정했나" 를 못 댄다.
    CONSTRAINT ck_logistics_action_proposals_approved
        CHECK (status <> 'APPROVED'
               OR (approved_as_of IS NOT NULL AND approved_by IS NOT NULL)),
    CONSTRAINT ck_logistics_action_proposals_rejected
        CHECK (status <> 'REJECTED'
               OR (rejected_as_of IS NOT NULL AND rejected_by IS NOT NULL
                   AND rejection_reason IS NOT NULL)),
    CONSTRAINT ck_logistics_action_proposals_expired
        CHECK (status <> 'EXPIRED' OR expired_as_of IS NOT NULL),
    CONSTRAINT ck_logistics_action_proposals_superseded
        CHECK (status <> 'SUPERSEDED' OR superseded_as_of IS NOT NULL),

    -- 🔴 **끝난 날이 둘이면 그날 상태를 못 고른다.** 과거 재현의 전제다.
    --    ⚠️ Commit 6 이 APPROVED → SUPERSEDED 를 열어야 한다면 이 제약과 과거 재현의
    --       우선순위를 **함께** 다시 정해야 한다 — 한쪽만 풀면 조회가 조용히 틀린다.
    CONSTRAINT ck_logistics_action_proposals_single_terminal
        CHECK ((approved_as_of    IS NOT NULL)::int
             + (rejected_as_of    IS NOT NULL)::int
             + (expired_as_of     IS NOT NULL)::int
             + (superseded_as_of  IS NOT NULL)::int <= 1),

    -- 제안된 날보다 앞서 결정될 수 없다.
    CONSTRAINT ck_logistics_action_proposals_decided_order
        CHECK (COALESCE(approved_as_of,   proposed_as_of) >= proposed_as_of
           AND COALESCE(rejected_as_of,   proposed_as_of) >= proposed_as_of
           AND COALESCE(expired_as_of,    proposed_as_of) >= proposed_as_of
           AND COALESCE(superseded_as_of, proposed_as_of) >= proposed_as_of),

    -- 🔴 사람 칸을 **빈 문자열로 채우지 않는다.** ''는 "모른다" 를 "있다" 로 위장한다.
    CONSTRAINT ck_logistics_action_proposals_actors
        CHECK (length(btrim(proposed_by)) > 0
           AND (approved_by      IS NULL OR length(btrim(approved_by)) > 0)
           AND (rejected_by      IS NULL OR length(btrim(rejected_by)) > 0)
           AND (rejection_reason IS NULL OR length(btrim(rejection_reason)) > 0)),

    -- 🔴 **영향을 못 댄 제안은 행이 될 수 없다.** Exception 의 근거 CHECK 와 같은 규율
    --    이다 — 다만 «못 쟀다»(UNRESOLVED) 는 정상적인 답이고 여기서 막지 않는다.
    CONSTRAINT ck_logistics_action_proposals_payload
        CHECK (jsonb_typeof(parameters_json) = 'object'
           AND jsonb_typeof(impact_json) = 'object'
           AND jsonb_typeof(evidence_refs_json) = 'array')
);

-- 🔴 **한 문제에 살아 있는 대응안은 하나다.** 응용 코드의 조회가 한 번 빠지는 날에도
--    DB 가 막는다 — 같은 문제에 승인 대기 제안이 둘이면 사람이 무엇을 승인하는지
--    모른다. ⚠️ APPROVED 도 «살아 있다» — 실행(Commit 6)이 끝나야 자리가 빈다.
CREATE UNIQUE INDEX IF NOT EXISTS uq_logistics_action_proposals_live
    ON haetdeul.logistics_action_proposals (sim_run_id, exception_id)
    WHERE status IN ('PROPOSED', 'APPROVED');

CREATE INDEX IF NOT EXISTS idx_logistics_action_proposals_run_status
    ON haetdeul.logistics_action_proposals (sim_run_id, status);

CREATE INDEX IF NOT EXISTS idx_logistics_action_proposals_exception
    ON haetdeul.logistics_action_proposals (sim_run_id, exception_id);

CREATE INDEX IF NOT EXISTS idx_logistics_action_proposals_key
    ON haetdeul.logistics_action_proposals (sim_run_id, proposal_key);

COMMENT ON TABLE haetdeul.logistics_action_proposals IS
    '재고·물류 Agent 가 올린 대응안 한 건과 사람의 결정 (#628 Core). 🔴 APPROVED 는 EXECUTED 가 아니다 — "진행해도 좋다" 는 사람의 결정일 뿐이고 승인 뒤에도 재고·판매·매입은 한 줄도 안 바뀐다(실행은 Commit 6). 한 Exception 에 살아 있는(PROPOSED·APPROVED) 행은 하나다.';
COMMENT ON COLUMN haetdeul.logistics_action_proposals.decision_owner IS
    '승인 뒤 실제 실행 책임을 지는 부서. 🔴 ACTION_DECISION_OWNERS 에서 결정론으로 계산하며 모델이 고르는 칸이 아니다. ⚠️ approved_by(승인한 사람)와 다른 축이다.';
COMMENT ON COLUMN haetdeul.logistics_action_proposals.impact_json IS
    'estimate_action_impact(Commit 3)의 답 그대로. 🔴 제안을 저장하며 다시 셈하는 숫자는 없고, UNRESOLVED 를 지어낸 값으로 메워 FEASIBLE 로 바꾸지 않는다.';
COMMENT ON COLUMN haetdeul.logistics_action_proposals.observed_as_of IS
    '조사가 낸 값 그대로 — 근거 입력들이 알 수 있었던 가장 늦은 날. 🔴 하나라도 관측일이 없으면 NULL 이고 제안일·승인일·현재시각으로 메우지 않는다.';
COMMENT ON COLUMN haetdeul.logistics_action_proposals.proposal_key IS
    'f(sim_run_id, exception_id, action_type, 정규화된 parameters) 지문. 재시도로 같은 제안이 두 번 들어오는 것을 알아보는 용도다. 🔴 유일 제약이 아니다 — 거절된 뒤 같은 안을 다시 올리는 것은 정상이다.';
COMMENT ON COLUMN haetdeul.logistics_action_proposals.rationale IS
    '모델이 적은 이유 문장. 🔴 업무 사실이 아니라 기록이다 — 숫자와 판정의 주인은 impact_json 이다.';

COMMIT;
