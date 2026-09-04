-- 재무 현재 상태 View 교정 — **재무 소유 후속 마이그레이션.**
--
-- WHY
--   공유 기본 스키마(`database/10_domain_schema.sql`)가 만드는
--   `v_current_finance_state` 는 `finance_state_id = 'FIN-DAY30-LOAN'` 을 박아 둔다.
--   그래서 승인 상태전이가 2026-01-01 상태를 새로 넣어도 DB 는 계속 2025-12-31
--   한 행만 돌려주고, 다음 실행일 재무는 영원히 T0 만 본다.
--
-- WHAT
--   같은 View 를 **상태 ID 에 매이지 않은 선택 계약**으로 바꾼다.
--   축은 `sim_runs` 에서 조인해 온다 — 리터럴을 다른 리터럴로 바꾸지 않는다.
--
-- ORDER
--   `database/10_domain_schema.sql` **다음에** 적용한다. 기본 스키마가 만든 정의를
--   덮어쓰는 것이 이 파일의 일이다.
--
-- OWNERSHIP
--   View 의 의미는 재무가 소유한다. 공유 기본 스키마 파일은 이 브랜치에서 일부러
--   건드리지 않는다 — 재무 브랜치는 재무 파일만 바꾼다.
--
--
-- ★ **축은 `sim_runs` 가 준다.** `sim_run_id` 와 `financing_mode` 를 조인으로 가져온다.
--   그래서 무차입(`BASE_NO_LOAN`) 상태가 대출 baseline 자리에 섞이지 않는다 —
--   같은 날짜에 두 행이 실제로 있고(FIN-DAY30-BASE · FIN-DAY30-LOAN), 어느 쪽이
--   이 실행의 상태인지는 `sim_runs.financing_mode` 가 정한다. 재무가 고르지 않는다.
--
-- 🔴 **`sim_runs.as_of` 로 고르지 않는다.** 그 값은 시드된 뒤 아무도 전진시키지
--    않는다(`status = SEEDED`, 쓰는 코드 없음). 그것으로 고르면 상태 ID 대신
--    날짜가 박힌 것일 뿐 View 는 그대로 2025-12-31 에 멈춘다 — 같은 병을 이름만
--    바꿔 다시 앓는다.
--
-- 🔴 **동률을 몰래 하나로 줄이지 않는다.** `DISTINCT ON` · `LIMIT 1` 을 쓰지 않는다.
--    한 축에서 같은 날짜에 상태가 둘이면 그건 못 믿을 DB 상태이고, View 는 두 행을
--    그대로 보여 준다. 고르는 것을 거부하는 판단은 재무 런타임이 한다
--    (`load_finance_state_row` → `finance_state_ambiguous`). View 가 대신 골라 주면
--    그 잘못된 상태가 정상 응답으로 둔갑한다.
--
-- ★ **컬럼 계약은 그대로다.** 16개 컬럼의 이름·순서·타입을 유지한다 —
--   `CREATE OR REPLACE VIEW` 가 그것을 요구하고, 공유 `v_dashboard_state` 가 이 View 를
--   `CROSS JOIN` 해서 읽는다. `DROP ... CASCADE` 를 쓰지 않는다: 그러면 남의 도메인
--   View 가 같이 지워진다.
--
-- ★ **`finance_states` 를 다시 정의하지 않는다.** 표의 정본은 공유 스키마다.

CREATE OR REPLACE VIEW haetdeul.v_current_finance_state AS
SELECT
    fs.finance_state_id,
    fs.sim_run_id,
    fs.state_date,
    fs.state_type,
    fs.financing_mode,
    fs.current_cash_krw,
    fs.minimum_operating_cash_krw,
    fs.committed_outflows_krw,
    fs.unsettled_purchase_payables_krw,
    fs.receivables_krw,
    fs.inventory_book_value_krw,
    fs.operational_inventory_value_krw,
    fs.current_debt_krw,
    fs.recommended_loan_amount_krw,
    fs.financial_limit_krw,
    fs.note
FROM haetdeul.finance_states AS fs
JOIN haetdeul.sim_runs AS sr
  ON sr.sim_run_id = fs.sim_run_id
 AND sr.financing_mode = fs.financing_mode
WHERE fs.state_date = (
    SELECT max(latest.state_date)
    FROM haetdeul.finance_states AS latest
    WHERE latest.sim_run_id = fs.sim_run_id
      AND latest.financing_mode = fs.financing_mode
);

COMMENT ON VIEW haetdeul.v_current_finance_state IS
    'sim_runs 가 정한 축(sim_run_id · financing_mode)에서 가장 늦은 재무 상태. '
    '특정 finance_state_id 에 매이지 않는다 — 새 상태가 들어오면 그것이 현재가 된다. '
    '과거 시점 조회는 이 View 가 아니라 as-of 질의가 한다.';
