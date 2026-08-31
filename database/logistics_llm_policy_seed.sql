-- 물류 LLM 업무 위험 임계 2종 — 선택 정책 Seed (물류 LLM 정책 결정서 §4)
--
-- ★ 선택 정책이다: 이 행이 없어도 물류는 정상 동작하며 해당 signal 판정만 SKIPPED
--   + soft_warning(CAPACITY_TIGHT_POLICY_UNRESOLVED 등)으로 표시된다.
-- ★ 값은 실업계 기준이 아니라 시뮬레이션·Agent 검증용 PROVISIONAL 운영값이다.
--   실측(고정 18일 창 사용률 분포 / inventory_lots 잔여비율 분포) 후 갱신한다.
-- ★ 이 스크립트는 agent_policy_config 의 컬럼을 backend/app/logistics/repository.py 의
--   조회 계약 기준으로 작성했다 — 실제 테이블에 추가 NOT NULL 컬럼이 있으면 맞춰 보정할 것.

INSERT INTO haetdeul.agent_policy_config (
    domain, policy_key, value_kind, value_numeric, value_text, value_json,
    source_ref, policy_version, usage_scope, is_active
)
SELECT 'logistics', 'capacity_tight_ratio', 'NUMERIC', 0.90, NULL, NULL,
       'MVP-DECISION-20260830', 'v1.3-PROVISIONAL', 'AGENT_MVP_DEMO', TRUE
WHERE NOT EXISTS (
    SELECT 1 FROM haetdeul.agent_policy_config
    WHERE domain = 'logistics'
      AND policy_key = 'capacity_tight_ratio'
      AND policy_version = 'v1.3-PROVISIONAL'
      AND usage_scope = 'AGENT_MVP_DEMO'
);

INSERT INTO haetdeul.agent_policy_config (
    domain, policy_key, value_kind, value_numeric, value_text, value_json,
    source_ref, policy_version, usage_scope, is_active
)
SELECT 'logistics', 'freshness_pressure_ratio', 'NUMERIC', 0.30, NULL, NULL,
       'MVP-DECISION-20260830', 'v1.3-PROVISIONAL', 'AGENT_MVP_DEMO', TRUE
WHERE NOT EXISTS (
    SELECT 1 FROM haetdeul.agent_policy_config
    WHERE domain = 'logistics'
      AND policy_key = 'freshness_pressure_ratio'
      AND policy_version = 'v1.3-PROVISIONAL'
      AND usage_scope = 'AGENT_MVP_DEMO'
);
