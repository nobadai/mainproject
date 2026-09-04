-- 재무 상태 as-of 조회 인덱스.
--
-- `app.finance.db.load_finance_state_row` 는 고정된 한 행이 아니라
--
--     같은 sim_run · 같은 financing_mode 안에서 state_date <= as_of 중 가장 늦은 행
--
-- 을 고른다. 그 조회 형태를 그대로 받는 인덱스다.
--
-- ★ 같은 인덱스가 `finance_current_state_view.sql` 의 View 도 받친다 — 그 View 는
--   축마다 `max(state_date)` 를 찾는다.
--
-- ★ **테이블 정의를 여기에 다시 적지 않는다.** `finance_states` 는 공유 스키마
--   (`database/10_domain_schema.sql`) 가 소유한다 — 옮겨 적으면 정본이 둘이 된다.
--   이 파일은 재무가 만든 조회에 재무가 붙이는 인덱스만 담는다.

CREATE INDEX IF NOT EXISTS ix_finance_states_as_of
    ON haetdeul.finance_states (sim_run_id, financing_mode, state_date DESC);
