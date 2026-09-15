-- 물류 LLM 업무 위험 임계 2종 — 선택 정책 Seed (물류 LLM 정책 결정서 §4)
--
-- ★ 선택 정책이다: 이 행이 없어도 물류는 정상 동작하며 해당 signal 판정만 SKIPPED
--   + soft_warning(CAPACITY_TIGHT_POLICY_UNRESOLVED 등)으로 표시된다.
-- ★ 값은 실업계 기준이 아니라 시뮬레이션·Agent 검증용 PROVISIONAL 운영값이다.
--   실측(고정 18일 창 사용률 분포 / inventory_lots 잔여비율 분포) 후 갱신한다.
-- ★ 실 스키마(database/10_domain_schema.sql) 대조로 NOT NULL 컬럼을 보정했다 (이슈 #101):
--   evidence_grade='SIM_FIXED' · approved_by='HUMAN' — 기존 물류 정책 8행의 관례와 동일.
--   source_ref 는 관례(MVP-DECISION-YYYYMMDD:<식별자>)에 맞춰 콜론 식별자를 붙였다.

INSERT INTO haetdeul.agent_policy_config (
    domain, policy_key, value_kind, value_numeric, value_text, value_json,
    evidence_grade, approved_by,
    source_ref, policy_version, usage_scope, is_active
)
SELECT 'logistics', 'capacity_tight_ratio', 'NUMERIC', 0.90, NULL, NULL,
       'SIM_FIXED', 'HUMAN',
       'MVP-DECISION-20260830:LLM-CAPACITY-TIGHT', 'v1.3-PROVISIONAL', 'AGENT_MVP_DEMO', TRUE
WHERE NOT EXISTS (
    -- 가드는 uq_agent_policy_version (policy_version, domain, policy_key) 과 같은
    -- 열로 건다 — usage_scope 를 추가로 걸면 scope 만 다른 기존 행이 있을 때
    -- 가드를 통과하고 유니크 위반으로 죽는다 (멱등 깨짐).
    SELECT 1 FROM haetdeul.agent_policy_config
    WHERE domain = 'logistics'
      AND policy_key = 'capacity_tight_ratio'
      AND policy_version = 'v1.3-PROVISIONAL'
);

INSERT INTO haetdeul.agent_policy_config (
    domain, policy_key, value_kind, value_numeric, value_text, value_json,
    evidence_grade, approved_by,
    source_ref, policy_version, usage_scope, is_active
)
SELECT 'logistics', 'freshness_pressure_ratio', 'NUMERIC', 0.30, NULL, NULL,
       'SIM_FIXED', 'HUMAN',
       'MVP-DECISION-20260830:LLM-FRESHNESS-PRESSURE', 'v1.3-PROVISIONAL', 'AGENT_MVP_DEMO', TRUE
WHERE NOT EXISTS (
    SELECT 1 FROM haetdeul.agent_policy_config
    WHERE domain = 'logistics'
      AND policy_key = 'freshness_pressure_ratio'
      AND policy_version = 'v1.3-PROVISIONAL'
);
