-- 2026-01-06 상태전이 증명 그릇 — **물류 소유 SIM_FIXED fixture 정정.**
--
-- WHY
--   `target_state_date = commitment.as_of + 1 달력일` 이라 2026-01-05 승인은
--   2026-01-06 행에 쓴다. 그런데 물류 전이는 `UPDATE` 뿐이라(`app/logistics/
--   transition.py`) 그날 행이 **미리 있어야** 하고, 실제로 심겨 있다.
--
--   문제는 심긴 값이다. `in_transit_status = 'CONFIRMED_ZERO'` 는 *"확인했고 입고
--   예정이 없다"* 는 뜻인데, 2026-01-06 은 **아직 아무도 확인한 적이 없는 그릇**이다.
--   그래서 전이가 실패해도 물류는 초록으로 답한다 — 재무는 행 자체가 없어 시끄럽고
--   물류만 조용한 비대칭이 생긴다 (재무 행은 INSERT, 물류 행은 씨앗 + UPDATE).
--
-- WHAT
--   그 행의 **두 칸**만 바꾼다.
--
--     in_transit_status   CONFIRMED_ZERO -> UNRESOLVED
--     in_transit_json     []             -> NULL
--
-- 🔴 **두 칸을 반드시 함께 바꾼다.** `schemas.py` 의 `LogisticsRuntimeFixture` 가
--    `UNRESOLVED` 에 `NULL` 이 아닌 값을 붙이면 거부한다
--    (`in_transit UNRESOLVED must preserve None`). status 만 바꾸면 그 행은 읽히지
--    않고 어댑터가 `RUNTIME_NOT_READY` 가 아니라 **`ERROR`** 로 선다.
--
-- 🔴 **판정을 만드는 값은 `NULL` 쪽이다.** 규칙은 `in_transit is None` 만 본다
--    (`tools.py` B-1 · `rules.py` soft warning). `in_transit_status` 는 Repository
--    검증까지만 살아 있는 라벨이다.
--
-- 🔴 **2026-01-05 는 건드리지 않는다.** Day1 이 읽을 T0 이고 *"확인했고 입고 예정이
--    없다"* 가 그 행에서는 참말이다.
--
-- ★ 나머지 칸(`confirmed_inbound_*` · `confirmed_outbound_*` · `evidence_grade` ·
--   `approved_by` · `source_ref`)은 다른 사실이고 다른 근거를 갖는다 — 같은 UPDATE 에
--   얹지 않는다.
-- ★ 전이가 성공하면 이 값은 스스로 지워진다. `persist_inventory` 가
--   `CONFIRMED`/`CONFIRMED_ZERO` 와 `in_transit_json` 을 함께 덮어써 짝이 맞는다.
--
-- ORDER
--   `database/10_domain_schema.sql` 및 해당 fixture 씨앗 다음.
--
-- 재실행 안전: 조건에 씨앗의 `source_ref` 를 넣어 **전이가 이미 쓴 행은 되돌리지
-- 않는다.** `persist_inventory` 가 `source_ref` 를 자기 값으로 덮으므로, 전이가 돈
-- 뒤에는 이 UPDATE 가 0행이다.

UPDATE haetdeul.logistics_runtime_fixture
SET in_transit_status = 'UNRESOLVED',
    in_transit_json = NULL,
    updated_at = NOW()
WHERE sim_run_id = 'SIM-BURNIN-202512'
  AND as_of = DATE '2026-01-06'
  AND usage_scope = 'AGENT_MVP_DEMO'
  AND source_ref = 'MASTER-DECISION-20260904:THROUGHPUT-D2';
