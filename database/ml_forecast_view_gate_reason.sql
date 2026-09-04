-- v_ml_price_forecast 의 daily[] 에 gate_reason 을 싣는다 (2026-09-03)
--
-- 🔴 왜
--   표(ml_price_forecasts)에는 gate_reason 이 있는데 뷰가 daily 에 안 넣었다.
--   받는 쪽은 "이 행을 쓰지 말라"(is_gated)는 알아도 "왜"를 못 봤다.
--
--   실측 2026-09-03
--     is_gated 이면서 gate_reason 이 있는 행    326
--     is_gated 인데 gate_reason 이 NULL 인 행     0
--
--   AUC 는 사유가 lead_time 하나라 지금은 추론이 되지만, WHSL 은 셋이라
--   (lead_time · quality · lead_time+quality) 추론이 안 된다.
--   WHSL 을 쓰기 시작하는 날 물린다.
--
-- ★ 순수 추가다. 기존 키가 안 바뀌고 안 없어진다.
--   이 뷰를 읽는 곳은 app/master/inputs.py:126 하나이고, 마스터는 daily 를
--   손대지 않고 그대로 나른다.
--
-- ⚠️ 같은 변경이 두 곳에 있다 — 본 DDL(10_domain_schema.sql)과 이 ALTER 판.
--   tests/master/test_schema_files_agree.py 가 둘이 갈리면 운다.
--
-- 되돌리기
--   이 파일에서 ", 'gate_reason', gate_reason" 만 빼고 다시 실행한다.

CREATE OR REPLACE VIEW haetdeul.v_ml_price_forecast AS
 SELECT base_dt AS as_of,
    item_nm AS item,
    target_kind,
    to_char((min(generated_at) AT TIME ZONE 'Asia/Seoul'::text), 'YYYY-MM-DD"T"HH24:MI:SS+09:00'::text) AS generated_at,
    min(unit) AS unit,
    min(current_price) AS current_price,
    (count(*))::integer AS horizon_days,
    min(model_version) AS model_version,
    jsonb_agg(jsonb_build_object('date', to_char((target_dt)::timestamp with time zone, 'YYYY-MM-DD'::text), 'predicted', predicted, 'lower', lower, 'upper', upper, 'is_filled', is_filled, 'is_gated', is_gated, 'gate_reason', gate_reason) ORDER BY offset_days) AS daily,
    bool_or(is_filled) AS has_filled_rows,
    (count(*) FILTER (WHERE is_filled))::integer AS filled_count,
    bool_and(COALESCE(use_recommended, true)) AS use_recommended,
    min(quality_note) AS quality_note,
    min(grade_name) AS grade_name,
    min(spec_desc) AS spec_desc
   FROM haetdeul.ml_price_forecasts f
  GROUP BY base_dt, item_nm, target_kind;
