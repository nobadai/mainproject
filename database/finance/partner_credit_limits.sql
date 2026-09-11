-- 거래처 여신한도 정본 (Finance / Sales Validation).
--
-- 🔴 **왜 새 표가 필요한가.** `partners` 에도 `agent_policy_config` 에도 여신한도
--    컬럼이 없다 (`app/finance/rules.py` 가 그 사실을 적어 두었다). 그래서
--    `FIN-SALES-CREDIT` · `FIN-SALES-AR-CAPACITY` 두 규칙이 항상 닫혀 있었고,
--    판매 재무검증이 `partner_credit_limit_krw` 미비로 RUNTIME_NOT_READY 가 됐다.
--
-- ★ **한도는 정책이 아니라 거래처가 소유한 사실**이다. 그래서 Finance Policy 가 아니라
--   거래처 축의 표에 둔다 — 실행마다 같은 값이 아니고 계약마다 다르다.
--
-- ★ **0 과 NULL 은 다르다.**
--     credit_limit_krw = 0   여신한도가 0원이라는 사실
--     행이 없음               여신한도가 아직 확정되지 않음
--   그래서 컬럼을 NOT NULL 로 두고, "모름" 은 행의 부재로만 표현한다. NULL 을 허용하면
--   두 뜻이 한 칸에 들어가고, 읽는 쪽이 매번 다시 정해야 한다.
--
-- ★ **기간 축이 있다.** 한도는 바뀐다. 과거 실행을 다시 돌릴 때 그날의 한도로 판정해야
--   백테스트가 성립한다 (`as_of` 규율).
--
-- ⚠️ 겹치는 활성 구간을 DB 가 막지는 않는다. `EXCLUDE USING gist` 는 `btree_gist`
--    확장이 필요해 공유 스키마에 확장을 더하지 않았다 — 대신 Finance loader 가
--    같은 `as_of` 를 덮는 활성 행이 둘이면 **고르지 않고 fail-closed** 한다.
--    임의로 하나를 집으면 어느 한도로 판정했는지 아무도 못 되짚는다.

CREATE TABLE IF NOT EXISTS haetdeul.partner_credit_limits (
    partner_credit_limit_id text NOT NULL,
    partner_id              text NOT NULL,
    credit_limit_krw        numeric(18,6) NOT NULL,
    currency                text NOT NULL DEFAULT 'KRW',
    effective_from          date NOT NULL,
    effective_to            date,
    evidence_grade          text NOT NULL,
    source_ref              text NOT NULL,
    policy_version          text NOT NULL,
    usage_scope             text NOT NULL,
    is_active               boolean NOT NULL DEFAULT true,
    note                    text,
    created_at              timestamp with time zone NOT NULL DEFAULT now(),
    updated_at              timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT partner_credit_limits_pkey PRIMARY KEY (partner_credit_limit_id),
    CONSTRAINT partner_credit_limits_partner_fkey
        FOREIGN KEY (partner_id) REFERENCES haetdeul.partners(partner_id),
    -- 한도는 음수일 수 없다. 0 은 유효한 사실이다.
    CONSTRAINT partner_credit_limits_amount_check
        CHECK (credit_limit_krw >= (0)::numeric),
    -- 끝이 있으면 시작보다 뒤여야 한다.
    CONSTRAINT partner_credit_limits_period_check
        CHECK (effective_to IS NULL OR effective_to >= effective_from),
    -- 근거 등급은 닫힌 어휘다. SIM_FIXED 는 **시뮬레이션 고정값**이라는 뜻이고
    -- 실제 계약 한도(VENDOR/OFFICIAL)와 섞이지 않는다.
    CONSTRAINT partner_credit_limits_grade_check
        CHECK (evidence_grade = ANY (ARRAY['OFFICIAL'::text, 'VENDOR'::text, 'SIM_FIXED'::text])),
    -- 한 거래처의 같은 시작일이 두 번 서지 않는다.
    CONSTRAINT partner_credit_limits_partner_from_key
        UNIQUE (partner_id, effective_from)
);

COMMENT ON TABLE haetdeul.partner_credit_limits IS
    '거래처별 여신한도 정본. 한도는 정책이 아니라 거래처가 소유한 사실이다. 행이 없으면 «미확정»이고 0 은 «한도 0원»이다.';

COMMENT ON COLUMN haetdeul.partner_credit_limits.credit_limit_krw IS
    '여신한도(원). 0 은 실제 0원이며 미확정이 아니다.';
COMMENT ON COLUMN haetdeul.partner_credit_limits.effective_to IS
    'NULL 이면 아직 종료되지 않은 구간.';
COMMENT ON COLUMN haetdeul.partner_credit_limits.evidence_grade IS
    'SIM_FIXED 는 MVP 시뮬레이션 고정값이며 실제 계약 한도가 아니다.';

-- as-of 조회 축. `(partner_id, as_of)` 로 활성 구간을 찾는다.
CREATE INDEX IF NOT EXISTS idx_partner_credit_limits_partner_period
    ON haetdeul.partner_credit_limits (partner_id, effective_from DESC)
    WHERE is_active;
