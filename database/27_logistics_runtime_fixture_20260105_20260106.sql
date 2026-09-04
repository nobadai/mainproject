-- 물류 runtime fixture - 관통 실행일 쌍 `2026-01-05` · `2026-01-06`
--
-- ══════════════════════════════════════════════════════════════════════════
-- 왜 이 두 날인가
--
--   마스터가 관통 날짜를 옮겼다 (2026-09-04 회신 §1.2).
--
--   ```text
--   as_of        요일  ML배치  경락가   fixture
--   2025-12-31   Wed   있음    있음     있음
--   2026-01-01   Thu   없음    없음     있음     ← 신정. 실행일이 아니다
--   2026-01-02   Fri   있음    있음     있음
--   2026-01-05   Mon   있음    있음     🔴 없음
--   2026-01-06   Tue   있음    있음     🔴 없음
--   ```
--
--   `#256` 이 `target_state_date` 를 **달력 다음 날**로 정해서, 관통을 증명하려면
--   달력으로 붙어 있는 실행일 쌍이 필요하다.
--
--   ```text
--   01-05 승인 → target_state_date = 01-06 에 쓴다
--   Day2       → 01-06 을 읽는다                    같은 행
--   ```
--
--   ⚠️ `12-31 → 01-02` 로는 안 된다. 01-01 이 신정이라 승인이 `01-01` 행에 쓰이고
--      Day2 는 `01-02` 행을 읽는다 - **다른 행**이다. 그 구간은 `open_day` 가
--      맡는다 (마스터 회신 §1.4). `01-02` 행은 그 증명의 도착점이라 **안 지운다.**
--
-- 무엇을 물려받고 무엇을 새로 두나 (마스터 회신 §2)
--
--   ```text
--   물려받는다      sim_run_id · usage_scope · confirmed_inbound/outbound
--                   zone_capacity · evidence_grade · approved_by
--   새로 둔다       as_of · fixture_id · source_ref · in_transit
--   ```
--
-- 🔴 **두 행의 `in_transit` 이 다르다** (마스터 통보 2026-09-04 §2)
--
--   ```text
--   01-05   CONFIRMED_ZERO · []      Day1 이 읽을 T0. 확인했고 입고 예정이 없다
--   01-06   UNRESOLVED     · NULL    전이가 UPDATE 할 그릇. 아직 아무도 확인하지 않았다
--   ```
--
--   `CONFIRMED_ZERO` 는 *"없다"* 가 아니라 *"확인했고 없다"* 다. `01-06` 을 그렇게
--   심으면 **전이가 실패해도 물류가 초록으로 답한다** - 재무는 행 자체가 없어
--   시끄러운데(INSERT) 물류만 조용해진다(씨앗 + UPDATE). Day2 가 *"입고 예정 없음"*
--   을 사실로 받아 판단을 계속한다.
--
--   ⚠️ **`in_transit_json` 도 함께 `NULL` 이어야 한다.** `schemas.py` 의
--      `LogisticsRuntimeFixture` 가 `UNRESOLVED` 에 `[]` 를 붙이면 거부하고
--      (`in_transit UNRESOLVED must preserve None`), 그 행은 읽히지 않아 어댑터가
--      `RUNTIME_NOT_READY` 가 아니라 **`ERROR`** 로 선다. 그리고 판정을 만드는 것은
--      상태 문자열이 아니라 `in_transit is None` 쪽이다 - 규칙 두 곳 모두 그것만 본다.
--
--   ★ 전이가 성공하면 `persist_inventory` 가 두 칸을 함께 덮어써 짝이 맞는다.
--
--   ★ `sim_run_id` 를 문자열로 적지 않는다. 마스터가 그 값을 주지 않기로 했고
--     (회신 §3), 물류는 직전 사이클 행에서 물려받아 안다. 손으로 적으면 실행이
--     여럿이 되는 날 이 파일만 옛 값을 들고 남는다.
--
--   ★ `zone_capacity_status` 도 적지 않고 물려받는다. 창고 구조는 그 사이에
--     바뀌지 않았고, 여기서 `CONFIRMED` 로 적으면 **없던 확정이 생긴다.**
--
-- 선행: `2025-12-31` fixture 행. 이 파일은 그 행에서 `sim_run_id` 와 설정값을
--       물려받으므로 없으면 위 검사가 막는다.
--
--       🔴 **그 행을 만드는 SQL 이 이 저장소에 아직 없다** (2026-09-04 실측).
--          `12-31` · `01-01` 두 행은 어느 씨앗 파일에도 없고 실 DB 에만 있다.
--          그래서 빈 DB 에서는 이 파일만으로 관통 상태를 세울 수 없다.
--          `01-01` 은 note 가 시연 전용이라 적힌 행이라, 두 행을 씨앗으로 옮길지는
--          물류가 따로 판단한다 - 여기서 정하지 않는다.
--
-- 멱등: `fixture_id` 로 막는다. 두 번 돌려도 같다.
--       ⚠️ `DO NOTHING` 이라 **이미 있는 행의 값은 고치지 않는다.** 실 DB 행이
--          이 파일과 어긋나면 그 어긋남은 따로 다뤄야 한다.
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
            ' 물려받을 곳이 없으면 이 행들을 만들 수 없다.', base_rows;
    END IF;
END
$$;


-- ── 두 행 생성 ────────────────────────────────────────────────────────────

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
    'LOG-RUNTIME-' || base.sim_run_id || '-' || to_char(target.as_of, 'YYYYMMDD'),
    base.sim_run_id,
    target.as_of,
    -- 🔴 두 행이 다르다 - 위 머리말 참조. 승인의 `persist_inventory` 가 채우기
    --    전까지, 01-05 는 확인된 0 이고 01-06 은 아직 확인한 적이 없다.
    target.in_transit_status, target.in_transit_json,
    base.confirmed_inbound_status,  base.confirmed_inbound_json,
    base.confirmed_outbound_status, base.confirmed_outbound_json,
    -- lot_priority 는 판단이라 물려받지 않는다 (마스터 회신 §2).
    'CONFIRMED_ZERO', '[]'::JSONB,
    base.zone_capacity_status,      base.guaranteed_capacity_by_zone_json,
    base.usage_scope,
    base.evidence_grade, base.approved_by,
    target.source_ref,
    TRUE,
    target.note
FROM haetdeul.logistics_runtime_fixture base
CROSS JOIN (
    VALUES
        (
            DATE '2026-01-05',
            -- Day1 이 읽을 T0. 확인했고 입고 예정이 없다 - 참말이다.
            'CONFIRMED_ZERO', '[]'::JSONB,
            'MASTER-DECISION-20260904:THROUGHPUT-D1',
            '관통 Day1. 마스터가 관통 날짜를 12-31→01-05 로 옮겼다 (회신 §1.2). '
            '이 날 승인이 01-06 행의 in_transit 을 채운다.'
        ),
        (
            DATE '2026-01-06',
            -- 🔴 전이가 UPDATE 할 그릇. 아직 아무도 확인하지 않았다.
            'UNRESOLVED', NULL::JSONB,
            'MASTER-DECISION-20260904:THROUGHPUT-D2',
            '관통 Day2. 01-05 승인의 target_state_date 가 이 날이다. in_transit 은 '
            'UNRESOLVED 로 둔다 - 전이가 실패하면 그대로 남아 missing_data 로 나가고, '
            '성공하면 persist_inventory 가 덮어쓴다. '
            'register_transition 이 붙고 open_day 가 생기면 이런 행은 전이가 스스로 '
            '만든다 - 이 행은 그때까지의 임시방편이다.'
        )
) AS target(as_of, in_transit_status, in_transit_json, source_ref, note)
WHERE base.as_of = DATE '2025-12-31'
  AND base.usage_scope = 'AGENT_MVP_DEMO'
  AND base.is_active
ON CONFLICT (fixture_id) DO NOTHING;


COMMIT;


-- ── 확인 ──────────────────────────────────────────────────────────────────
--
-- SELECT as_of, in_transit_status, zone_capacity_status, sim_run_id, source_ref
--   FROM haetdeul.logistics_runtime_fixture
--  WHERE usage_scope = 'AGENT_MVP_DEMO' AND is_active
--  ORDER BY as_of;
--   기대: 5행 · 01-05 와 01-06 이 12-31 과 같은 sim_run_id
--         01-05 in_transit_status = CONFIRMED_ZERO · 01-06 = UNRESOLVED
--
-- 그리고 코드에서 (전이가 돌기 전 기준):
--   as_of=2026-01-05  PRE_PURCHASE → READY
--   as_of=2026-01-06  PRE_PURCHASE → RUNTIME_NOT_READY
--                     missing_data 에 logistics_rule/IN_TRANSIT_SCHEDULE_UNRESOLVED
--   ⚠️ 01-06 이 ERROR 로 서면 in_transit_json 이 NULL 이 아니다.
--
--
-- ── 되돌리기 ──────────────────────────────────────────────────────────────
--
-- DELETE FROM haetdeul.logistics_runtime_fixture
--  WHERE as_of IN (DATE '2026-01-05', DATE '2026-01-06')
--    AND usage_scope = 'AGENT_MVP_DEMO';
