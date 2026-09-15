-- `finance_agent_runs_v22.mode` 에 `SALES_VALIDATION` 을 허용한다.
--
-- ★ **이미 만들어진 DB 에만 쓴다.** 신규 구축은 `finance_agent_runs_v22.sql` 하나로
--   선다 (그 파일의 CHECK 에 이미 SALES_VALIDATION 이 들어 있다).
--
-- 왜 따로 필요한가
--   본 DDL 은 `CREATE TABLE IF NOT EXISTS` 라서, 표가 이미 있는 DB 에서는 통째로
--   건너뛴다. 즉 **CHECK 는 옛 상태로 남는다.** 그 상태에서 판매 검증 실행을 저장하면
--   전부 CHECK 위반으로 실패한다.
--
-- ★ **표를 다시 만들지 않는다.** DROP/TRUNCATE/DELETE 를 쓰지 않고 제약만 바꾼다.
--   행도 열도 그대로다 — 공유 DB 를 파괴적으로 바꿀 수 있다고 가정하지 않는다.
--
-- ★ **여러 번 실행해도 안전하다.** 제약을 이름으로 찾아 지우고 다시 만든다.
--   두 번째 실행은 첫 번째와 같은 상태로 끝난다.
--
-- ★ 제약 이름을 박아 두지 않는다. PostgreSQL 기본 이름은
--   `finance_agent_runs_v22_mode_check` 지만, 손으로 만든 DB 에서는 다를 수 있다.
--   그래서 **`mode` 컬럼만 참조하는 CHECK** 를 찾아서 지운다 — 다른 컬럼이 섞인
--   제약은 건드리지 않으므로 관계없는 검사를 실수로 날리지 않는다.
--
-- 실행
--   psql -v ON_ERROR_STOP=1 -f database/finance_agent_runs_v22_sales_validation.sql
--
-- 되돌리기
--   아래 IN 목록에서 'SALES_VALIDATION' 을 빼고 다시 실행한다. 단, 그 값으로 저장된
--   행이 이미 있으면 되돌릴 수 없다 (그 행을 지워야 하는데, 지우지 않는다).

BEGIN;

DO $$
DECLARE
    target_constraint TEXT;
BEGIN
    -- 표가 없으면 신규 구축 경로다. 여기서 만들지 않는다.
    IF to_regclass('haetdeul.finance_agent_runs_v22') IS NULL THEN
        RAISE NOTICE 'haetdeul.finance_agent_runs_v22 가 없다 — 신규 구축은 본 DDL 을 쓴다.';
        RETURN;
    END IF;

    -- `mode` 컬럼 **하나만** 참조하는 CHECK 제약을 전부 지운다.
    -- 이름이 무엇이든 찾아내고, 여러 컬럼이 걸린 제약은 건드리지 않는다.
    FOR target_constraint IN
        SELECT con.conname
        FROM pg_constraint AS con
        JOIN pg_class AS rel ON rel.oid = con.conrelid
        JOIN pg_namespace AS nsp ON nsp.oid = rel.relnamespace
        WHERE nsp.nspname = 'haetdeul'
          AND rel.relname = 'finance_agent_runs_v22'
          AND con.contype = 'c'
          AND con.conkey = ARRAY[
              (
                  SELECT att.attnum
                  FROM pg_attribute AS att
                  WHERE att.attrelid = rel.oid
                    AND att.attname = 'mode'
                    AND NOT att.attisdropped
              )
          ]
    LOOP
        EXECUTE format(
            'ALTER TABLE haetdeul.finance_agent_runs_v22 DROP CONSTRAINT %I',
            target_constraint
        );
    END LOOP;

    -- 새 제약을 단다. 기존 허용값은 그대로 두고 SALES_VALIDATION 만 더한다.
    ALTER TABLE haetdeul.finance_agent_runs_v22
        ADD CONSTRAINT finance_agent_runs_v22_mode_check
        CHECK (mode IN ('PRE_PURCHASE', 'SCENARIO_VALIDATION', 'SALES_VALIDATION'));
END
$$;

COMMIT;
