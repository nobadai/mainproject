-- 물류 runtime fixture — `2026-01-02` 행
--
-- ══════════════════════════════════════════════════════════════════════════
-- 마스터 요청 `260904_물류앞_요청_2026_01_02_runtime_fixture_행` 의 답이다.
--
--   as_of=2026-01-02 로 물류 스냅샷을 못 읽어(`LookupError`) 이틀 관통의 Day2 가
--   서지 않는다. `repository.py` 가 `as_of = %s` **정확히 일치**로 고르므로 앞날
--   행으로 대신할 수 없고, 그 설계에는 이견이 없다 — *"그날 fixture 가 아직 없다"*
--   와 *"어제 것을 그냥 쓴다"* 를 섞지 않는 것이 맞다.
-- ══════════════════════════════════════════════════════════════════════════
--
-- 🔴 **이것은 임시방편이다.**
--
--   `register_transition("logistics", …)` 이 붙으면 Day1 승인이
--   `persist_inventory` 를 통해 Day2 행을 스스로 만든다 (마스터 회신 §1.2).
--   그때까지 관통이 막혀 있는 것을 푸는 한 행이다.
--
--   ⚠️ 날마다 필요한 행을 사람이 심는 방식은 **채택하지 않았다.** 사흘째마다
--      사람이 필요해진다. 규칙은 `persist` 가 만들고, 승인이 없는 날은 마스터
--      `open_day(as_of)` 가 연다 (마스터 회신 §1.2 · §1.4).
--
-- ══════════════════════════════════════════════════════════════════════════
-- 🔴 **`in_transit` 을 비워서 만든다** (마스터 요청 §4.2)
--
--   어제 승인이 만들 값을 손으로 넣으면 시뮬레이션이 아니라 각본이 된다.
--   행을 먼저 만들고 승인이 그것을 채운다.
--
-- 🔴 **`2026-01-01` 행을 본뜨지 않는다**
--
--   그 행의 note 가 이렇게 적혀 있다.
--
--     "Presentation-only resolved zone and lot-priority runtime fixture;
--      not operational data."
--
--   시연용으로 만든 행이라 사이클 사실로 굳히면 안 된다. 그리고 그 행의
--   `lot_priority` 배추 1건은 **그날의 판단**이라 옮기면 이틀 전 판단이 오늘
--   사실이 된다.
--
--   ⇒ **`2025-12-31` 행(직전 사이클 행)을 기준으로 삼는다.**
--
--   ⚠️ 이 차이가 carry-forward 규칙의 함정을 드러낸다 — *"전날 행"* 이 아니라
--      *"직전 사이클 행"* 이어야 한다. 달력상 전날인 01-01 은 시연 행이다.
--      `persist` 를 구현할 때 이 구분을 잊지 않는다.
-- ══════════════════════════════════════════════════════════════════════════
--
-- 값의 근거 — 물류 회신 `물류 마스터 회신 상태전이 미결 넷 2026-09-04` §1.1
--
--   evidence_grade = SIM_FIXED
--     이 등급이 근거를 대는 대상은 **사람이 심은 설정값**(zone_capacity ·
--     lot_priority · confirmed_*)이지 `in_transit` 이 아니다. 사이클이 그 칸을
--     채워도 등급의 뜻이 흔들리지 않는다.
--     그리고 HARD_ALLOWED_GRADES = {OFFICIAL, VENDOR, SIM_FIXED} 에서 빠지면
--     LOG-H01·LOG-H02 가 하드 제약으로 서지 못한다.
--
--   confirmed_inbound / outbound = CONFIRMED_ZERO · []
--     🔴 회피가 아니라 사실이다 — 01-02 도착 예정인 확정 입·출고가 실제로 없다.
--     ⚠️ UNRESOLVED 로 두면 `is_inbound_schedule_complete()` 가 거짓이 되어
--        `calculate_cap_by_date()` 가 IN_TRANSIT_SCHEDULE_UNRESOLVED 로 서고,
--        물류가 RUNTIME_NOT_READY 를 내 밴드가 안 선다. Day2 가 여기서 또 막힌다.
--
--   zone_capacity = UNRESOLVED · NULL
--     `guaranteed_capacity_by_zone_json` 은 kg 기반인데 페르소나 v0.5.2 가 Zone
--     물리정본을 Pallet Position 으로 바꿨다 (07 §6). 지금 CONFIRMED 로 심으면
--     곧 폐기할 값이 그날의 사실로 남는다.
--     ★ 관통에 지장이 없다 — 12-31 행이 UNRESOLVED 인 채로 Day1 이 세 품목
--       E1_APPROVED 를 냈다. (그리고 repository.py:332 가 이 값을 정책에서 읽지
--       않고 None 으로 두므로 지금 LOG-H02 는 애초에 검증을 못 하고 있다.)
--
--   lot_priority = CONFIRMED_ZERO · []
--     판단은 물려받지 않는다. 그리고 이 칸의 어휘가 바뀌는 중이다 —
--     페르소나 05 §7.1 이 sell_priority 로 재정의했고 기존
--     NEEDS_PRIORITY_SHIPMENT 와 병행 기간이라, 지금 값을 심으면 두 어휘가
--     한 행에 섞인다.
--
-- 선행: 없음 (`logistics_runtime_fixture` 는 `10_domain_schema.sql` 에 있다)
-- 멱등: 두 번 돌려도 같다.
-- ══════════════════════════════════════════════════════════════════════════

BEGIN;

SET search_path = haetdeul, public;


-- ── 기대상태 검사 ─────────────────────────────────────────────────────────

DO $$
DECLARE
    base_rows BIGINT;
BEGIN
    SELECT count(*) INTO base_rows
    FROM haetdeul.logistics_runtime_fixture
    WHERE as_of = DATE '2025-12-31'
      AND usage_scope = 'AGENT_MVP_DEMO'
      AND is_active;

    IF base_rows <> 1 THEN
        RAISE EXCEPTION
            '기준으로 삼을 2025-12-31 행이 1건이 아니라 %건이다.'
            ' sim_run_id 를 물려받을 곳이 없으면 이 행을 만들 수 없다.', base_rows;
    END IF;
END
$$;


-- ── 행 생성 — sim_run_id 는 12-31 행에서 물려받는다 ───────────────────────
--
-- ★ sim_run_id 를 문자열로 적지 않는다. 마스터가 그 값을 주지 않기로 했고
--   (회신 §3), 물류는 **직전 사이클 행에서 물려받아** 안다. 여기 손으로 적으면
--   실행이 여럿이 되는 날 이 파일만 옛 값을 들고 남는다.

INSERT INTO haetdeul.logistics_runtime_fixture (
    fixture_id, sim_run_id, as_of,
    in_transit_status,         in_transit_json,
    confirmed_inbound_status,  confirmed_inbound_json,
    confirmed_outbound_status, confirmed_outbound_json,
    lot_priority_status,       lot_priority_json,
    zone_capacity_status,      guaranteed_capacity_by_zone_json,
    usage_scope, evidence_grade, approved_by, source_ref, is_active, note
)
SELECT
    'LOG-RUNTIME-SIM-BURNIN-202512-20260102',
    base.sim_run_id,                       -- 물려받는다
    DATE '2026-01-02',
    'CONFIRMED_ZERO', '[]'::JSONB,         -- 🔴 비워 둔다 — 승인이 채운다
    'CONFIRMED_ZERO', '[]'::JSONB,
    'CONFIRMED_ZERO', '[]'::JSONB,
    'CONFIRMED_ZERO', '[]'::JSONB,
    'UNRESOLVED',     NULL,
    base.usage_scope,                      -- 물려받는다
    'SIM_FIXED', 'HUMAN',
    'MVP-DECISION-20260825:LOG-RUNTIME-CYCLE-DAY2',
    TRUE,
    '이틀 관통 Day2 행. 2025-12-31 행을 기준으로 만들었고 2026-01-01(시연 전용) 행은 '
    '본뜨지 않았다. in_transit 은 비어 있고 Day1 승인의 persist_inventory 가 채운다. '
    'register_transition 이 붙으면 이런 행은 persist 가 스스로 만든다 — 이 행은 그때까지의 '
    '임시방편이다.'
FROM haetdeul.logistics_runtime_fixture base
WHERE base.as_of = DATE '2025-12-31'
  AND base.usage_scope = 'AGENT_MVP_DEMO'
  AND base.is_active
ON CONFLICT (fixture_id) DO NOTHING;


COMMIT;


-- ── 확인 ──────────────────────────────────────────────────────────────────
--
-- SELECT as_of, fixture_id, sim_run_id, in_transit_status, confirmed_inbound_status,
--        confirmed_outbound_status, zone_capacity_status, lot_priority_status, evidence_grade
--   FROM haetdeul.logistics_runtime_fixture ORDER BY as_of;
--   기대: 3행 · 01-02 행의 sim_run_id = SIM-BURNIN-202512 · in_transit_status = CONFIRMED_ZERO
--
-- 그리고 코드에서:
--   get_current_inventory_logistics_snapshot(as_of=date(2026,1,2))  → LookupError 가 안 난다
--
--
-- ── 되돌리기 ──────────────────────────────────────────────────────────────
--
-- DELETE FROM haetdeul.logistics_runtime_fixture
--  WHERE fixture_id = 'LOG-RUNTIME-SIM-BURNIN-202512-20260102';
