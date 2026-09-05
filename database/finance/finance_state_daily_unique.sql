-- Finance daily-state invariant — one row per runtime axis and calendar date.
--
-- Apply after database/10_domain_schema.sql and before runtime transition/day_open traffic.
-- This migration never repairs or deletes data. Existing duplicates are an accounting-state
-- ambiguity and must be investigated, so the preflight raises before creating the index.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM haetdeul.finance_states
        GROUP BY sim_run_id, financing_mode, state_date
        HAVING COUNT(*) > 1
    ) THEN
        RAISE EXCEPTION
            'finance_states has duplicate (sim_run_id, financing_mode, state_date) rows; '
            'investigate before applying uq_finance_states_axis_date';
    END IF;
END
$$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_finance_states_axis_date
    ON haetdeul.finance_states (sim_run_id, financing_mode, state_date);
