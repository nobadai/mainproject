-- 재고·물류 AI Agent Core — 운영 Exception · 조사 실행 · 대응안 표
--   (2026-09-12 · #628 Commit 2 / 2026-09-13 · #628 Commit 5 · Commit 7)
--
-- ══════════════════════════════════════════════════════════════════════════
-- 🔴 **표가 셋이고, 한 줄로 이어진다.**
--
--   표는 필요해진 판에서 하나씩 섰다 — 빈 표를 먼저 세우면 "있는데 아무도 안 쓴다"
--   가 사실로 굳어서다.
--
--   ```text
--   logistics_exceptions        §1  Commit 2   운영 판단이 필요한 조건
--   logistics_investigations    §3  Commit 7   그 조건을 **실제로 조사한 한 번**
--   logistics_action_proposals  §4  Commit 5   그 조사가 낸 **대응안 한 건**
--   ```
--
--   ```text
--   Exception ──┬── Investigation A ── Proposal A
--               ├── Investigation B ── (제안 없음)
--               └── Investigation C ── Proposal B
--
--   Exception : Investigation = 1:N     같은 문제를 여러 번 조사할 수 있다
--   Investigation : Proposal   = 1:0..1  조사했다고 늘 제안이 나오지는 않고,
--                                        났다면 **하나**다
--   ```
--
--   🔴 **«0..1» 의 1 을 DB 가 지킨다** (`uq_logistics_action_proposals_investigation`).
--      한 조사는 **한 번 끝난 판단**이고 그 판단이 고른 대응안은 하나다
--      (`recommended_index` 하나). 그 조사로 제안을 둘 만들 수 있으면, 끝난 판단 하나가
--      승인 대상 둘을 낳는다 — 새 안이 필요하면 **새 조사**를 돌려야 한다.
--
-- 🔴 **`investigation_id` 가 드디어 가리킬 곳을 얻었다** (Commit 7). Commit 5 는 그
--    칸을 NULL·FK 없음으로 두었다 — 조사가 DB 에 안 남던 때라 가리킬 행이 없어서다.
--    이제 §3 이 그 정본을 세우고 §4 가 **실행·문제 축까지 묶어** 가리킨다.
--    ⚠️ **칸은 여전히 nullable 이다.** 손으로 세운 제안과 Commit 5 시절의 기존 행은
--       가리킬 조사가 없고, 그것을 NOT NULL 로 막으면 과거 행이 통째로 불법이 된다.
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
--   ⚠️ **ALTER 가 있다 (§2 · §5).** 대상은 전부 이 파일이 만든 자기 표
--      (`logistics_exceptions` · `logistics_investigations` ·
--      `logistics_action_proposals`) 이고, 하는 일은 칸 하나와 제약 몇 개를 더하는
--      것뿐이다 — 남의 표가 아니고 DROP 도 없다.
--
--   🔴 **기존 운영 DB 가 §5 의 이유다.** `CREATE TABLE IF NOT EXISTS` 안의 제약을
--      고쳐도 표가 이미 있는 DB 에는 **아무 일도 안 일어난다.** 그래서 새 제약은
--      «CREATE 안» 과 «멱등 ALTER» **양쪽**에 적는다 — 신규 DB 는 앞엣것으로, 운영
--      DB 는 뒤엣것으로 같은 자리에 도착한다.
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

-- ═══════════════════════════════════════════════════════════════════════════
-- §3  Exception 감지 severity 의 «날짜별 이력»  (LOG-AGENT-005)
--
--     🔴 **`severity` 는 덮어써서 과거를 못 센다.** `touch_exception` 이 재감지마다
--        `SET severity = …` 로 최신값을 덮으므로, 화면이 과거 `as_of` 를 보면 그날
--        우선도를 증명할 수 없어 `—` 로 감춘다.
--
--     ⇒ 감지마다 «그날 · severity» 한 벌을 이 칸에 쌓는다.
--        원소 = {"as_of": 감지날짜, "severity": …} · 하루 1원소(같은 날은 마지막
--        감지값으로 교체) · Historical 은 as_of <= 기준일 중 max(as_of) 원소로 복원한다.
--
--     ⚠️ **backfill 하지 않는다.** 기존 행은 DEFAULT `[]` 로 남고 화면 fallback(`—`)을
--        쓴다 — 지금 severity(덮어쓴 값)를 과거로 복제하지 않는다.
--
--     운영 중 DB 는 database/logistics_exceptions_detection_history.sql 로 같은 칸을 더한다.
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE haetdeul.logistics_exceptions
    ADD COLUMN IF NOT EXISTS detection_history_json JSONB NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN haetdeul.logistics_exceptions.detection_history_json IS
    '[{as_of, severity}] 감지 이력 (LOG-AGENT-005). 하루 1원소 · 같은 날은 마지막 감지값으로 교체. 🔴 Historical 은 as_of <= 기준일 중 max(as_of) 원소의 severity 로 그날 우선도를 복원한다(배열 순서 비의존). severity 칸은 최신값 캐시로 그대로 두고, 이 칸이 날짜별 이력을 든다. 기존 행은 [] 이며 backfill 하지 않는다.';

-- 🔴 **조사와 대응안이 «같은 실행의» 문제만 가리키게 하려고** 둔다 (§3 · §4).
--    `exception_id` 가 이미 PK 라 유일성은 더 안 보태지만, 복합 FK 는 «유일한 칸 묶음»
--    만 가리킬 수 있어서 이 선언이 있어야 `(sim_run_id, exception_id)` 를 가리킬 수 있다.
DO $exception_axis$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'uq_logistics_exceptions_run_axis'
          AND conrelid = 'haetdeul.logistics_exceptions'::regclass
    ) THEN
        ALTER TABLE haetdeul.logistics_exceptions
            ADD CONSTRAINT uq_logistics_exceptions_run_axis
            UNIQUE (sim_run_id, exception_id);
    END IF;
END
$exception_axis$;

COMMENT ON COLUMN haetdeul.logistics_exceptions.proposed_as_of IS
    '이 문제에 대응안이 **처음** 선 시뮬레이션 영업일. 🔴 한 번 적히면 불변이다 — 제안이 거절되어 status 가 OPEN 으로 돌아가도 지우지 않는다(일어난 사실이다). ⚠️ 상태 이력 전체가 아니다: OPEN↔PROPOSED 를 여러 번 오간 자취는 이 칸으로 복원되지 않는다.';


-- ═══════════════════════════════════════════════════════════════════════════
-- §3  조사 실행 — "이 문제를 언제 조사했고 무엇으로 끝났나"  (#628 Commit 7)
--
--     🔴 **끝난 실행의 기록이다 — 상태표가 아니다.**
--
--     ```text
--     Exception      지금 참인 조건       상태가 오간다 (OPEN ↔ PROPOSED → RESOLVED)
--     Investigation  조사 **한 번**       🔴 한 번 적히면 안 바뀐다
--     Proposal       그 조사가 낸 대응안   상태가 오간다 (PROPOSED → APPROVED → …)
--     ```
--
--        그래서 이 표에는 상태 칸도 결정 칸도 없다. 다시 조사했으면 **새 행**이다
--        (`UPDATE result` · `UPDATE finish_reason` 을 하는 Production 코드가 없다).
--
--     🔴 **Tool 답 창고가 아니다.** `tool_trace_json` 은 *"무엇을 어떤 순서로 물었고
--        그 호출이 어떻게 끝났나"* 만 담는다 — Tool 이 낸 답 전체는 **안 들어온다.**
--        용량 문맥의 18일 창 · 품목 Lot 목록 · 예약 객체를 조사마다 통째로 복사하면
--        이 표가 감사 기록이 아니라 조회 캐시가 된다 (Commit 5 가 제안의 근거 칸에
--        내린 결정과 같다).
--
--     🔴 **같은 문제를 같은 날 두 번 조사할 수 있다.** 그래서
--        `(sim_run_id, exception_id, as_of)` 에 유일 제약을 **안 건다** — 두 번
--        조사했으면 두 번 실행한 것이고, 그 둘은 Tool 상황도 LLM 판단도 다를 수 있다.
--        «중복» 으로 접으면 나중 조사가 앞 조사를 덮어 실행 이력이 사라진다.
--
--     ⚠️ **기존 `logistics_agent_runs` 를 늘려 쓰지 않았다.** 저 표는 Logistics API 의
--        Request/Response 실행이력이다 — `cycle ∈ PROCUREMENT·SALES` ·
--        `request_payload`/`response_payload` · 쓰는 자리는 `app/logistics/service.py`
--        하나. 조사는 «Exception 하나 → Tool 조사 → 후보 action» 이라 축이 다르고,
--        `cycle` 에 `INVESTIGATION` 을 끼워 넣으면 그 표의 뜻이 깨진다.
--
--     ⚠️ **범용 `agent_runs` 도 못 쓴다.** ① `exception_id` 칸이 없어 §4 가 요구하는
--        «같은 실행 · 같은 문제» 복합 FK 의 대상이 될 수 없고, ② 상태 칸이
--        `run_status` 하나뿐이라 `finish_reason`(그래프가 어디서 끝났나)과
--        `llm_status`(AI 가 실제로 판단했나)를 한 칸에 뭉개야 하며, ③ **남의 표다** —
--        매입 제안과 검토가 FK 로 매달려 있다.
-- ═══════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS haetdeul.logistics_investigations (
    -- INV-{uuid}. 🔴 **번호를 세서 짓지 않는다** — `MAX(n)+1` 은 동시에 두 조사가
    --    시작하면 같은 이름을 낸다. 조사는 사람이 부를 때마다 서므로 채번이 경합한다.
    investigation_id      TEXT NOT NULL,
    -- 🔴 리셋 축. 이 칸이 있어서 `reset_sim_run_ledger` 가 표를 **자동으로 발견**해
    --    `--reset` 때 지운다 (`master/sim_run_open.py` `_axis_tables`).
    sim_run_id            TEXT NOT NULL,
    exception_id          TEXT NOT NULL,
    -- 시뮬레이션 영업일. 🔴 `created_at`(벽시계)과 다른 축이고, 조사 Runtime 이 받은
    --    `as_of` 그대로다 — `date.today()` · `created_at::date` 로 대체하지 않는다.
    as_of                 DATE NOT NULL,

    -- 🔴 **두 축을 한 칸에 안 담는다.** 그래야 *"규칙 제안인데 조사는 정상 종료"*
    --    같은 흔한 경우가 적힌다.
    --    finish_reason  그래프가 **어디서** 끝났나
    --    llm_status     AI 가 **실제로** 판단했나
    finish_reason         TEXT NOT NULL,
    llm_status            TEXT NOT NULL,
    -- 공급자 전송이 최종 실패한 원인 분류. 성공했거나 애초에 안 보냈으면 NULL 이다.
    llm_error_kind        TEXT,

    -- 🔴 조사가 낸 값 **그대로**. `None` 이면 NULL 이다 — `as_of` · `created_at` 으로
    --    메우지 않는다 (상세설계 §18 · 제안 표와 같은 규율).
    observed_as_of        DATE,

    -- [{sequence, tool_name, arguments, status, reason, detail, observed_as_of,
    --   uncertainties}, …]
    -- 🔴 **`answer` 키가 없다.** 이 칸이 답하는 것은 *"무엇을 시도했나"* 이지
    --    *"그 답이 무엇이었나"* 가 아니다.
    -- ⚠️ 제안의 `evidence_refs_json`(왜 이 대응안을 올렸나 · 핵심 facts)과 **다른
    --    칸이다.** 둘을 같은 payload 로 만들지 않는다.
    tool_trace_json       JSONB NOT NULL,
    -- 조사의 **최종 판단** — 요약 · 발견 · 못 본 것 · 후보와 그 영향 · 추천 번호.
    -- 🔴 여기서 숫자를 새로 만들지 않는다. `options[].impact` 는 Commit 3 의
    --    결정론 계산기가 낸 답 그대로이고, 저장하며 다시 셈하는 값은 하나도 없다.
    result_json           JSONB NOT NULL,

    -- 🔴 DB 벽시계. 감사용이고 **영업 판단에 쓰지 않는다.**
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT logistics_investigations_pkey PRIMARY KEY (investigation_id),
    -- 🔴 §4 의 복합 FK 가 가리킬 자리. PK 가 이미 유일성을 주지만 복합 FK 는
    --    «유일한 칸 묶음» 만 가리킬 수 있다.
    -- ⚠️ **이것이 «같은 날 한 번» 제약은 아니다.** `investigation_id` 가 묶음에 들어
    --    있어서 같은 문제를 같은 날 두 번 조사해도 둘 다 선다.
    CONSTRAINT uq_logistics_investigations_axis
        UNIQUE (sim_run_id, exception_id, investigation_id),

    CONSTRAINT logistics_investigations_sim_run_id_fkey
        FOREIGN KEY (sim_run_id) REFERENCES haetdeul.sim_runs(sim_run_id),
    -- 🔴 **조사는 «같은 실행의» 문제만 가리킨다.** 홑 FK 둘로는 «RUN-B 의 조사가
    --    RUN-A 의 문제를 가리키는» 조합을 못 막는다 — 각자 자기 칸만 보기 때문이다.
    CONSTRAINT logistics_investigations_exception_axis_fkey
        FOREIGN KEY (sim_run_id, exception_id)
        REFERENCES haetdeul.logistics_exceptions (sim_run_id, exception_id),

    -- 🔴 어휘는 닫혀 있다 — `FinishReason` · `LLMStatus` 와 **글자 그대로** 같다.
    CONSTRAINT ck_logistics_investigations_finish_reason
        CHECK (finish_reason IN ('FINISHED', 'NOT_FOUND', 'TOOL_FAILED',
                                 'BUDGET_EXCEEDED', 'GUARD_EXHAUSTED', 'TIMEOUT',
                                 'LLM_FAILED')),
    CONSTRAINT ck_logistics_investigations_llm_status
        CHECK (llm_status IN ('SUCCESS', 'SKIPPED_TEMPLATE', 'FALLBACK', 'DISABLED')),
    -- 🔴 «모른다» 를 빈 문자열로 적지 않는다. 안 실패했으면 NULL 이다.
    CONSTRAINT ck_logistics_investigations_llm_error_kind
        CHECK (llm_error_kind IS NULL OR length(btrim(llm_error_kind)) > 0),
    -- 🔴 **모양이 틀린 감사 기록은 행이 될 수 없다.** trace 는 배열, 결과는 객체다.
    CONSTRAINT ck_logistics_investigations_payload
        CHECK (jsonb_typeof(tool_trace_json) = 'array'
           AND jsonb_typeof(result_json) = 'object')
);

-- 🔴 **유일 인덱스가 아니다.** 한 문제를 며칠에 걸쳐 여러 번 조사한 이력을 그대로
--    읽기 위한 것이다 (같은 날 두 행도 정상이다).
CREATE INDEX IF NOT EXISTS idx_logistics_investigations_exception
    ON haetdeul.logistics_investigations (sim_run_id, exception_id, as_of);

CREATE INDEX IF NOT EXISTS idx_logistics_investigations_run_as_of
    ON haetdeul.logistics_investigations (sim_run_id, as_of);

COMMENT ON TABLE haetdeul.logistics_investigations IS
    '재고·물류 Agent 가 Exception 하나를 **실제로 조사한 한 번** (#628 Commit 7). 🔴 끝난 실행의 감사 기록이라 상태 칸도 결정 칸도 없고, 다시 조사하면 새 행이다. 🔴 Tool 답 전체를 담지 않는다 — tool_trace_json 은 "무엇을 어떤 순서로 물었나" 까지다. ⚠️ 기존 logistics_agent_runs(Logistics API Request/Response 이력)와 다른 축이고, 범용 agent_runs 는 exception_id 칸이 없어 대응안의 복합 FK 대상이 될 수 없다.';
COMMENT ON COLUMN haetdeul.logistics_investigations.tool_trace_json IS
    '[{sequence, tool_name, arguments, status, reason, detail, observed_as_of, uncertainties}, …]. 🔴 answer 키가 없다 — 이 칸은 "무엇을 시도했나" 이지 "그 답이 무엇이었나" 가 아니다. ⚠️ 제안의 evidence_refs_json(왜 승인 대상으로 올렸나)과 다른 칸이다.';
COMMENT ON COLUMN haetdeul.logistics_investigations.result_json IS
    '조사의 최종 판단 — 요약 · 발견 · 못 본 것 · 후보와 그 영향 · 추천 번호. 🔴 저장하며 다시 셈하는 숫자가 하나도 없다: options[].impact 는 Commit 3 의 결정론 계산기가 낸 답 그대로다. 🔴 raw LLM prompt/response · system prompt · 공급자 payload 는 담지 않는다.';
COMMENT ON COLUMN haetdeul.logistics_investigations.finish_reason IS
    '그래프가 **어디서** 끝났나 (FINISHED · NOT_FOUND · TOOL_FAILED · BUDGET_EXCEEDED · GUARD_EXHAUSTED · TIMEOUT · LLM_FAILED). ⚠️ llm_status(AI 가 실제로 판단했나)와 다른 축이다 — 한 칸에 뭉개지 않는다.';
COMMENT ON COLUMN haetdeul.logistics_investigations.observed_as_of IS
    '조사가 낸 값 그대로 — 근거 입력들이 알 수 있었던 가장 늦은 날. 🔴 하나라도 관측일이 없으면 NULL 이고 as_of · created_at 으로 메우지 않는다.';


-- ═══════════════════════════════════════════════════════════════════════════
-- §4  대응안 — "그 문제에 무엇을 할까" 한 줄  (#628 Commit 5)
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
    -- 🔴 **이 제안을 낸 조사** (§3 · Commit 7). 아래 복합 FK 가 «같은 실행 · 같은
    --    문제의» 조사만 가리키게 하고, 부분 유일 인덱스가 **한 조사 한 제안**을 지킨다.
    -- ⚠️ **FK 가 못 막는 것이 하나 있다.** 같은 문제를 두 번 조사하면 `INV-A` 와 `INV-B`
    --    가 나란히 서고 둘은 실행도 문제도 같다 — 그래서 «A 의 ID 에 B 의 결과» 를 매다는
    --    호출은 FK 를 통과한다. 그 자리는 응용(`proposal_service._check_investigation`)이
    --    저장된 조사 snapshot 과 넘어온 결과를 **대조해서** 막는다.
    -- ⚠️ **nullable 이다.** 손으로 세운 제안과 Commit 5 시절의 기존 행은 가리킬 조사가
    --    없다 — NOT NULL 로 막으면 그 행들이 통째로 불법이 된다. NULL 이면 복합 FK 도
    --    검사하지 않는다 (MATCH SIMPLE).
    -- 🔴 **지문(`proposal_key`)에는 안 들어간다.** 같은 안을 다시 조사해서 다시 올린
    --    것도 «같은 뜻의 제안» 이다 — 조사 ID 를 섞으면 지문이 늘 달라져 재시도를
    --    아무것도 못 막는다.
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
    -- [{sequence, tool_name, arguments, facts, observed_as_of, uncertainties}, …]
    -- 🔴 번호만 적지 않는다 — 조사 실행이 저장되지 않으므로 번호는 가리킬 곳이 없다.
    -- 🔴 **Tool 답 전체도 적지 않는다** — 제안은 조사 로그 저장소가 아니다.
    --    `facts` 는 승인 판단에 실제로 쓴 칸만 담는다 (상세설계 §10.4 ⑧).
    -- ⚠️ **실행 결과를 여기 섞지 않는다.** 이 칸은 *"왜 승인했나"* 이고, *"무엇이
    --    실행됐나"* 는 `execution_result_json` 이다.
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
    -- 🔴 **승인은 끝이 아니다** (Commit 6). `approved_as_of` 와 `executed_as_of` 가 한
    --    행에 함께 서는 것이 **정상 흐름**이다 — D5 제안 · D6 승인 · D8 실행.
    executed_as_of        DATE,
    failed_as_of          DATE,
    -- 🔴 조사 결과의 값을 **그대로** 옮긴다. 제안일·승인일·현재시각으로 메우지 않는다.
    observed_as_of        DATE,

    proposed_by           TEXT NOT NULL,
    approved_by           TEXT,
    rejected_by           TEXT,
    approval_note         TEXT,
    rejection_reason      TEXT,
    -- ⚠️ `decision_owner`(실행 책임 부서) 와 **다른 축이다.** 실제로 돌린 주체다.
    --    예: decision_owner=LOGISTICS · executed_by=master-runner.
    executed_by           TEXT,
    -- 무엇을 실행했고 어느 정본 행을 가리키나. 🔴 **업무 표 snapshot 이 아니다** —
    -- 실행 함수가 낸 authoritative 값만 담고, 여기서 숫자를 새로 만들지 않는다.
    execution_result_json JSONB,
    -- 🔴 **확정된 실패만.** «모른다» 를 실패로 적지 않는다 (상세설계 §10.5).
    failure_code          TEXT,
    failure_reason        TEXT,

    -- 이 제안이 대체한 이전 제안.
    previous_proposal_id  TEXT,

    -- 🔴 DB 벽시계. 감사용이고 **영업 판단에 쓰지 않는다.**
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT logistics_action_proposals_pkey PRIMARY KEY (proposal_id),
    -- 🔴 아래 자기참조 복합 FK 가 가리킬 자리. PK 가 이미 유일성을 주지만 복합 FK 는
    --    «유일한 칸 묶음» 만 가리킬 수 있다.
    CONSTRAINT uq_logistics_action_proposals_axis
        UNIQUE (sim_run_id, exception_id, proposal_id),

    CONSTRAINT logistics_action_proposals_sim_run_id_fkey
        FOREIGN KEY (sim_run_id) REFERENCES haetdeul.sim_runs(sim_run_id),
    CONSTRAINT logistics_action_proposals_exception_fkey
        FOREIGN KEY (exception_id)
        REFERENCES haetdeul.logistics_exceptions(exception_id),
    -- 🔴 **실행 축을 DB 가 지킨다.** 위의 홑 FK 만으로는 «RUN-B 의 제안이 RUN-A 의
    --    문제를 가리키는» 조합을 못 막는다 — 두 FK 가 각자 자기 칸만 보기 때문이다.
    --    응용이 이미 막고 있어도 장부의 불변식은 DB 에도 있어야 한다.
    CONSTRAINT logistics_action_proposals_exception_axis_fkey
        FOREIGN KEY (sim_run_id, exception_id)
        REFERENCES haetdeul.logistics_exceptions (sim_run_id, exception_id),
    -- 🔴 **제안은 «같은 실행 · 같은 문제를» 조사한 기록만 가리킨다** (Commit 7).
    --    없는 조사 ID · RUN-B 의 조사 · EX-B 의 조사를 전부 여기서 막는다.
    --    ⚠️ `investigation_id` 가 NULL 이면 검사하지 않는다 (MATCH SIMPLE) — 손으로
    --       세운 제안에는 가리킬 조사가 없다.
    CONSTRAINT logistics_action_proposals_investigation_axis_fkey
        FOREIGN KEY (sim_run_id, exception_id, investigation_id)
        REFERENCES haetdeul.logistics_investigations
                   (sim_run_id, exception_id, investigation_id),
    CONSTRAINT logistics_action_proposals_previous_fkey
        FOREIGN KEY (previous_proposal_id)
        REFERENCES haetdeul.logistics_action_proposals(proposal_id),
    -- 🔴 **대체는 같은 실행 · 같은 문제 안에서만.** `previous_proposal_id` 가 NULL 이면
    --    (MATCH SIMPLE) 검사하지 않는다 — 첫 제안은 가리킬 앞이 없다.
    CONSTRAINT logistics_action_proposals_previous_axis_fkey
        FOREIGN KEY (sim_run_id, exception_id, previous_proposal_id)
        REFERENCES haetdeul.logistics_action_proposals
                   (sim_run_id, exception_id, proposal_id),

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
    -- 🔴 **승인 provenance 는 실행 뒤에도 남는다** (Commit 6 보정). 위 제약은 상태가
    --    `APPROVED` 일 때만 보므로, 실행이 상태를 옮긴 순간 «누가 승인했나» 가 비어도
    --    DB 가 통과시켰다 — *"승인은 됐는데 누가 했는지 모른다"* 는 행이 설 수 있었다.
    --    ⚠️ 위 제약을 지우지 않고 **더한다** — 이 파일의 DROP 0 규율 그대로이고, 새
    --       제약이 더 강해 둘이 충돌하지 않는다.
    CONSTRAINT ck_logistics_action_proposals_approval_provenance
        CHECK (status NOT IN ('APPROVED', 'EXECUTED', 'FAILED')
               OR (approved_as_of IS NOT NULL AND approved_by IS NOT NULL)),
    CONSTRAINT ck_logistics_action_proposals_rejected
        CHECK (status <> 'REJECTED'
               OR (rejected_as_of IS NOT NULL AND rejected_by IS NOT NULL
                   AND rejection_reason IS NOT NULL)),
    CONSTRAINT ck_logistics_action_proposals_expired
        CHECK (status <> 'EXPIRED' OR expired_as_of IS NOT NULL),
    CONSTRAINT ck_logistics_action_proposals_superseded
        CHECK (status <> 'SUPERSEDED' OR superseded_as_of IS NOT NULL),

    -- 🔴 **«결정» 의 날은 하나다.** 승인도 거절도 만료도 대체도 한 제안에 한 번뿐이다.
    --    ★ **실행 날짜는 이 셈에 안 들어간다** (Commit 6). `executed_as_of` 는 결정이
    --      아니라 그 결정을 **수행한** 날이라, `approved_as_of` 와 함께 서는 것이 정상
    --      흐름이다 — 그래서 이 제약을 고칠 필요가 없었다(축이 다르다).
    --    ⚠️ APPROVED → SUPERSEDED 를 열어야 한다면 그때는 이 제약과 과거 재현의
    --       우선순위를 **함께** 다시 정해야 한다 — 한쪽만 풀면 조회가 조용히 틀린다.
    CONSTRAINT ck_logistics_action_proposals_single_terminal
        CHECK ((approved_as_of    IS NOT NULL)::int
             + (rejected_as_of    IS NOT NULL)::int
             + (expired_as_of     IS NOT NULL)::int
             + (superseded_as_of  IS NOT NULL)::int <= 1),

    -- ── 실행 축 (Commit 6) ────────────────────────────────────────────
    -- 🔴 **실행은 승인 뒤에만 있다.** 승인 없이 실행된 행은 «누가 진행해도 좋다고
    --    했나» 를 못 댄다.
    CONSTRAINT ck_logistics_action_proposals_executed
        CHECK (status <> 'EXECUTED'
               OR (approved_as_of IS NOT NULL AND executed_as_of IS NOT NULL
                   AND executed_by IS NOT NULL AND failed_as_of IS NULL)),
    CONSTRAINT ck_logistics_action_proposals_failed
        CHECK (status <> 'FAILED'
               OR (approved_as_of IS NOT NULL AND failed_as_of IS NOT NULL
                   AND failure_code IS NOT NULL AND executed_as_of IS NULL)),
    -- 🔴 **실행 날짜를 드는 상태는 둘뿐이다.** `APPROVED` 인데 실행일이 적혀 있으면
    --    그날 상태를 못 고른다.
    CONSTRAINT ck_logistics_action_proposals_execution_owner
        CHECK (status IN ('EXECUTED', 'FAILED')
               OR (executed_as_of IS NULL AND failed_as_of IS NULL
                   AND execution_result_json IS NULL AND failure_code IS NULL)),
    -- 성공과 실패가 한 행에 같이 설 수 없다.
    CONSTRAINT ck_logistics_action_proposals_execution_exclusive
        CHECK (executed_as_of IS NULL OR failed_as_of IS NULL),
    -- 승인보다 앞서 실행될 수 없다.
    CONSTRAINT ck_logistics_action_proposals_execution_order
        CHECK ((executed_as_of IS NULL OR executed_as_of >= approved_as_of)
           AND (failed_as_of   IS NULL OR failed_as_of   >= approved_as_of)),
    -- 🔴 사람·주체 칸을 빈 문자열로 채우지 않는다.
    CONSTRAINT ck_logistics_action_proposals_execution_actor
        CHECK ((executed_by   IS NULL OR length(btrim(executed_by)) > 0)
           AND (failure_code  IS NULL OR length(btrim(failure_code)) > 0)
           AND (execution_result_json IS NULL
                OR jsonb_typeof(execution_result_json) = 'object')),

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

-- 🔴 **한 조사가 낸 대응안은 하나다** (Commit 7 보정 · 상세설계 §11).
--    ⚠️ 일반 `UNIQUE (investigation_id)` 가 아니라 **부분** 유일 인덱스인 이유: 손으로
--       세운 제안은 `investigation_id` 가 NULL 이고 그런 행은 여럿이어야 한다. (PostgreSQL
--       의 UNIQUE 도 NULL 을 서로 다르게 보지만, 조건을 적어 두면 «NULL 은 예외» 가
--       우연이 아니라 **의도**라고 읽힌다.)
--
--    🔴 **끝난 제안도 자리를 계속 차지한다.** `previous_proposal_id` 처럼 지워지는 칸이
--       아니라, 제안이 `REJECTED` · `EXPIRED` · `SUPERSEDED` 가 돼도 «그 조사가 이 제안을
--       냈다» 는 사실은 남기 때문이다 — 그래서 거절된 뒤 같은 조사로 새 제안을 세우는
--       길이 여기서 닫힌다.
--
--    ⚠️ **기존 DB 에 이미 중복이 있으면 이 문장이 큰 소리로 실패한다. 그것이 맞다** —
--       조용히 하나를 NULL 로 만들거나 지우지 않는다. 먼저 이렇게 확인한다:
--
--       SELECT investigation_id, count(*)
--       FROM haetdeul.logistics_action_proposals
--       WHERE investigation_id IS NOT NULL
--       GROUP BY investigation_id HAVING count(*) > 1;
CREATE UNIQUE INDEX IF NOT EXISTS uq_logistics_action_proposals_investigation
    ON haetdeul.logistics_action_proposals (investigation_id)
    WHERE investigation_id IS NOT NULL;

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
COMMENT ON COLUMN haetdeul.logistics_action_proposals.executed_as_of IS
    '실제 실행이 **확인된** 시뮬레이션 영업일 (Commit 6). 🔴 approved_as_of 와 함께 서는 것이 정상 흐름이다 — 승인은 끝이 아니라 실행 대기다. ⚠️ 타 부서에 «요청을 접수시켰다» 는 것만으로는 적지 않는다(handoff accepted ≠ executed).';
COMMENT ON COLUMN haetdeul.logistics_action_proposals.failed_as_of IS
    '실행 실패가 **확정된** 날. 🔴 «실행됐는지 모른다» 를 여기 적지 않는다 — 모르는 것을 실패로 적으면 재시도가 이중 실행을 낳는다.';
COMMENT ON COLUMN haetdeul.logistics_action_proposals.executed_by IS
    '실제로 실행을 돌린 주체. ⚠️ decision_owner(실행 책임 부서)와 다른 축이다 — decision_owner=LOGISTICS 인데 executed_by=master-runner 일 수 있다.';
COMMENT ON COLUMN haetdeul.logistics_action_proposals.execution_result_json IS
    '무엇을 실행했고 어느 정본 행을 가리키나 (action · owner · reference_id · result 정도). 🔴 업무 표 snapshot 이 아니고, 여기서 숫자를 새로 만들지 않는다 — 실행 함수의 authoritative 반환이나 DB read-back 값만 담는다. ⚠️ evidence_refs_json(왜 승인했나)과 섞지 않는다.';
COMMENT ON COLUMN haetdeul.logistics_action_proposals.rationale IS
    '모델이 적은 이유 문장. 🔴 업무 사실이 아니라 기록이다 — 숫자와 판정의 주인은 impact_json 이다.';


-- ═══════════════════════════════════════════════════════════════════════════
-- §5  이미 표가 선 DB 를 §3 · §4 와 **같은 자리**로  (#628 Commit 5 close · Commit 7)
--
--     🔴 **`CREATE TABLE IF NOT EXISTS` 안의 제약은 기존 DB 에 안 닿는다.** 표가
--        있으면 그 문장은 통째로 건너뛰므로, 위에서 제약을 더해도 운영 DB 는 예전
--        모양 그대로다. 그래서 같은 제약을 여기서 한 번 더 — 멱등하게 — 건다.
--
--     ⚠️ **`NOT VALID` 를 쓰지 않는다.** `ADD CONSTRAINT` 는 기존 행을 전부 검사하고,
--        축이 어긋난 행이 있으면 **여기서 큰 소리로 실패한다.** 그것이 맞다 —
--        어긋난 장부를 조용히 통과시키고 «제약이 있다» 고 적는 것보다, 마이그레이션이
--        멈추고 사람이 그 행을 보는 편이 낫다.
-- ═══════════════════════════════════════════════════════════════════════════

-- ── 실행 축 칸 (Commit 6) — 기존 DB 에도 같은 모양으로 ────────────────────
ALTER TABLE haetdeul.logistics_action_proposals
    ADD COLUMN IF NOT EXISTS executed_as_of        DATE,
    ADD COLUMN IF NOT EXISTS failed_as_of          DATE,
    ADD COLUMN IF NOT EXISTS executed_by           TEXT,
    ADD COLUMN IF NOT EXISTS execution_result_json JSONB,
    ADD COLUMN IF NOT EXISTS failure_code          TEXT,
    ADD COLUMN IF NOT EXISTS failure_reason        TEXT;

DO $execution_axis$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_logistics_action_proposals_executed'
          AND conrelid = 'haetdeul.logistics_action_proposals'::regclass
    ) THEN
        ALTER TABLE haetdeul.logistics_action_proposals
            ADD CONSTRAINT ck_logistics_action_proposals_executed
            CHECK (status <> 'EXECUTED'
                   OR (approved_as_of IS NOT NULL AND executed_as_of IS NOT NULL
                       AND executed_by IS NOT NULL AND failed_as_of IS NULL));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_logistics_action_proposals_failed'
          AND conrelid = 'haetdeul.logistics_action_proposals'::regclass
    ) THEN
        ALTER TABLE haetdeul.logistics_action_proposals
            ADD CONSTRAINT ck_logistics_action_proposals_failed
            CHECK (status <> 'FAILED'
                   OR (approved_as_of IS NOT NULL AND failed_as_of IS NOT NULL
                       AND failure_code IS NOT NULL AND executed_as_of IS NULL));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_logistics_action_proposals_execution_owner'
          AND conrelid = 'haetdeul.logistics_action_proposals'::regclass
    ) THEN
        ALTER TABLE haetdeul.logistics_action_proposals
            ADD CONSTRAINT ck_logistics_action_proposals_execution_owner
            CHECK (status IN ('EXECUTED', 'FAILED')
                   OR (executed_as_of IS NULL AND failed_as_of IS NULL
                       AND execution_result_json IS NULL AND failure_code IS NULL));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_logistics_action_proposals_approval_provenance'
          AND conrelid = 'haetdeul.logistics_action_proposals'::regclass
    ) THEN
        ALTER TABLE haetdeul.logistics_action_proposals
            ADD CONSTRAINT ck_logistics_action_proposals_approval_provenance
            CHECK (status NOT IN ('APPROVED', 'EXECUTED', 'FAILED')
                   OR (approved_as_of IS NOT NULL AND approved_by IS NOT NULL));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_logistics_action_proposals_execution_exclusive'
          AND conrelid = 'haetdeul.logistics_action_proposals'::regclass
    ) THEN
        ALTER TABLE haetdeul.logistics_action_proposals
            ADD CONSTRAINT ck_logistics_action_proposals_execution_exclusive
            CHECK (executed_as_of IS NULL OR failed_as_of IS NULL);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_logistics_action_proposals_execution_order'
          AND conrelid = 'haetdeul.logistics_action_proposals'::regclass
    ) THEN
        ALTER TABLE haetdeul.logistics_action_proposals
            ADD CONSTRAINT ck_logistics_action_proposals_execution_order
            CHECK ((executed_as_of IS NULL OR executed_as_of >= approved_as_of)
               AND (failed_as_of   IS NULL OR failed_as_of   >= approved_as_of));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_logistics_action_proposals_execution_actor'
          AND conrelid = 'haetdeul.logistics_action_proposals'::regclass
    ) THEN
        ALTER TABLE haetdeul.logistics_action_proposals
            ADD CONSTRAINT ck_logistics_action_proposals_execution_actor
            CHECK ((executed_by   IS NULL OR length(btrim(executed_by)) > 0)
               AND (failure_code  IS NULL OR length(btrim(failure_code)) > 0)
               AND (execution_result_json IS NULL
                    OR jsonb_typeof(execution_result_json) = 'object'));
    END IF;
END
$execution_axis$;

DO $proposal_axis$
BEGIN
    -- 🔴 조사 축 FK (Commit 7). §3 의 표는 `CREATE TABLE IF NOT EXISTS` 로 방금 섰고,
    --    기존 DB 의 제안 표에는 이 제약이 없다.
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'logistics_action_proposals_investigation_axis_fkey'
          AND conrelid = 'haetdeul.logistics_action_proposals'::regclass
    ) THEN
        ALTER TABLE haetdeul.logistics_action_proposals
            ADD CONSTRAINT logistics_action_proposals_investigation_axis_fkey
            FOREIGN KEY (sim_run_id, exception_id, investigation_id)
            REFERENCES haetdeul.logistics_investigations
                       (sim_run_id, exception_id, investigation_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'uq_logistics_action_proposals_axis'
          AND conrelid = 'haetdeul.logistics_action_proposals'::regclass
    ) THEN
        ALTER TABLE haetdeul.logistics_action_proposals
            ADD CONSTRAINT uq_logistics_action_proposals_axis
            UNIQUE (sim_run_id, exception_id, proposal_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'logistics_action_proposals_exception_axis_fkey'
          AND conrelid = 'haetdeul.logistics_action_proposals'::regclass
    ) THEN
        ALTER TABLE haetdeul.logistics_action_proposals
            ADD CONSTRAINT logistics_action_proposals_exception_axis_fkey
            FOREIGN KEY (sim_run_id, exception_id)
            REFERENCES haetdeul.logistics_exceptions (sim_run_id, exception_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'logistics_action_proposals_previous_axis_fkey'
          AND conrelid = 'haetdeul.logistics_action_proposals'::regclass
    ) THEN
        ALTER TABLE haetdeul.logistics_action_proposals
            ADD CONSTRAINT logistics_action_proposals_previous_axis_fkey
            FOREIGN KEY (sim_run_id, exception_id, previous_proposal_id)
            REFERENCES haetdeul.logistics_action_proposals
                       (sim_run_id, exception_id, proposal_id);
    END IF;
END
$proposal_axis$;

COMMENT ON CONSTRAINT ck_logistics_action_proposals_approval_provenance
    ON haetdeul.logistics_action_proposals IS
    '🔴 승인에는 반드시 사람과 날이 있다 — **실행이 상태를 옮긴 뒤에도.** status 가 APPROVED 일 때만 보는 제약은 EXECUTED/FAILED 로 넘어간 순간 «누가 승인했나» 가 비어도 통과시킨다.';
COMMENT ON CONSTRAINT logistics_action_proposals_investigation_axis_fkey
    ON haetdeul.logistics_action_proposals IS
    '🔴 대응안은 **같은 실행 · 같은 문제를 조사한** 기록만 가리킨다 (Commit 7). 없는 조사 ID · RUN-B 의 조사 · EX-B 의 조사를 전부 막는다. ⚠️ investigation_id 가 NULL 이면 검사하지 않는다(MATCH SIMPLE) — 손으로 세운 제안에는 가리킬 조사가 없다. ⚠️ 같은 실행·같은 문제의 **다른 조사**는 이 FK 가 못 막는다 — 그 대조는 proposal_service._check_investigation 이 저장된 snapshot 으로 한다.';
COMMENT ON INDEX haetdeul.uq_logistics_action_proposals_investigation IS
    '🔴 한 조사가 낸 대응안은 **최대 하나다** (Investigation : Proposal = 1:0..1). 한 조사는 한 번 끝난 판단이고 그 판단이 고른 안은 하나다 — 새 안이 필요하면 새 조사를 돌린다. ⚠️ 끝난 제안(REJECTED·EXPIRED·SUPERSEDED)도 자리를 계속 차지한다. investigation_id 가 NULL 인 수동 제안은 여럿 허용된다.';
COMMENT ON CONSTRAINT logistics_action_proposals_exception_axis_fkey
    ON haetdeul.logistics_action_proposals IS
    '🔴 대응안은 **같은 실행의** 문제만 가리킨다. 홑 FK 둘(sim_run_id / exception_id)은 각자 자기 칸만 보므로 «RUN-B 의 제안이 RUN-A 의 문제를 가리키는» 조합을 못 막는다.';
COMMENT ON CONSTRAINT logistics_action_proposals_previous_axis_fkey
    ON haetdeul.logistics_action_proposals IS
    '🔴 대체는 같은 실행 · 같은 문제 안에서만. previous_proposal_id 가 NULL 이면 검사하지 않는다(MATCH SIMPLE) — 첫 제안은 가리킬 앞이 없다.';

COMMIT;
