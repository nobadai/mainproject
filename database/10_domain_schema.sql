-- 도메인 표 · 뷰 — 살아 있는 test DB 에서 뜬 스냅샷 (2026-08-30)
--
-- ══════════════════════════════════════════════════════════════════════════
-- 🔴 **이 파일은 사람이 쓴 것이 아니라 pg_dump 로 뜬 것이다.**
--
--   `database/` 의 다른 파일들은 파트가 직접 쓴 DDL 이고, 왜 그렇게 만들었는지가
--   주석에 남아 있다. 이 파일에는 그게 없다 — **오늘 test DB 가 이런 모양이더라**
--   가 전부다.
--
--   본 DB 를 세우기 전에 **각 파트가 자기 표를 확인해야 한다.** 특히:
--     · 지금 안 쓰는 표가 섞여 있을 수 있다 (`agent_runs` · `sim_runs` ·
--       `constraint_reviews` · `finance_agent_runs_v22` 등 — 이름이 겹치거나 옛것)
--     · CHECK · DEFAULT 가 test DB 에서 손으로 바뀐 것이 있으면 그대로 따라온다
--     · 시드 데이터는 들어 있지 않다 (스키마만)
-- ══════════════════════════════════════════════════════════════════════════
--
-- ★ 왜 이 파일이 필요한가
--   `database/` 에 DDL 이 있는 것은 **6개뿐**이었다 (각 파트의 `*_agent_runs` 와
--   `master_decisions`). 나머지 32표·8뷰 — `items` · `partners` · `sales` ·
--   `inventory_lots` · `item_storage_policies` · `ml_price_forecasts` … 는
--   저장소에 없었다. **본 DB 를 이 저장소만으로 세울 수 없는 상태였다.**
--
-- ★ 소유
--   여기 담긴 표는 마스터 파트 것이 아니다. 마스터가 책임지는 것은
--   `master_decisions` 하나이고, `orchestrator_agent_runs` 를 공유해 쓴다.
--   이 파일은 **빈 자리를 메우려고 뜬 것**이지 마스터가 설계한 것이 아니다.
--
-- ★ 실행 순서는 `database/README.md` 를 본다.
--   `00_init_schema.sql` 다음, 각 파트의 `*_agent_runs.sql` 앞이다.

--
-- PostgreSQL database dump
--

\restrict Dp8ylbnM0ZMqDiePndhCz0JGMhE5efg3O7zdJq3b0elL1tFA7Z8szCyzeMyeSmR

-- Dumped from database version 17.10 (Debian 17.10-1.pgdg13+1)
-- Dumped by pg_dump version 17.11

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: haetdeul; Type: SCHEMA; Schema: -; Owner: -
--

-- 00_init_schema.sql 이 이미 만든다. pg_dump 가 넣은 것을 IF NOT EXISTS 로
-- 바꿨다 — 원문 그대로면 순서대로 돌릴 때 'already exists' 로 멈춘다.
CREATE SCHEMA IF NOT EXISTS haetdeul;


--
-- Name: f_ml_forecast_archive(); Type: FUNCTION; Schema: haetdeul; Owner: -
--

CREATE FUNCTION haetdeul.f_ml_forecast_archive() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    reasons TEXT[] := ARRAY[]::TEXT[];
BEGIN
    IF OLD.model_version IS DISTINCT FROM NEW.model_version THEN
        reasons := reasons || 'model'::TEXT;
    END IF;
    IF OLD.generated_at IS DISTINCT FROM NEW.generated_at THEN
        reasons := reasons || 'generated_at'::TEXT;
    END IF;
    IF OLD.predicted IS DISTINCT FROM NEW.predicted
       OR OLD.current_price IS DISTINCT FROM NEW.current_price THEN
        reasons := reasons || 'price'::TEXT;
    END IF;
    IF OLD.lower IS DISTINCT FROM NEW.lower
       OR OLD.upper IS DISTINCT FROM NEW.upper THEN
        reasons := reasons || 'band'::TEXT;
    END IF;
    IF OLD.use_recommended IS DISTINCT FROM NEW.use_recommended THEN
        reasons := reasons || 'quality'::TEXT;
    END IF;
    IF OLD.is_filled IS DISTINCT FROM NEW.is_filled
       OR OLD.is_gated IS DISTINCT FROM NEW.is_gated
       OR OLD.src_lead_biz_d IS DISTINCT FROM NEW.src_lead_biz_d THEN
        reasons := reasons || 'origin'::TEXT;
    END IF;
    IF OLD.spec_desc IS DISTINCT FROM NEW.spec_desc
       OR OLD.grade_name IS DISTINCT FROM NEW.grade_name THEN
        reasons := reasons || 'spec'::TEXT;
    END IF;

    -- 바뀐 게 없으면 이력을 만들지 않는다. 배치를 하루에 여러 번 돌려도
    -- 쓰레기가 쌓이지 않는다.
    IF array_length(reasons, 1) IS NULL THEN
        RETURN NEW;
    END IF;

    INSERT INTO haetdeul.ml_price_forecasts_history (
        change_reason, base_dt, item_nm, target_kind, offset_days, target_dt,
        predicted, lower, upper, current_price, unit, model_version, generated_at,
        src_lead_biz_d, is_filled, is_gated, gate_reason,
        market_name, grade_name, spec_desc, unit_weight_kg,
        quality_note, use_recommended, created_at)
    VALUES (
        array_to_string(reasons, ','), OLD.base_dt, OLD.item_nm, OLD.target_kind,
        OLD.offset_days, OLD.target_dt, OLD.predicted, OLD.lower, OLD.upper,
        OLD.current_price, OLD.unit, OLD.model_version, OLD.generated_at,
        OLD.src_lead_biz_d, OLD.is_filled, OLD.is_gated, OLD.gate_reason,
        OLD.market_name, OLD.grade_name, OLD.spec_desc, OLD.unit_weight_kg,
        OLD.quality_note, OLD.use_recommended, OLD.created_at);
    RETURN NEW;
END $$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: agent_policies; Type: TABLE; Schema: haetdeul; Owner: -
--

CREATE TABLE haetdeul.agent_policies (
    agent_type text NOT NULL,
    company_persona_id text NOT NULL,
    policy_json jsonb NOT NULL,
    source_ref text NOT NULL,
    provisional boolean DEFAULT false NOT NULL,
    active boolean DEFAULT true NOT NULL,
    note text
);


--
-- Name: TABLE agent_policies; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON TABLE haetdeul.agent_policies IS '각 Agent의 판단 임계치·허용 조정축·재시도 등 설정값.';


--
-- Name: COLUMN agent_policies.agent_type; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.agent_policies.agent_type IS 'Agent 종류.';


--
-- Name: COLUMN agent_policies.company_persona_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.agent_policies.company_persona_id IS '적용 회사 Persona ID.';


--
-- Name: COLUMN agent_policies.policy_json; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.agent_policies.policy_json IS 'Agent별 판단 임계치·조정축·재시도 정책을 담은 JSON.';


--
-- Name: COLUMN agent_policies.source_ref; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.agent_policies.source_ref IS '근거/정책의 안정적 참조 ID.';


--
-- Name: COLUMN agent_policies.provisional; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.agent_policies.provisional IS '잠정/Proxy/Assumption 값 포함 여부.';


--
-- Name: COLUMN agent_policies.active; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.agent_policies.active IS '활성 여부.';


--
-- Name: COLUMN agent_policies.note; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.agent_policies.note IS '추가 설명 및 주의사항.';


--
-- Name: agent_policy_config; Type: TABLE; Schema: haetdeul; Owner: -
--

CREATE TABLE haetdeul.agent_policy_config (
    policy_id bigint NOT NULL,
    domain character varying(30) NOT NULL,
    policy_key character varying(100) NOT NULL,
    value_kind character varying(20) NOT NULL,
    value_numeric numeric,
    value_text text,
    value_json jsonb,
    unit character varying(50),
    evidence_grade character varying(20) NOT NULL,
    source_ref character varying(200) NOT NULL,
    approved_by character varying(100) NOT NULL,
    policy_version character varying(40) NOT NULL,
    persona_version character varying(40),
    usage_scope character varying(40) DEFAULT 'AGENT_MVP_DEMO'::character varying NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT agent_policy_config_evidence_grade_check CHECK (((evidence_grade)::text = ANY ((ARRAY['OFFICIAL'::character varying, 'VENDOR'::character varying, 'SIM_FIXED'::character varying, 'ASSUMED'::character varying])::text[]))),
    CONSTRAINT agent_policy_config_value_kind_check CHECK (((value_kind)::text = ANY ((ARRAY['NUMERIC'::character varying, 'TEXT'::character varying, 'JSON'::character varying])::text[]))),
    CONSTRAINT ck_agent_policy_single_value CHECK ((((((value_numeric IS NOT NULL))::integer + ((value_text IS NOT NULL))::integer) + ((value_json IS NOT NULL))::integer) = 1))
);


--
-- Name: agent_policy_config_policy_id_seq; Type: SEQUENCE; Schema: haetdeul; Owner: -
--

CREATE SEQUENCE haetdeul.agent_policy_config_policy_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: agent_policy_config_policy_id_seq; Type: SEQUENCE OWNED BY; Schema: haetdeul; Owner: -
--

ALTER SEQUENCE haetdeul.agent_policy_config_policy_id_seq OWNED BY haetdeul.agent_policy_config.policy_id;


--
-- Name: agent_runs; Type: TABLE; Schema: haetdeul; Owner: -
--

CREATE TABLE haetdeul.agent_runs (
    agent_run_id text NOT NULL,
    sim_run_id text NOT NULL,
    agent_type text NOT NULL,
    agent_version text,
    as_of date NOT NULL,
    started_at timestamp with time zone NOT NULL,
    finished_at timestamp with time zone,
    run_status text NOT NULL,
    input_snapshot_json jsonb,
    output_snapshot_json jsonb,
    error_message text
);


--
-- Name: TABLE agent_runs; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON TABLE haetdeul.agent_runs IS 'Agent 실행 감사로그. 버전·as_of·입출력 Snapshot·오류를 기록한다.';


--
-- Name: COLUMN agent_runs.agent_run_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.agent_runs.agent_run_id IS 'Agent 실행 ID.';


--
-- Name: COLUMN agent_runs.sim_run_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.agent_runs.sim_run_id IS '시뮬레이션 실행 ID.';


--
-- Name: COLUMN agent_runs.agent_type; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.agent_runs.agent_type IS 'Agent 종류.';


--
-- Name: COLUMN agent_runs.agent_version; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.agent_runs.agent_version IS 'Agent 버전.';


--
-- Name: COLUMN agent_runs.as_of; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.agent_runs.as_of IS '판단 시점 기준일.';


--
-- Name: COLUMN agent_runs.started_at; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.agent_runs.started_at IS '실행 시작시각.';


--
-- Name: COLUMN agent_runs.finished_at; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.agent_runs.finished_at IS '실행 종료시각.';


--
-- Name: COLUMN agent_runs.run_status; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.agent_runs.run_status IS 'Agent 실행상태.';


--
-- Name: COLUMN agent_runs.input_snapshot_json; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.agent_runs.input_snapshot_json IS 'Agent에 전달된 입력 Snapshot.';


--
-- Name: COLUMN agent_runs.output_snapshot_json; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.agent_runs.output_snapshot_json IS 'Agent가 생성한 출력 Snapshot.';


--
-- Name: COLUMN agent_runs.error_message; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.agent_runs.error_message IS '실행 실패 시 오류메시지.';


--
-- Name: auction_prices_daily; Type: TABLE; Schema: haetdeul; Owner: -
--

CREATE TABLE haetdeul.auction_prices_daily (
    id bigint,
    auction_date date,
    market_category character varying(10),
    wholesale_market_code character varying(20),
    wholesale_market_name character varying(100),
    item_code character varying(20),
    item_name character varying(50),
    grade_code character varying(20),
    grade_name character varying(50),
    avg_auction_price_krw_per_kg numeric(28,6),
    min_auction_price_krw_per_kg numeric(28,6),
    max_auction_price_krw_per_kg numeric(28,6),
    trade_volume_kg numeric(28,6),
    trade_amount_krw numeric(28,6),
    package_trade_quantity numeric(28,6),
    source_trade_count bigint,
    source character varying(200),
    subclass_code character varying(20),
    subclass_name character varying(100),
    package_code character varying(20),
    package_name character varying(50),
    unit_weight_kg numeric(18,3)
);


--
-- Name: company_personas; Type: TABLE; Schema: haetdeul; Owner: -
--

CREATE TABLE haetdeul.company_personas (
    persona_id text NOT NULL,
    persona_version text NOT NULL,
    company_name text NOT NULL,
    company_name_en text,
    industry text NOT NULL,
    founder_birth_year integer,
    founder_age integer,
    employment_status text,
    startup_stage text,
    first_startup boolean,
    business_region text,
    company_scale text,
    priority_support boolean,
    tax_arrears boolean,
    credit_incident boolean,
    office_type text,
    office_deposit_krw numeric(18,2) DEFAULT 0 NOT NULL,
    office_monthly_cost_krw numeric(18,2) DEFAULT 0 NOT NULL,
    initial_owner_funding_krw numeric(18,2) NOT NULL,
    initial_debt_krw numeric(18,2) DEFAULT 0 NOT NULL,
    other_external_funding_krw numeric(18,2) DEFAULT 0 NOT NULL,
    target_runway_months integer NOT NULL,
    minimum_cash_buffer_months integer NOT NULL,
    purchase_payment_days integer NOT NULL,
    sales_collection_days integer NOT NULL,
    monthly_labor_cost_krw numeric(18,2) NOT NULL,
    minimum_contribution_margin_rate numeric(10,8),
    target_contribution_margin_rate numeric(10,8),
    burn_in_days integer DEFAULT 30 NOT NULL,
    burn_in_start date,
    burn_in_end date,
    active boolean DEFAULT true NOT NULL,
    note text
);


--
-- Name: TABLE company_personas; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON TABLE haetdeul.company_personas IS '회사·대표자·재무의 고정 Persona 기준값. 현재 현금처럼 시간에 따라 변하는 값은 finance_states에서 관리한다.';


--
-- Name: COLUMN company_personas.persona_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.company_personas.persona_id IS '회사 Persona 고유 ID.';


--
-- Name: COLUMN company_personas.persona_version; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.company_personas.persona_version IS 'Persona 버전.';


--
-- Name: COLUMN company_personas.company_name; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.company_personas.company_name IS '회사명.';


--
-- Name: COLUMN company_personas.company_name_en; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.company_personas.company_name_en IS '회사 영문명.';


--
-- Name: COLUMN company_personas.industry; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.company_personas.industry IS '회사 업종.';


--
-- Name: COLUMN company_personas.founder_birth_year; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.company_personas.founder_birth_year IS '대표자 Persona 출생연도.';


--
-- Name: COLUMN company_personas.founder_age; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.company_personas.founder_age IS '기준연도 대표자 Persona 만 나이.';


--
-- Name: COLUMN company_personas.employment_status; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.company_personas.employment_status IS '대표자 Persona 고용상태.';


--
-- Name: COLUMN company_personas.startup_stage; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.company_personas.startup_stage IS '창업 단계.';


--
-- Name: COLUMN company_personas.first_startup; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.company_personas.first_startup IS '첫 창업 여부.';


--
-- Name: COLUMN company_personas.business_region; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.company_personas.business_region IS '사업장 권역.';


--
-- Name: COLUMN company_personas.company_scale; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.company_personas.company_scale IS '기업 규모.';


--
-- Name: COLUMN company_personas.priority_support; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.company_personas.priority_support IS '정책자금 중점지원분야 해당 여부.';


--
-- Name: COLUMN company_personas.tax_arrears; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.company_personas.tax_arrears IS '세금 체납 여부.';


--
-- Name: COLUMN company_personas.credit_incident; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.company_personas.credit_incident IS '금융 연체·사고 여부.';


--
-- Name: COLUMN company_personas.office_type; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.company_personas.office_type IS '사업장 형태.';


--
-- Name: COLUMN company_personas.office_deposit_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.company_personas.office_deposit_krw IS '사무실 보증금(원).';


--
-- Name: COLUMN company_personas.office_monthly_cost_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.company_personas.office_monthly_cost_krw IS '사무실 월 비용(원/월).';


--
-- Name: COLUMN company_personas.initial_owner_funding_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.company_personas.initial_owner_funding_krw IS '초기 자기자금(원).';


--
-- Name: COLUMN company_personas.initial_debt_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.company_personas.initial_debt_krw IS 'Day0 초기 부채(원).';


--
-- Name: COLUMN company_personas.other_external_funding_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.company_personas.other_external_funding_krw IS '기타 외부조달금(원).';


--
-- Name: COLUMN company_personas.target_runway_months; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.company_personas.target_runway_months IS '목표 운영기간(개월).';


--
-- Name: COLUMN company_personas.minimum_cash_buffer_months; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.company_personas.minimum_cash_buffer_months IS '최소 현금 방어기간(개월).';


--
-- Name: COLUMN company_personas.purchase_payment_days; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.company_personas.purchase_payment_days IS '매입대금 지급주기 D+n. 현재 D+0 Baseline이며 실계약 확인 시 교체 대상.';


--
-- Name: COLUMN company_personas.sales_collection_days; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.company_personas.sales_collection_days IS '판매대금 회수주기 D+n. 현재 통합 Persona 기준 D+30.';


--
-- Name: COLUMN company_personas.monthly_labor_cost_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.company_personas.monthly_labor_cost_krw IS '월 인건비 기준값(원/월).';


--
-- Name: COLUMN company_personas.minimum_contribution_margin_rate; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.company_personas.minimum_contribution_margin_rate IS '손익분기 관점의 최소 기여이익률. 0~1 비율.';


--
-- Name: COLUMN company_personas.target_contribution_margin_rate; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.company_personas.target_contribution_margin_rate IS '영업 가격정책 목표 기여이익률. 0~1 비율.';


--
-- Name: COLUMN company_personas.burn_in_days; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.company_personas.burn_in_days IS 'Agent 실행 전 선행운영 기간(일).';


--
-- Name: COLUMN company_personas.burn_in_start; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.company_personas.burn_in_start IS 'Burn-in 시작일.';


--
-- Name: COLUMN company_personas.burn_in_end; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.company_personas.burn_in_end IS 'Burn-in 종료일.';


--
-- Name: COLUMN company_personas.active; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.company_personas.active IS '활성 여부.';


--
-- Name: COLUMN company_personas.note; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.company_personas.note IS '추가 설명 및 주의사항.';


--
-- Name: constraint_reviews; Type: TABLE; Schema: haetdeul; Owner: -
--

CREATE TABLE haetdeul.constraint_reviews (
    review_id text NOT NULL,
    proposal_id text NOT NULL,
    scenario_id text NOT NULL,
    sim_run_id text NOT NULL,
    agent_type text NOT NULL,
    review_agent_run_id text,
    as_of date NOT NULL,
    verdict text NOT NULL,
    max_feasible_qty_kg numeric(18,6),
    max_feasible_amount_krw numeric(18,6),
    hard_constraints_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    soft_warnings_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    reasoning text NOT NULL,
    suggested_adjustment_json jsonb,
    evidence_ids_json jsonb DEFAULT '[]'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT constraint_reviews_agent_type_check CHECK ((agent_type = ANY (ARRAY['LOGISTICS'::text, 'SALES'::text, 'FINANCE'::text]))),
    CONSTRAINT constraint_reviews_verdict_check CHECK ((verdict = ANY (ARRAY['APPROVE'::text, 'CONDITIONAL'::text, 'REJECT'::text])))
);


--
-- Name: TABLE constraint_reviews; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON TABLE haetdeul.constraint_reviews IS '재고·물류/영업/재무 Agent의 T2 병렬 제약 검토 결과.';


--
-- Name: COLUMN constraint_reviews.review_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.constraint_reviews.review_id IS 'T2 제약검토 ID.';


--
-- Name: COLUMN constraint_reviews.proposal_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.constraint_reviews.proposal_id IS '매입 Agent 제안 ID.';


--
-- Name: COLUMN constraint_reviews.scenario_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.constraint_reviews.scenario_id IS '매입 Agent 시나리오 ID.';


--
-- Name: COLUMN constraint_reviews.sim_run_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.constraint_reviews.sim_run_id IS '시뮬레이션 실행 ID.';


--
-- Name: COLUMN constraint_reviews.agent_type; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.constraint_reviews.agent_type IS 'Agent 종류.';


--
-- Name: COLUMN constraint_reviews.review_agent_run_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.constraint_reviews.review_agent_run_id IS '검토를 수행한 Agent Run ID.';


--
-- Name: COLUMN constraint_reviews.as_of; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.constraint_reviews.as_of IS '판단 시점 기준일.';


--
-- Name: COLUMN constraint_reviews.verdict; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.constraint_reviews.verdict IS 'APPROVE/CONDITIONAL/REJECT 판정.';


--
-- Name: COLUMN constraint_reviews.max_feasible_qty_kg; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.constraint_reviews.max_feasible_qty_kg IS '물류/영업 관점의 최대 실행 가능 수량.';


--
-- Name: COLUMN constraint_reviews.max_feasible_amount_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.constraint_reviews.max_feasible_amount_krw IS '재무 관점의 최대 실행 가능 금액.';


--
-- Name: COLUMN constraint_reviews.hard_constraints_json; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.constraint_reviews.hard_constraints_json IS 'Hard Constraint 목록 JSON.';


--
-- Name: COLUMN constraint_reviews.soft_warnings_json; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.constraint_reviews.soft_warnings_json IS '주의사항 목록 JSON.';


--
-- Name: COLUMN constraint_reviews.reasoning; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.constraint_reviews.reasoning IS '판단 이유.';


--
-- Name: COLUMN constraint_reviews.suggested_adjustment_json; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.constraint_reviews.suggested_adjustment_json IS '조건부/반려 시 해당 Agent가 허용된 축 안에서 제안하는 변경안.';


--
-- Name: COLUMN constraint_reviews.evidence_ids_json; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.constraint_reviews.evidence_ids_json IS '판단에 사용한 evidence/ref_id 목록.';


--
-- Name: COLUMN constraint_reviews.created_at; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.constraint_reviews.created_at IS '생성시각.';


--
-- Name: daily_closings; Type: TABLE; Schema: haetdeul; Owner: -
--

CREATE TABLE haetdeul.daily_closings (
    sim_run_id text NOT NULL,
    close_date date NOT NULL,
    day_no integer NOT NULL,
    purchase_cash_out_krw numeric(18,6) NOT NULL,
    logistics_cash_out_krw numeric(18,6) NOT NULL,
    payroll_interest_cash_out_krw numeric(18,6) NOT NULL,
    sales_recognized_krw numeric(18,6) NOT NULL,
    collection_cash_in_krw numeric(18,6) NOT NULL,
    base_net_cash_krw numeric(18,6) NOT NULL,
    base_cash_balance_krw numeric(18,6) NOT NULL,
    loan_execution_krw numeric(18,6) NOT NULL,
    loan_cash_balance_krw numeric(18,6) NOT NULL,
    receivables_balance_krw numeric(18,6) NOT NULL,
    inventory_qty_kg numeric(18,6) NOT NULL,
    accounting_inventory_cost_krw numeric(18,6) NOT NULL,
    closed boolean DEFAULT true NOT NULL
);


--
-- Name: TABLE daily_closings; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON TABLE haetdeul.daily_closings IS '매입·판매·물류·현금·미수·재고를 일자별로 집계한 마감 결과.';


--
-- Name: COLUMN daily_closings.sim_run_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.daily_closings.sim_run_id IS '시뮬레이션 실행 ID.';


--
-- Name: COLUMN daily_closings.close_date; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.daily_closings.close_date IS '일별 마감 기준일.';


--
-- Name: COLUMN daily_closings.day_no; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.daily_closings.day_no IS '시뮬레이션/Burn-in Day 순번.';


--
-- Name: COLUMN daily_closings.purchase_cash_out_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.daily_closings.purchase_cash_out_krw IS '일별 매입 현금유출(원).';


--
-- Name: COLUMN daily_closings.logistics_cash_out_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.daily_closings.logistics_cash_out_krw IS '일별 물류 현금유출(원).';


--
-- Name: COLUMN daily_closings.payroll_interest_cash_out_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.daily_closings.payroll_interest_cash_out_krw IS '일별 급여/이자 현금유출(원).';


--
-- Name: COLUMN daily_closings.sales_recognized_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.daily_closings.sales_recognized_krw IS '일별 발생 매출액(원).';


--
-- Name: COLUMN daily_closings.collection_cash_in_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.daily_closings.collection_cash_in_krw IS '일별 실제 매출대금 현금유입(원).';


--
-- Name: COLUMN daily_closings.base_net_cash_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.daily_closings.base_net_cash_krw IS '대출 제외 일일 순현금 증감(원).';


--
-- Name: COLUMN daily_closings.base_cash_balance_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.daily_closings.base_cash_balance_krw IS '대출 없는 Base 시나리오 현금잔액(원).';


--
-- Name: COLUMN daily_closings.loan_execution_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.daily_closings.loan_execution_krw IS '해당 일자 대출 실행액(원).';


--
-- Name: COLUMN daily_closings.loan_cash_balance_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.daily_closings.loan_cash_balance_krw IS '대출 반영 후 현금잔액(원).';


--
-- Name: COLUMN daily_closings.receivables_balance_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.daily_closings.receivables_balance_krw IS '마감 시점 미수금 잔액(원).';


--
-- Name: COLUMN daily_closings.inventory_qty_kg; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.daily_closings.inventory_qty_kg IS '마감 시점 재고수량(kg).';


--
-- Name: COLUMN daily_closings.accounting_inventory_cost_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.daily_closings.accounting_inventory_cost_krw IS '마감 기준 회계적 재고원가(원).';


--
-- Name: COLUMN daily_closings.closed; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.daily_closings.closed IS '일별 마감 완료 여부. 다음 날 Agent 실행 전제 검증에 사용.';


--
-- Name: deliveries; Type: TABLE; Schema: haetdeul; Owner: -
--

CREATE TABLE haetdeul.deliveries (
    delivery_id text NOT NULL,
    sim_run_id text NOT NULL,
    sale_id text NOT NULL,
    customer_partner_id text NOT NULL,
    logistics_contract_id text,
    dispatch_date date NOT NULL,
    delivery_date date NOT NULL,
    total_quantity_kg numeric(18,6) NOT NULL,
    vehicle_class text NOT NULL,
    distance_km numeric(10,3),
    transport_cost_krw numeric(18,6) NOT NULL,
    allocated_logistics_cost_krw numeric(18,6) NOT NULL,
    status text NOT NULL,
    note text,
    CONSTRAINT deliveries_status_check CHECK ((status = ANY (ARRAY['PLANNED'::text, 'DISPATCHED'::text, 'DELIVERED'::text, 'CANCELLED'::text])))
);


--
-- Name: TABLE deliveries; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON TABLE haetdeul.deliveries IS '판매 건에 대한 실제 배송 실행정보. 현재 5PL/외주운송 기준.';


--
-- Name: COLUMN deliveries.delivery_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.deliveries.delivery_id IS '배송 ID.';


--
-- Name: COLUMN deliveries.sim_run_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.deliveries.sim_run_id IS '시뮬레이션 실행 ID.';


--
-- Name: COLUMN deliveries.sale_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.deliveries.sale_id IS '판매 Header ID.';


--
-- Name: COLUMN deliveries.customer_partner_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.deliveries.customer_partner_id IS '판매 고객 Partner ID.';


--
-- Name: COLUMN deliveries.logistics_contract_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.deliveries.logistics_contract_id IS '적용 물류계약/Persona ID.';


--
-- Name: COLUMN deliveries.dispatch_date; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.deliveries.dispatch_date IS '배송 출발일.';


--
-- Name: COLUMN deliveries.delivery_date; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.deliveries.delivery_date IS '납품일.';


--
-- Name: COLUMN deliveries.total_quantity_kg; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.deliveries.total_quantity_kg IS '총수량(kg).';


--
-- Name: COLUMN deliveries.vehicle_class; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.deliveries.vehicle_class IS '차량 클래스.';


--
-- Name: COLUMN deliveries.distance_km; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.deliveries.distance_km IS '배송거리(km).';


--
-- Name: COLUMN deliveries.transport_cost_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.deliveries.transport_cost_krw IS '배송 운송비(원).';


--
-- Name: COLUMN deliveries.allocated_logistics_cost_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.deliveries.allocated_logistics_cost_krw IS '해당 주문에 배부된 직접물류비(원).';


--
-- Name: COLUMN deliveries.status; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.deliveries.status IS '상태값.';


--
-- Name: COLUMN deliveries.note; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.deliveries.note IS '추가 설명 및 주의사항.';


--
-- Name: evidences; Type: TABLE; Schema: haetdeul; Owner: -
--

CREATE TABLE haetdeul.evidences (
    evidence_id text NOT NULL,
    source_ref text NOT NULL,
    evidence_type text NOT NULL,
    source_name text NOT NULL,
    source_uri text,
    claim text,
    source_as_of text,
    status text NOT NULL,
    note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE evidences; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON TABLE haetdeul.evidences IS '프로젝트 전체 근거 원장. Persona·시장데이터·정책값·Agent 판단이 참조하는 source_ref와 출처를 관리한다.';


--
-- Name: COLUMN evidences.evidence_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.evidences.evidence_id IS '연결된 근거 원장 ID.';


--
-- Name: COLUMN evidences.source_ref; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.evidences.source_ref IS '근거/정책의 안정적 참조 ID.';


--
-- Name: COLUMN evidences.evidence_type; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.evidences.evidence_type IS '근거 성격/유형.';


--
-- Name: COLUMN evidences.source_name; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.evidences.source_name IS '근거 문서/기관/데이터셋명.';


--
-- Name: COLUMN evidences.source_uri; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.evidences.source_uri IS '근거 원문 URL 또는 파일 식별정보.';


--
-- Name: COLUMN evidences.claim; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.evidences.claim IS '근거가 뒷받침하는 값/조건 요약.';


--
-- Name: COLUMN evidences.source_as_of; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.evidences.source_as_of IS '근거 기준일/조회일/적용기간.';


--
-- Name: COLUMN evidences.status; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.evidences.status IS '상태값.';


--
-- Name: COLUMN evidences.note; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.evidences.note IS '추가 설명 및 주의사항.';


--
-- Name: COLUMN evidences.created_at; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.evidences.created_at IS '생성시각.';


--
-- Name: expenses; Type: TABLE; Schema: haetdeul; Owner: -
--

CREATE TABLE haetdeul.expenses (
    expense_id text NOT NULL,
    sim_run_id text NOT NULL,
    expense_date date NOT NULL,
    expense_category text NOT NULL,
    amount_krw numeric(18,6) NOT NULL,
    is_fixed boolean NOT NULL,
    related_delivery_id text,
    evidence_id text,
    status text NOT NULL,
    note text,
    CONSTRAINT expenses_amount_krw_check CHECK ((amount_krw >= (0)::numeric)),
    CONSTRAINT expenses_status_check CHECK ((status = ANY (ARRAY['PAID'::text, 'ACCRUED'::text, 'CANCELLED'::text])))
);


--
-- Name: TABLE expenses; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON TABLE haetdeul.expenses IS '매입대금 이외 인건비·물류비·이자 등 운영비 원장.';


--
-- Name: COLUMN expenses.expense_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.expenses.expense_id IS '운영비 ID.';


--
-- Name: COLUMN expenses.sim_run_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.expenses.sim_run_id IS '시뮬레이션 실행 ID.';


--
-- Name: COLUMN expenses.expense_date; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.expenses.expense_date IS '비용 발생일.';


--
-- Name: COLUMN expenses.expense_category; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.expenses.expense_category IS '비용 분류.';


--
-- Name: COLUMN expenses.amount_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.expenses.amount_krw IS '금액(원).';


--
-- Name: COLUMN expenses.is_fixed; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.expenses.is_fixed IS '고정비 여부.';


--
-- Name: COLUMN expenses.related_delivery_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.expenses.related_delivery_id IS '연결된 배송 ID.';


--
-- Name: COLUMN expenses.evidence_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.expenses.evidence_id IS '연결된 근거 원장 ID.';


--
-- Name: COLUMN expenses.status; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.expenses.status IS '상태값.';


--
-- Name: COLUMN expenses.note; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.expenses.note IS '추가 설명 및 주의사항.';


--
-- Name: finance_states; Type: TABLE; Schema: haetdeul; Owner: -
--

CREATE TABLE haetdeul.finance_states (
    finance_state_id text NOT NULL,
    sim_run_id text NOT NULL,
    state_date date NOT NULL,
    state_type text NOT NULL,
    financing_mode text NOT NULL,
    current_cash_krw numeric(18,6) NOT NULL,
    minimum_operating_cash_krw numeric(18,6) NOT NULL,
    committed_outflows_krw numeric(18,6) DEFAULT 0 NOT NULL,
    unsettled_purchase_payables_krw numeric(18,6) DEFAULT 0 NOT NULL,
    receivables_krw numeric(18,6) DEFAULT 0 NOT NULL,
    inventory_book_value_krw numeric(18,6) DEFAULT 0 NOT NULL,
    operational_inventory_value_krw numeric(18,6) DEFAULT 0 NOT NULL,
    current_debt_krw numeric(18,6) DEFAULT 0 NOT NULL,
    recommended_loan_amount_krw numeric(18,6),
    financial_limit_krw numeric(18,6) GENERATED ALWAYS AS ((((current_cash_krw - minimum_operating_cash_krw) - committed_outflows_krw) - unsettled_purchase_payables_krw)) STORED,
    note text
);


--
-- Name: TABLE finance_states; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON TABLE haetdeul.finance_states IS '특정 시점의 재무 Snapshot. 현재현금·최소운영현금·미수·부채·재고가치·financial_limit을 관리한다.';


--
-- Name: COLUMN finance_states.finance_state_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.finance_states.finance_state_id IS '재무 상태 Snapshot ID.';


--
-- Name: COLUMN finance_states.sim_run_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.finance_states.sim_run_id IS '시뮬레이션 실행 ID.';


--
-- Name: COLUMN finance_states.state_date; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.finance_states.state_date IS '재무상태 기준일.';


--
-- Name: COLUMN finance_states.state_type; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.finance_states.state_type IS 'DAY0/DAY30 등 상태유형.';


--
-- Name: COLUMN finance_states.financing_mode; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.finance_states.financing_mode IS '자금조달 Scenario.';


--
-- Name: COLUMN finance_states.current_cash_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.finance_states.current_cash_krw IS '현재 현금(원).';


--
-- Name: COLUMN finance_states.minimum_operating_cash_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.finance_states.minimum_operating_cash_krw IS '매입과 별도로 남겨야 하는 최소 운영현금.';


--
-- Name: COLUMN finance_states.committed_outflows_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.finance_states.committed_outflows_krw IS '확정 예정 현금유출(원).';


--
-- Name: COLUMN finance_states.unsettled_purchase_payables_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.finance_states.unsettled_purchase_payables_krw IS '미정산 매입대금 총액(원).';


--
-- Name: COLUMN finance_states.receivables_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.finance_states.receivables_krw IS '미수금 총액(원).';


--
-- Name: COLUMN finance_states.inventory_book_value_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.finance_states.inventory_book_value_krw IS '통합 Persona 원본 마감에서 사용한 회계적 재고원가.';


--
-- Name: COLUMN finance_states.operational_inventory_value_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.finance_states.operational_inventory_value_krw IS '현재 inventory_lots 잔량×Lot 원가 기준 운영 재고가치.';


--
-- Name: COLUMN finance_states.current_debt_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.finance_states.current_debt_krw IS '현재 부채/대출잔액(원).';


--
-- Name: COLUMN finance_states.recommended_loan_amount_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.finance_states.recommended_loan_amount_krw IS 'Persona 검토에서 산출한 권장 대출액. 실제 승인 대출액이 아님.';


--
-- Name: COLUMN finance_states.financial_limit_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.finance_states.financial_limit_krw IS '재무 Agent 최대 집행가능금액 기준. current_cash-minimum_operating_cash-committed_outflows-unsettled_purchase_payables 자동계산.';


--
-- Name: COLUMN finance_states.note; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.finance_states.note IS '추가 설명 및 주의사항.';


--
-- Name: forecasts; Type: TABLE; Schema: haetdeul; Owner: -
--

CREATE TABLE haetdeul.forecasts (
    forecast_id text NOT NULL,
    sim_run_id text,
    item_id text NOT NULL,
    generated_at timestamp with time zone NOT NULL,
    as_of date NOT NULL,
    target_date date NOT NULL,
    price_type text NOT NULL,
    predicted_price_krw_per_kg numeric(18,6) NOT NULL,
    low_price_krw_per_kg numeric(18,6),
    high_price_krw_per_kg numeric(18,6),
    confidence numeric(8,6),
    model_version text NOT NULL,
    evidence_id text,
    CONSTRAINT forecasts_check CHECK (((generated_at)::date <= as_of)),
    CONSTRAINT forecasts_check1 CHECK ((target_date >= as_of))
);


--
-- Name: TABLE forecasts; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON TABLE haetdeul.forecasts IS 'ML 가격예측 결과. 실제 ML Pipeline이 생성하며 초기 Seed에서는 비어 있는 것이 정상이다.';


--
-- Name: COLUMN forecasts.forecast_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.forecasts.forecast_id IS 'ML 예측 고유 ID.';


--
-- Name: COLUMN forecasts.sim_run_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.forecasts.sim_run_id IS '시뮬레이션 실행 ID.';


--
-- Name: COLUMN forecasts.item_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.forecasts.item_id IS '품목 고유 ID.';


--
-- Name: COLUMN forecasts.generated_at; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.forecasts.generated_at IS '모델 예측 생성시각. generated_at::date는 as_of 이후일 수 없다.';


--
-- Name: COLUMN forecasts.as_of; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.forecasts.as_of IS '모델이 사용할 수 있었던 정보의 판단 기준일.';


--
-- Name: COLUMN forecasts.target_date; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.forecasts.target_date IS '예측 대상일.';


--
-- Name: COLUMN forecasts.price_type; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.forecasts.price_type IS '가격 유형.';


--
-- Name: COLUMN forecasts.predicted_price_krw_per_kg; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.forecasts.predicted_price_krw_per_kg IS '예측가격(원/kg).';


--
-- Name: COLUMN forecasts.low_price_krw_per_kg; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.forecasts.low_price_krw_per_kg IS '예측구간 하한(원/kg).';


--
-- Name: COLUMN forecasts.high_price_krw_per_kg; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.forecasts.high_price_krw_per_kg IS '예측구간 상한(원/kg).';


--
-- Name: COLUMN forecasts.confidence; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.forecasts.confidence IS '신뢰도/불확실성 정보.';


--
-- Name: COLUMN forecasts.model_version; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.forecasts.model_version IS 'ML 모델 버전.';


--
-- Name: COLUMN forecasts.evidence_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.forecasts.evidence_id IS '연결된 근거 원장 ID.';


--
-- Name: inventory_lots; Type: TABLE; Schema: haetdeul; Owner: -
--

CREATE TABLE haetdeul.inventory_lots (
    lot_id text NOT NULL,
    sim_run_id text NOT NULL,
    purchase_item_id text NOT NULL,
    item_id text NOT NULL,
    grade text NOT NULL,
    received_at date NOT NULL,
    original_qty_kg numeric(18,6) NOT NULL,
    remaining_qty_kg numeric(18,6) NOT NULL,
    unit_cost_krw_per_kg numeric(18,6) NOT NULL,
    storage_zone text NOT NULL,
    status text NOT NULL,
    derivation_status text NOT NULL,
    CONSTRAINT inventory_lots_check CHECK ((remaining_qty_kg <= original_qty_kg)),
    CONSTRAINT inventory_lots_original_qty_kg_check CHECK ((original_qty_kg >= (0)::numeric)),
    CONSTRAINT inventory_lots_remaining_qty_kg_check CHECK ((remaining_qty_kg >= (0)::numeric)),
    CONSTRAINT inventory_lots_status_check CHECK ((status = ANY (ARRAY['ACTIVE'::text, 'DEPLETED'::text, 'DISPOSED'::text, 'HOLD'::text])))
);


--
-- Name: TABLE inventory_lots; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON TABLE haetdeul.inventory_lots IS '입고로 생성된 재고 Lot의 현재 상태. 잔여신선도는 저장하지 않고 as_of 기준으로 계산한다.';


--
-- Name: COLUMN inventory_lots.lot_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.inventory_lots.lot_id IS '재고 Lot ID.';


--
-- Name: COLUMN inventory_lots.sim_run_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.inventory_lots.sim_run_id IS '시뮬레이션 실행 ID.';


--
-- Name: COLUMN inventory_lots.purchase_item_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.inventory_lots.purchase_item_id IS '매입 Detail ID.';


--
-- Name: COLUMN inventory_lots.item_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.inventory_lots.item_id IS '품목 고유 ID.';


--
-- Name: COLUMN inventory_lots.grade; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.inventory_lots.grade IS '품질등급.';


--
-- Name: COLUMN inventory_lots.received_at; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.inventory_lots.received_at IS '입고일.';


--
-- Name: COLUMN inventory_lots.original_qty_kg; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.inventory_lots.original_qty_kg IS 'Lot 최초 입고수량(kg).';


--
-- Name: COLUMN inventory_lots.remaining_qty_kg; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.inventory_lots.remaining_qty_kg IS '현재 Lot 잔량. inventory_moves의 IN-OUT-DISPOSE와 정합해야 한다.';


--
-- Name: COLUMN inventory_lots.unit_cost_krw_per_kg; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.inventory_lots.unit_cost_krw_per_kg IS 'Lot 원가단가(원/kg).';


--
-- Name: COLUMN inventory_lots.storage_zone; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.inventory_lots.storage_zone IS '보관 Zone 코드.';


--
-- Name: COLUMN inventory_lots.status; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.inventory_lots.status IS '상태값.';


--
-- Name: COLUMN inventory_lots.derivation_status; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.inventory_lots.derivation_status IS 'Burn-in Lot이 어떤 파생규칙으로 생성됐는지 나타내는 상태.';


--
-- Name: inventory_moves; Type: TABLE; Schema: haetdeul; Owner: -
--

CREATE TABLE haetdeul.inventory_moves (
    move_id text NOT NULL,
    sim_run_id text NOT NULL,
    lot_id text NOT NULL,
    sale_item_id text,
    move_type text NOT NULL,
    quantity_kg numeric(18,6) NOT NULL,
    moved_at date NOT NULL,
    reason_code text NOT NULL,
    note text,
    CONSTRAINT inventory_moves_move_type_check CHECK ((move_type = ANY (ARRAY['IN'::text, 'OUT'::text, 'DISPOSE'::text, 'ADJUST'::text]))),
    CONSTRAINT inventory_moves_quantity_kg_check CHECK ((quantity_kg > (0)::numeric))
);


--
-- Name: TABLE inventory_moves; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON TABLE haetdeul.inventory_moves IS 'Lot의 입고·출고·폐기·조정 이동 원장.';


--
-- Name: COLUMN inventory_moves.move_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.inventory_moves.move_id IS '재고 이동 ID.';


--
-- Name: COLUMN inventory_moves.sim_run_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.inventory_moves.sim_run_id IS '시뮬레이션 실행 ID.';


--
-- Name: COLUMN inventory_moves.lot_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.inventory_moves.lot_id IS '재고 Lot ID.';


--
-- Name: COLUMN inventory_moves.sale_item_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.inventory_moves.sale_item_id IS '판매 Detail ID.';


--
-- Name: COLUMN inventory_moves.move_type; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.inventory_moves.move_type IS '재고 이동 유형.';


--
-- Name: COLUMN inventory_moves.quantity_kg; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.inventory_moves.quantity_kg IS '이동 수량. 양수로 저장하고 방향은 move_type으로 구분한다.';


--
-- Name: COLUMN inventory_moves.moved_at; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.inventory_moves.moved_at IS '재고 이동일.';


--
-- Name: COLUMN inventory_moves.reason_code; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.inventory_moves.reason_code IS '재고 이동 사유 코드.';


--
-- Name: COLUMN inventory_moves.note; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.inventory_moves.note IS '추가 설명 및 주의사항.';


--
-- Name: item_storage_policies; Type: TABLE; Schema: haetdeul; Owner: -
--

CREATE TABLE haetdeul.item_storage_policies (
    item_id text NOT NULL,
    storage_zone text NOT NULL,
    temp_min_c numeric(6,2),
    temp_max_c numeric(6,2),
    rh_min_pct numeric(6,2),
    rh_max_pct numeric(6,2),
    operational_limit_days integer,
    disposal_candidate_days integer DEFAULT 2 NOT NULL,
    medium_grade_factor numeric(8,4) DEFAULT 0.60 NOT NULL,
    loss_rate_baseline numeric(8,6) DEFAULT 0 NOT NULL,
    operational_policy_status text NOT NULL,
    note text
);


--
-- Name: TABLE item_storage_policies; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON TABLE haetdeul.item_storage_policies IS '품목별 저장환경과 재고 Agent용 운영 보관정책.';


--
-- Name: COLUMN item_storage_policies.item_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.item_storage_policies.item_id IS '품목 고유 ID.';


--
-- Name: COLUMN item_storage_policies.storage_zone; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.item_storage_policies.storage_zone IS '보관 Zone 코드.';


--
-- Name: COLUMN item_storage_policies.temp_min_c; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.item_storage_policies.temp_min_c IS '저장 최저온도(℃).';


--
-- Name: COLUMN item_storage_policies.temp_max_c; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.item_storage_policies.temp_max_c IS '저장 최고온도(℃).';


--
-- Name: COLUMN item_storage_policies.rh_min_pct; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.item_storage_policies.rh_min_pct IS '상대습도 하한(%).';


--
-- Name: COLUMN item_storage_policies.rh_max_pct; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.item_storage_policies.rh_max_pct IS '상대습도 상한(%).';


--
-- Name: COLUMN item_storage_policies.operational_limit_days; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.item_storage_policies.operational_limit_days IS 'MVP 운영상 상품 등급 보관한계 일수. 공식 유통기한이 아님.';


--
-- Name: COLUMN item_storage_policies.disposal_candidate_days; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.item_storage_policies.disposal_candidate_days IS '잔여 보관가능일이 이 값 이하이면 폐기검토 후보로 보는 정책값.';


--
-- Name: COLUMN item_storage_policies.medium_grade_factor; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.item_storage_policies.medium_grade_factor IS '중품 보관한계 계산 계수. 상품 운영 보관일수×계수.';


--
-- Name: COLUMN item_storage_policies.loss_rate_baseline; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.item_storage_policies.loss_rate_baseline IS '기본 감모/폐기율.';


--
-- Name: COLUMN item_storage_policies.operational_policy_status; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.item_storage_policies.operational_policy_status IS '운영 보관정책 상태.';


--
-- Name: COLUMN item_storage_policies.note; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.item_storage_policies.note IS '추가 설명 및 주의사항.';


--
-- Name: items; Type: TABLE; Schema: haetdeul; Owner: -
--

CREATE TABLE haetdeul.items (
    item_id text NOT NULL,
    item_code text NOT NULL,
    item_name text NOT NULL,
    category text DEFAULT '채소류'::text NOT NULL,
    base_unit text DEFAULT 'kg'::text NOT NULL,
    mvp_active boolean DEFAULT true NOT NULL,
    ml_target boolean DEFAULT true NOT NULL
);


--
-- Name: TABLE items; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON TABLE haetdeul.items IS '매입·판매·재고·시장가격·ML이 공통 참조하는 품목 마스터.';


--
-- Name: COLUMN items.item_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.items.item_id IS '품목 고유 ID.';


--
-- Name: COLUMN items.item_code; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.items.item_code IS 'API/코드에서 사용하는 품목 코드.';


--
-- Name: COLUMN items.item_name; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.items.item_name IS '품목명.';


--
-- Name: COLUMN items.category; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.items.category IS '분류.';


--
-- Name: COLUMN items.base_unit; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.items.base_unit IS '기본 단위.';


--
-- Name: COLUMN items.mvp_active; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.items.mvp_active IS '현재 MVP 대상 여부.';


--
-- Name: COLUMN items.ml_target; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.items.ml_target IS 'ML 예측 대상 여부.';


--
-- Name: logistics_contracts; Type: TABLE; Schema: haetdeul; Owner: -
--

CREATE TABLE haetdeul.logistics_contracts (
    logistics_contract_id text NOT NULL,
    company_persona_id text NOT NULL,
    provider_partner_id text,
    logistics_model text NOT NULL,
    own_warehouse boolean NOT NULL,
    own_vehicle_count integer NOT NULL,
    required_capacity_plt numeric(12,3) NOT NULL,
    guaranteed_capacity_plt numeric(12,3) NOT NULL,
    effective_kg_per_pallet numeric(18,3) NOT NULL,
    equivalent_capacity_ton numeric(18,6) NOT NULL,
    storage_rate_per_plt_month_krw numeric(18,6) NOT NULL,
    handling_rate_per_plt_event_krw numeric(18,6) NOT NULL,
    vehicle_class text NOT NULL,
    delivery_distance_km numeric(10,3),
    transport_cost_per_delivery_krw numeric(18,6) NOT NULL,
    management_fee_rate numeric(10,8) NOT NULL,
    monthly_storage_cost_krw numeric(18,6) NOT NULL,
    monthly_handling_cost_krw numeric(18,6) NOT NULL,
    monthly_transport_cost_krw numeric(18,6) NOT NULL,
    monthly_management_fee_krw numeric(18,6) NOT NULL,
    monthly_total_logistics_cost_krw numeric(18,6) NOT NULL,
    safety_stock_ratio numeric(10,8) NOT NULL,
    capacity_safety_margin_rate numeric(10,8) NOT NULL,
    contract_status text NOT NULL,
    provisional boolean DEFAULT true NOT NULL,
    note text
);


--
-- Name: TABLE logistics_contracts; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON TABLE haetdeul.logistics_contracts IS '5PL 용량·단가·차량·수수료 등 물류 Persona/계약 기준.';


--
-- Name: COLUMN logistics_contracts.logistics_contract_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.logistics_contracts.logistics_contract_id IS '적용 물류계약/Persona ID.';


--
-- Name: COLUMN logistics_contracts.company_persona_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.logistics_contracts.company_persona_id IS '적용 회사 Persona ID.';


--
-- Name: COLUMN logistics_contracts.provider_partner_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.logistics_contracts.provider_partner_id IS '물류 공급자 Partner ID.';


--
-- Name: COLUMN logistics_contracts.logistics_model; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.logistics_contracts.logistics_model IS '물류 운영모델.';


--
-- Name: COLUMN logistics_contracts.own_warehouse; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.logistics_contracts.own_warehouse IS '자가창고 보유 여부.';


--
-- Name: COLUMN logistics_contracts.own_vehicle_count; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.logistics_contracts.own_vehicle_count IS '자가차량 보유대수.';


--
-- Name: COLUMN logistics_contracts.required_capacity_plt; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.logistics_contracts.required_capacity_plt IS '기본 필요 Pallet 수.';


--
-- Name: COLUMN logistics_contracts.guaranteed_capacity_plt; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.logistics_contracts.guaranteed_capacity_plt IS '시뮬레이션에서 보장되는 것으로 보는 5PL Pallet 수. 실 SLA 확정 전 Baseline.';


--
-- Name: COLUMN logistics_contracts.effective_kg_per_pallet; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.logistics_contracts.effective_kg_per_pallet IS '운영상 1PLT당 유효 적재중량(kg).';


--
-- Name: COLUMN logistics_contracts.equivalent_capacity_ton; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.logistics_contracts.equivalent_capacity_ton IS '보장 PLT×유효 적재중량을 톤으로 환산한 용량.';


--
-- Name: COLUMN logistics_contracts.storage_rate_per_plt_month_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.logistics_contracts.storage_rate_per_plt_month_krw IS '보관단가(원/PLT·월).';


--
-- Name: COLUMN logistics_contracts.handling_rate_per_plt_event_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.logistics_contracts.handling_rate_per_plt_event_krw IS '하역단가(원/PLT·event).';


--
-- Name: COLUMN logistics_contracts.vehicle_class; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.logistics_contracts.vehicle_class IS '차량 클래스.';


--
-- Name: COLUMN logistics_contracts.delivery_distance_km; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.logistics_contracts.delivery_distance_km IS '배송비 산정 기준거리(km).';


--
-- Name: COLUMN logistics_contracts.transport_cost_per_delivery_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.logistics_contracts.transport_cost_per_delivery_krw IS '배송 1회 운송비 기준값(원).';


--
-- Name: COLUMN logistics_contracts.management_fee_rate; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.logistics_contracts.management_fee_rate IS '5PL 관리수수료율. 0~1 비율이며 현재 Simulation Assumption.';


--
-- Name: COLUMN logistics_contracts.monthly_storage_cost_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.logistics_contracts.monthly_storage_cost_krw IS '월 보관비(원).';


--
-- Name: COLUMN logistics_contracts.monthly_handling_cost_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.logistics_contracts.monthly_handling_cost_krw IS '월 하역비(원).';


--
-- Name: COLUMN logistics_contracts.monthly_transport_cost_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.logistics_contracts.monthly_transport_cost_krw IS '월 운송비(원).';


--
-- Name: COLUMN logistics_contracts.monthly_management_fee_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.logistics_contracts.monthly_management_fee_krw IS '월 5PL 관리수수료(원).';


--
-- Name: COLUMN logistics_contracts.monthly_total_logistics_cost_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.logistics_contracts.monthly_total_logistics_cost_krw IS '월 총 물류비(원).';


--
-- Name: COLUMN logistics_contracts.safety_stock_ratio; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.logistics_contracts.safety_stock_ratio IS '안전재고 비율(0~1).';


--
-- Name: COLUMN logistics_contracts.capacity_safety_margin_rate; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.logistics_contracts.capacity_safety_margin_rate IS '물류 capacity 판단 안전마진(0~1).';


--
-- Name: COLUMN logistics_contracts.contract_status; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.logistics_contracts.contract_status IS '계약/Simulation 상태.';


--
-- Name: COLUMN logistics_contracts.provisional; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.logistics_contracts.provisional IS '잠정/Proxy/Assumption 값 포함 여부.';


--
-- Name: COLUMN logistics_contracts.note; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.logistics_contracts.note IS '추가 설명 및 주의사항.';


--
-- Name: logistics_runtime_fixture; Type: TABLE; Schema: haetdeul; Owner: -
--

CREATE TABLE haetdeul.logistics_runtime_fixture (
    fixture_id text NOT NULL,
    sim_run_id text NOT NULL,
    as_of date NOT NULL,
    in_transit_status text NOT NULL,
    in_transit_json jsonb,
    confirmed_inbound_status text NOT NULL,
    confirmed_inbound_json jsonb,
    confirmed_outbound_status text NOT NULL,
    confirmed_outbound_json jsonb,
    usage_scope text NOT NULL,
    evidence_grade text NOT NULL,
    source_ref text NOT NULL,
    approved_by text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    lot_priority_status text,
    lot_priority_json jsonb,
    zone_capacity_status text,
    guaranteed_capacity_by_zone_json jsonb,
    CONSTRAINT ck_log_runtime_in_transit_status CHECK ((in_transit_status = ANY (ARRAY['CONFIRMED'::text, 'CONFIRMED_ZERO'::text, 'UNRESOLVED'::text]))),
    CONSTRAINT ck_log_runtime_inbound_status CHECK ((confirmed_inbound_status = ANY (ARRAY['CONFIRMED'::text, 'CONFIRMED_ZERO'::text, 'UNRESOLVED'::text]))),
    CONSTRAINT ck_log_runtime_outbound_status CHECK ((confirmed_outbound_status = ANY (ARRAY['CONFIRMED'::text, 'CONFIRMED_ZERO'::text, 'UNRESOLVED'::text]))),
    CONSTRAINT ck_logistics_runtime_fixture_lot_priority_status CHECK (((lot_priority_status IS NULL) OR (lot_priority_status = ANY (ARRAY['CONFIRMED'::text, 'CONFIRMED_ZERO'::text, 'UNRESOLVED'::text])))),
    CONSTRAINT ck_logistics_runtime_fixture_zone_capacity_status CHECK (((zone_capacity_status IS NULL) OR (zone_capacity_status = ANY (ARRAY['CONFIRMED'::text, 'CONFIRMED_ZERO'::text, 'UNRESOLVED'::text]))))
);


--
-- Name: market_quotes; Type: TABLE; Schema: haetdeul; Owner: -
--

CREATE TABLE haetdeul.market_quotes (
    quote_id text NOT NULL,
    sim_run_id text,
    day_no integer,
    quote_date date NOT NULL,
    item_id text NOT NULL,
    price_type text NOT NULL,
    grade text NOT NULL,
    variety text,
    market_name text,
    quote_method text NOT NULL,
    unit_price_krw_per_kg numeric(18,6) NOT NULL,
    observed_source_date date NOT NULL,
    fill_status text NOT NULL,
    market_observation_count integer,
    evidence_id text,
    CONSTRAINT market_quotes_check CHECK ((observed_source_date <= quote_date)),
    CONSTRAINT market_quotes_price_type_check CHECK ((price_type = ANY (ARRAY['WHOLESALE'::text, 'AUCTION'::text, 'RETAIL'::text]))),
    CONSTRAINT market_quotes_unit_price_krw_per_kg_check CHECK ((unit_price_krw_per_kg >= (0)::numeric))
);


--
-- Name: TABLE market_quotes; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON TABLE haetdeul.market_quotes IS '외부 시장가격 관측 원장. 실제 매입 체결가격과 분리한다.';


--
-- Name: COLUMN market_quotes.quote_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.market_quotes.quote_id IS '시장가격 관측 고유 ID.';


--
-- Name: COLUMN market_quotes.sim_run_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.market_quotes.sim_run_id IS '시뮬레이션 실행 ID.';


--
-- Name: COLUMN market_quotes.day_no; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.market_quotes.day_no IS '시뮬레이션/Burn-in Day 순번.';


--
-- Name: COLUMN market_quotes.quote_date; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.market_quotes.quote_date IS '시장가격 적용 기준일.';


--
-- Name: COLUMN market_quotes.item_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.market_quotes.item_id IS '품목 고유 ID.';


--
-- Name: COLUMN market_quotes.price_type; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.market_quotes.price_type IS '가격 유형.';


--
-- Name: COLUMN market_quotes.grade; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.market_quotes.grade IS '품질등급.';


--
-- Name: COLUMN market_quotes.variety; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.market_quotes.variety IS '품종.';


--
-- Name: COLUMN market_quotes.market_name; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.market_quotes.market_name IS '시장/조달처 명칭.';


--
-- Name: COLUMN market_quotes.quote_method; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.market_quotes.quote_method IS '대표가격 산출 방식.';


--
-- Name: COLUMN market_quotes.unit_price_krw_per_kg; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.market_quotes.unit_price_krw_per_kg IS '단가(원/kg).';


--
-- Name: COLUMN market_quotes.observed_source_date; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.market_quotes.observed_source_date IS '실제로 사용된 원천 관측일. look-ahead 방지를 위해 quote_date 이하이어야 한다.';


--
-- Name: COLUMN market_quotes.fill_status; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.market_quotes.fill_status IS 'DIRECT 또는 ASOF_PREV_OBSERVATION 등 가격 관측/보정 상태.';


--
-- Name: COLUMN market_quotes.market_observation_count; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.market_quotes.market_observation_count IS '대표가격 산정에 사용한 시장 관측 수.';


--
-- Name: COLUMN market_quotes.evidence_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.market_quotes.evidence_id IS '연결된 근거 원장 ID.';


--
-- Name: ml_price_forecasts; Type: TABLE; Schema: haetdeul; Owner: -
--

CREATE TABLE haetdeul.ml_price_forecasts (
    base_dt date NOT NULL,
    item_nm text NOT NULL,
    target_kind text NOT NULL,
    offset_days smallint NOT NULL,
    target_dt date NOT NULL,
    predicted integer NOT NULL,
    lower integer NOT NULL,
    upper integer NOT NULL,
    current_price integer NOT NULL,
    unit text NOT NULL,
    model_version text NOT NULL,
    generated_at timestamp with time zone NOT NULL,
    src_lead_biz_d smallint NOT NULL,
    is_filled boolean DEFAULT false NOT NULL,
    is_gated boolean DEFAULT false NOT NULL,
    gate_reason text,
    market_name text,
    grade_name text,
    spec_desc text,
    unit_weight_kg numeric(10,3),
    quality_note text,
    use_recommended boolean,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_ml_price_forecasts_band CHECK (((lower <= predicted) AND (predicted <= upper))),
    CONSTRAINT ck_ml_price_forecasts_calendar_axis CHECK ((target_dt = (base_dt + (offset_days)::integer))),
    CONSTRAINT ck_ml_price_forecasts_kind CHECK ((target_kind = ANY (ARRAY['AUC'::text, 'WHSL'::text, 'RTL'::text]))),
    CONSTRAINT ck_ml_price_forecasts_not_future CHECK ((((generated_at AT TIME ZONE 'Asia/Seoul'::text))::date <= base_dt)),
    CONSTRAINT ck_ml_price_forecasts_offset CHECK (((offset_days >= 1) AND (offset_days <= 18))),
    CONSTRAINT ck_ml_price_forecasts_positive CHECK (((predicted > 0) AND (lower > 0) AND (current_price > 0)))
);


--
-- Name: TABLE ml_price_forecasts; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON TABLE haetdeul.ml_price_forecasts IS 'ML 파트 가격 예측. purchase_agent.ports.get_forecast 의 원천. 1행 = 기준일×품목×타겟×D+n(달력일)';


--
-- Name: COLUMN ml_price_forecasts.offset_days; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.ml_price_forecasts.offset_days IS 'D+1~D+18 **달력일**. 우리 원본은 영업일 축이며 변환은 적재 측(ML)이 책임진다';


--
-- Name: COLUMN ml_price_forecasts.current_price; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.ml_price_forecasts.current_price IS '기준일 시점 최신 실제가. 모델이 앵커를 못 이기면 이 값이 곧 답이다';


--
-- Name: COLUMN ml_price_forecasts.is_filled; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.ml_price_forecasts.is_filled IS 'TRUE = 그날 예측이 없어 직전 개장일 값을 끌어온 행 (토·일·공휴일). 판정에 쓰기 전에 확인할 것';


--
-- Name: COLUMN ml_price_forecasts.is_gated; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.ml_price_forecasts.is_gated IS 'TRUE = 모델 대신 앵커를 그대로 쓴 행. LT<3 은 어제 가격이 이미 정답에 가까워 모델이 baseline 보다 나쁘다';


--
-- Name: ml_price_forecasts_history; Type: TABLE; Schema: haetdeul; Owner: -
--

CREATE TABLE haetdeul.ml_price_forecasts_history (
    history_id bigint NOT NULL,
    replaced_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    change_reason text,
    base_dt date NOT NULL,
    item_nm text NOT NULL,
    target_kind text NOT NULL,
    offset_days smallint NOT NULL,
    target_dt date NOT NULL,
    predicted integer NOT NULL,
    lower integer NOT NULL,
    upper integer NOT NULL,
    current_price integer NOT NULL,
    unit text NOT NULL,
    model_version text NOT NULL,
    generated_at timestamp with time zone NOT NULL,
    src_lead_biz_d smallint NOT NULL,
    is_filled boolean NOT NULL,
    is_gated boolean NOT NULL,
    gate_reason text,
    market_name text,
    grade_name text,
    spec_desc text,
    unit_weight_kg numeric(10,3),
    quality_note text,
    use_recommended boolean,
    created_at timestamp with time zone NOT NULL
);


--
-- Name: TABLE ml_price_forecasts_history; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON TABLE haetdeul.ml_price_forecasts_history IS '덮어쓰기로 대체된 예측. 값이 실제로 바뀔 때만 쌓인다. "그날 무엇을 예측했는지" 재현용';


--
-- Name: COLUMN ml_price_forecasts_history.replaced_at; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.ml_price_forecasts_history.replaced_at IS '대체된 시각. 이 시각 이전까지 이 행의 값이 유효했다';


--
-- Name: COLUMN ml_price_forecasts_history.change_reason; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.ml_price_forecasts_history.change_reason IS '무엇이 바뀌었나 — model / generated_at / price / band / quality 조합';


--
-- Name: ml_price_forecasts_history_history_id_seq; Type: SEQUENCE; Schema: haetdeul; Owner: -
--

CREATE SEQUENCE haetdeul.ml_price_forecasts_history_history_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ml_price_forecasts_history_history_id_seq; Type: SEQUENCE OWNED BY; Schema: haetdeul; Owner: -
--

ALTER SEQUENCE haetdeul.ml_price_forecasts_history_history_id_seq OWNED BY haetdeul.ml_price_forecasts_history.history_id;


--
-- Name: partner_item_demands; Type: TABLE; Schema: haetdeul; Owner: -
--

CREATE TABLE haetdeul.partner_item_demands (
    partner_id text NOT NULL,
    item_id text NOT NULL,
    daily_demand_kg numeric(14,3) NOT NULL,
    demand_basis text NOT NULL,
    provisional boolean DEFAULT true NOT NULL,
    CONSTRAINT partner_item_demands_daily_demand_kg_check CHECK ((daily_demand_kg >= (0)::numeric))
);


--
-- Name: TABLE partner_item_demands; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON TABLE haetdeul.partner_item_demands IS '거래처별 품목 일 기준수요를 정규화해 저장하는 관계 테이블.';


--
-- Name: COLUMN partner_item_demands.partner_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.partner_item_demands.partner_id IS '거래 상대방 고유 ID.';


--
-- Name: COLUMN partner_item_demands.item_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.partner_item_demands.item_id IS '품목 고유 ID.';


--
-- Name: COLUMN partner_item_demands.daily_demand_kg; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.partner_item_demands.daily_demand_kg IS '품목별 일 기준수요(kg/일).';


--
-- Name: COLUMN partner_item_demands.demand_basis; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.partner_item_demands.demand_basis IS '수요 산정 근거.';


--
-- Name: COLUMN partner_item_demands.provisional; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.partner_item_demands.provisional IS '잠정/Proxy/Assumption 값 포함 여부.';


--
-- Name: partners; Type: TABLE; Schema: haetdeul; Owner: -
--

CREATE TABLE haetdeul.partners (
    partner_id text NOT NULL,
    partner_name text NOT NULL,
    partner_type text NOT NULL,
    client_type text,
    production_tier text,
    annual_production_ton numeric(12,3),
    operating_days_per_year integer,
    factory_region text,
    factory_city text,
    factory_area text,
    cold_storage_capacity_ton numeric(12,3),
    relationship_days integer,
    order_cycle_days integer,
    sales_collection_days integer,
    pricing_contract_type text,
    active boolean DEFAULT true NOT NULL,
    provisional boolean DEFAULT false NOT NULL,
    note text,
    CONSTRAINT partners_partner_type_check CHECK ((partner_type = ANY (ARRAY['CUSTOMER'::text, 'SUPPLIER'::text, 'LOGISTICS_PROVIDER'::text, 'MARKET_REFERENCE'::text, 'OTHER'::text])))
);


--
-- Name: TABLE partners; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON TABLE haetdeul.partners IS '고객사·공급처·물류사 등 거래 상대방 마스터.';


--
-- Name: COLUMN partners.partner_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.partners.partner_id IS '거래 상대방 고유 ID.';


--
-- Name: COLUMN partners.partner_name; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.partners.partner_name IS '거래 상대방 표시명.';


--
-- Name: COLUMN partners.partner_type; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.partners.partner_type IS '거래 상대방 역할 유형.';


--
-- Name: COLUMN partners.client_type; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.partners.client_type IS '고객사 세부 업종 유형.';


--
-- Name: COLUMN partners.production_tier; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.partners.production_tier IS '고객사 생산규모 구간.';


--
-- Name: COLUMN partners.annual_production_ton; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.partners.annual_production_ton IS '연간 생산량(톤/년).';


--
-- Name: COLUMN partners.operating_days_per_year; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.partners.operating_days_per_year IS '연간 조업일수.';


--
-- Name: COLUMN partners.factory_region; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.partners.factory_region IS '공장 광역지역.';


--
-- Name: COLUMN partners.factory_city; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.partners.factory_city IS '공장 시/군.';


--
-- Name: COLUMN partners.factory_area; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.partners.factory_area IS '공장 세부지역.';


--
-- Name: COLUMN partners.cold_storage_capacity_ton; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.partners.cold_storage_capacity_ton IS '고객사 자체 저온저장능력. 우리 회사 5PL capacity와 별개.';


--
-- Name: COLUMN partners.relationship_days; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.partners.relationship_days IS '거래기간(일).';


--
-- Name: COLUMN partners.order_cycle_days; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.partners.order_cycle_days IS '기준 발주주기(일).';


--
-- Name: COLUMN partners.sales_collection_days; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.partners.sales_collection_days IS 'partners 테이블의 sales_collection_days 값.';


--
-- Name: COLUMN partners.pricing_contract_type; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.partners.pricing_contract_type IS '가격 계약 방식.';


--
-- Name: COLUMN partners.active; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.partners.active IS '활성 여부.';


--
-- Name: COLUMN partners.provisional; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.partners.provisional IS '잠정/Proxy/Assumption 값 포함 여부.';


--
-- Name: COLUMN partners.note; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.partners.note IS '추가 설명 및 주의사항.';


--
-- Name: payables; Type: TABLE; Schema: haetdeul; Owner: -
--

CREATE TABLE haetdeul.payables (
    payable_id text NOT NULL,
    sim_run_id text NOT NULL,
    purchase_id text NOT NULL,
    issued_date date NOT NULL,
    due_date date NOT NULL,
    original_amount_krw numeric(18,6) NOT NULL,
    paid_amount_krw numeric(18,6) DEFAULT 0 NOT NULL,
    outstanding_amount_krw numeric(18,6) NOT NULL,
    status text NOT NULL,
    settled_date date,
    CONSTRAINT payables_check CHECK ((abs(((original_amount_krw - paid_amount_krw) - outstanding_amount_krw)) < 0.1)),
    CONSTRAINT payables_status_check CHECK ((status = ANY (ARRAY['OPEN'::text, 'PARTIAL'::text, 'SETTLED'::text, 'WRITEOFF'::text])))
);


--
-- Name: TABLE payables; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON TABLE haetdeul.payables IS '매입 건별 매입채무/미지급금 원장.';


--
-- Name: COLUMN payables.payable_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.payables.payable_id IS '매입채무 ID.';


--
-- Name: COLUMN payables.sim_run_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.payables.sim_run_id IS '시뮬레이션 실행 ID.';


--
-- Name: COLUMN payables.purchase_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.payables.purchase_id IS '매입 Header ID.';


--
-- Name: COLUMN payables.issued_date; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.payables.issued_date IS '채권/채무 발생일.';


--
-- Name: COLUMN payables.due_date; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.payables.due_date IS '회수/지급 예정일.';


--
-- Name: COLUMN payables.original_amount_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.payables.original_amount_krw IS '최초 발생금액(원).';


--
-- Name: COLUMN payables.paid_amount_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.payables.paid_amount_krw IS '현재까지 지급금액(원).';


--
-- Name: COLUMN payables.outstanding_amount_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.payables.outstanding_amount_krw IS '현재 미회수/미지급 잔액(원).';


--
-- Name: COLUMN payables.status; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.payables.status IS '상태값.';


--
-- Name: COLUMN payables.settled_date; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.payables.settled_date IS '완전 정산일.';


--
-- Name: persona_evidences; Type: TABLE; Schema: haetdeul; Owner: -
--

CREATE TABLE haetdeul.persona_evidences (
    persona_evidence_id bigint NOT NULL,
    company_persona_id text NOT NULL,
    entity_type text NOT NULL,
    entity_key text NOT NULL,
    field_name text NOT NULL,
    evidence_id text NOT NULL,
    evidence_type text NOT NULL,
    provisional boolean DEFAULT true NOT NULL,
    note text
);


--
-- Name: TABLE persona_evidences; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON TABLE haetdeul.persona_evidences IS 'Persona/Agent 정책의 특정 필드와 evidences 근거를 연결하는 Link 테이블.';


--
-- Name: COLUMN persona_evidences.persona_evidence_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.persona_evidences.persona_evidence_id IS 'Persona-근거 연결 ID.';


--
-- Name: COLUMN persona_evidences.company_persona_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.persona_evidences.company_persona_id IS '적용 회사 Persona ID.';


--
-- Name: COLUMN persona_evidences.entity_type; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.persona_evidences.entity_type IS '근거가 연결되는 대상 유형.';


--
-- Name: COLUMN persona_evidences.entity_key; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.persona_evidences.entity_key IS '근거 대상 엔터티 식별자.';


--
-- Name: COLUMN persona_evidences.field_name; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.persona_evidences.field_name IS '근거가 설명하는 필드명.';


--
-- Name: COLUMN persona_evidences.evidence_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.persona_evidences.evidence_id IS '연결된 근거 원장 ID.';


--
-- Name: COLUMN persona_evidences.evidence_type; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.persona_evidences.evidence_type IS '근거 성격/유형.';


--
-- Name: COLUMN persona_evidences.provisional; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.persona_evidences.provisional IS '잠정/Proxy/Assumption 값 포함 여부.';


--
-- Name: COLUMN persona_evidences.note; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.persona_evidences.note IS '추가 설명 및 주의사항.';


--
-- Name: persona_evidences_persona_evidence_id_seq; Type: SEQUENCE; Schema: haetdeul; Owner: -
--

CREATE SEQUENCE haetdeul.persona_evidences_persona_evidence_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: persona_evidences_persona_evidence_id_seq; Type: SEQUENCE OWNED BY; Schema: haetdeul; Owner: -
--

ALTER SEQUENCE haetdeul.persona_evidences_persona_evidence_id_seq OWNED BY haetdeul.persona_evidences.persona_evidence_id;


--
-- Name: proposals; Type: TABLE; Schema: haetdeul; Owner: -
--

CREATE TABLE haetdeul.proposals (
    proposal_id text NOT NULL,
    scenario_id text NOT NULL,
    sim_run_id text NOT NULL,
    purchase_agent_run_id text,
    as_of date NOT NULL,
    item_id text,
    label text NOT NULL,
    total_quantity_kg numeric(18,6),
    max_price_krw_per_kg numeric(18,6),
    timing text,
    split_plan_json jsonb,
    sourcing_plan_json jsonb,
    expected_margin_rate numeric(10,8),
    expected_cost_krw numeric(18,6),
    confidence text,
    situation text,
    status text NOT NULL,
    rationale_json jsonb,
    risks_json jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE proposals; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON TABLE haetdeul.proposals IS '매입 Agent T1 시나리오 결과. 실제 Agent 실행 전에는 0행이 정상이다.';


--
-- Name: COLUMN proposals.proposal_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.proposals.proposal_id IS '매입 Agent 제안 ID.';


--
-- Name: COLUMN proposals.scenario_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.proposals.scenario_id IS '매입 Agent 시나리오 ID.';


--
-- Name: COLUMN proposals.sim_run_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.proposals.sim_run_id IS '시뮬레이션 실행 ID.';


--
-- Name: COLUMN proposals.purchase_agent_run_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.proposals.purchase_agent_run_id IS 'proposals 테이블의 purchase_agent_run_id 값.';


--
-- Name: COLUMN proposals.as_of; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.proposals.as_of IS '판단 시점 기준일.';


--
-- Name: COLUMN proposals.item_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.proposals.item_id IS '품목 고유 ID.';


--
-- Name: COLUMN proposals.label; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.proposals.label IS '표시명.';


--
-- Name: COLUMN proposals.total_quantity_kg; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.proposals.total_quantity_kg IS '총수량(kg).';


--
-- Name: COLUMN proposals.max_price_krw_per_kg; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.proposals.max_price_krw_per_kg IS '허용 최대단가(원/kg).';


--
-- Name: COLUMN proposals.timing; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.proposals.timing IS '매입/입고 시점 요약.';


--
-- Name: COLUMN proposals.split_plan_json; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.proposals.split_plan_json IS '분할 매입/입고 일정과 회차별 수량 계획 JSON.';


--
-- Name: COLUMN proposals.sourcing_plan_json; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.proposals.sourcing_plan_json IS '시장×등급×수량×단가 조달계획 JSON.';


--
-- Name: COLUMN proposals.expected_margin_rate; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.proposals.expected_margin_rate IS '기대마진율.';


--
-- Name: COLUMN proposals.expected_cost_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.proposals.expected_cost_krw IS '매입 Agent 예상원가. 재무 Agent는 실제 Line 기준으로 재계산해야 한다.';


--
-- Name: COLUMN proposals.confidence; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.proposals.confidence IS '신뢰도/불확실성 정보.';


--
-- Name: COLUMN proposals.situation; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.proposals.situation IS '판단 대상 상황 설명.';


--
-- Name: COLUMN proposals.status; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.proposals.status IS '상태값.';


--
-- Name: COLUMN proposals.rationale_json; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.proposals.rationale_json IS '구조화된 판단근거 JSON.';


--
-- Name: COLUMN proposals.risks_json; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.proposals.risks_json IS '식별된 위험요인 JSON.';


--
-- Name: COLUMN proposals.created_at; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.proposals.created_at IS '생성시각.';


--
-- Name: purchase_items; Type: TABLE; Schema: haetdeul; Owner: -
--

CREATE TABLE haetdeul.purchase_items (
    purchase_item_id text NOT NULL,
    purchase_id text NOT NULL,
    item_id text NOT NULL,
    grade text,
    market_name text,
    quantity_kg numeric(18,6) NOT NULL,
    unit_price_krw_per_kg numeric(18,6) NOT NULL,
    line_amount_krw numeric(18,6) NOT NULL,
    source_quote_id text,
    CONSTRAINT purchase_items_check CHECK ((abs((line_amount_krw - (quantity_kg * unit_price_krw_per_kg))) < 0.1)),
    CONSTRAINT purchase_items_line_amount_krw_check CHECK ((line_amount_krw >= (0)::numeric)),
    CONSTRAINT purchase_items_quantity_kg_check CHECK ((quantity_kg >= (0)::numeric)),
    CONSTRAINT purchase_items_unit_price_krw_per_kg_check CHECK ((unit_price_krw_per_kg >= (0)::numeric))
);


--
-- Name: TABLE purchase_items; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON TABLE haetdeul.purchase_items IS '매입 Detail. 품목·등급·시장·수량·단가·Line 금액을 저장한다.';


--
-- Name: COLUMN purchase_items.purchase_item_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.purchase_items.purchase_item_id IS '매입 Detail ID.';


--
-- Name: COLUMN purchase_items.purchase_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.purchase_items.purchase_id IS '매입 Header ID.';


--
-- Name: COLUMN purchase_items.item_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.purchase_items.item_id IS '품목 고유 ID.';


--
-- Name: COLUMN purchase_items.grade; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.purchase_items.grade IS '품질등급.';


--
-- Name: COLUMN purchase_items.market_name; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.purchase_items.market_name IS '시장/조달처 명칭.';


--
-- Name: COLUMN purchase_items.quantity_kg; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.purchase_items.quantity_kg IS '수량(kg).';


--
-- Name: COLUMN purchase_items.unit_price_krw_per_kg; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.purchase_items.unit_price_krw_per_kg IS '단가(원/kg).';


--
-- Name: COLUMN purchase_items.line_amount_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.purchase_items.line_amount_krw IS '매입 Line 금액. quantity_kg×unit_price_krw_per_kg와 일치해야 한다.';


--
-- Name: COLUMN purchase_items.source_quote_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.purchase_items.source_quote_id IS '연결된 시장가격 관측 ID.';


--
-- Name: purchases; Type: TABLE; Schema: haetdeul; Owner: -
--

CREATE TABLE haetdeul.purchases (
    purchase_id text NOT NULL,
    sim_run_id text NOT NULL,
    supplier_partner_id text,
    purchase_date date NOT NULL,
    payment_due_date date NOT NULL,
    purchase_type text NOT NULL,
    source_market_label text,
    total_amount_krw numeric(18,6) NOT NULL,
    settlement_status text NOT NULL,
    proposal_id text,
    scenario_id text,
    source_event_id text,
    evidence_id text,
    note text,
    CONSTRAINT purchases_settlement_status_check CHECK ((settlement_status = ANY (ARRAY['SETTLED'::text, 'OPEN'::text, 'CANCELLED'::text]))),
    CONSTRAINT purchases_total_amount_krw_check CHECK ((total_amount_krw >= (0)::numeric))
);


--
-- Name: TABLE purchases; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON TABLE haetdeul.purchases IS '매입 Header. 날짜·총액·정산상태·Agent 제안 연결정보를 저장한다.';


--
-- Name: COLUMN purchases.purchase_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.purchases.purchase_id IS '매입 Header ID.';


--
-- Name: COLUMN purchases.sim_run_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.purchases.sim_run_id IS '시뮬레이션 실행 ID.';


--
-- Name: COLUMN purchases.supplier_partner_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.purchases.supplier_partner_id IS '매입 공급처 Partner ID.';


--
-- Name: COLUMN purchases.purchase_date; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.purchases.purchase_date IS '매입일.';


--
-- Name: COLUMN purchases.payment_due_date; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.purchases.payment_due_date IS '매입대금 지급예정일.';


--
-- Name: COLUMN purchases.purchase_type; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.purchases.purchase_type IS '매입 유형.';


--
-- Name: COLUMN purchases.source_market_label; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.purchases.source_market_label IS '매입가격 산정에 사용한 시장 기준 설명.';


--
-- Name: COLUMN purchases.total_amount_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.purchases.total_amount_krw IS '매입 Header 총액. purchase_items.line_amount_krw 합계와 일치해야 한다.';


--
-- Name: COLUMN purchases.settlement_status; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.purchases.settlement_status IS '매입 정산상태.';


--
-- Name: COLUMN purchases.proposal_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.purchases.proposal_id IS '매입 Agent 제안 ID.';


--
-- Name: COLUMN purchases.scenario_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.purchases.scenario_id IS '매입 Agent 시나리오 ID.';


--
-- Name: COLUMN purchases.source_event_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.purchases.source_event_id IS '원본/Burn-in 이벤트 ID.';


--
-- Name: COLUMN purchases.evidence_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.purchases.evidence_id IS '연결된 근거 원장 ID.';


--
-- Name: COLUMN purchases.note; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.purchases.note IS '추가 설명 및 주의사항.';


--
-- Name: receivables; Type: TABLE; Schema: haetdeul; Owner: -
--

CREATE TABLE haetdeul.receivables (
    receivable_id text NOT NULL,
    sim_run_id text NOT NULL,
    sale_id text NOT NULL,
    issued_date date NOT NULL,
    due_date date NOT NULL,
    original_amount_krw numeric(18,6) NOT NULL,
    received_amount_krw numeric(18,6) DEFAULT 0 NOT NULL,
    outstanding_amount_krw numeric(18,6) NOT NULL,
    status text NOT NULL,
    CONSTRAINT receivables_check CHECK ((abs(((original_amount_krw - received_amount_krw) - outstanding_amount_krw)) < 0.1)),
    CONSTRAINT receivables_status_check CHECK ((status = ANY (ARRAY['OPEN'::text, 'PARTIAL'::text, 'COLLECTED'::text, 'WRITEOFF'::text])))
);


--
-- Name: TABLE receivables; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON TABLE haetdeul.receivables IS '판매 건별 매출채권/미수금 원장.';


--
-- Name: COLUMN receivables.receivable_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.receivables.receivable_id IS '매출채권 ID.';


--
-- Name: COLUMN receivables.sim_run_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.receivables.sim_run_id IS '시뮬레이션 실행 ID.';


--
-- Name: COLUMN receivables.sale_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.receivables.sale_id IS '판매 Header ID.';


--
-- Name: COLUMN receivables.issued_date; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.receivables.issued_date IS '채권/채무 발생일.';


--
-- Name: COLUMN receivables.due_date; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.receivables.due_date IS '회수/지급 예정일.';


--
-- Name: COLUMN receivables.original_amount_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.receivables.original_amount_krw IS '최초 발생금액(원).';


--
-- Name: COLUMN receivables.received_amount_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.receivables.received_amount_krw IS '현재까지 회수금액(원).';


--
-- Name: COLUMN receivables.outstanding_amount_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.receivables.outstanding_amount_krw IS '현재 미회수/미지급 잔액(원).';


--
-- Name: COLUMN receivables.status; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.receivables.status IS '상태값.';


--
-- Name: sale_items; Type: TABLE; Schema: haetdeul; Owner: -
--

CREATE TABLE haetdeul.sale_items (
    sale_item_id text NOT NULL,
    sale_id text NOT NULL,
    item_id text NOT NULL,
    grade text,
    quantity_kg numeric(18,6) NOT NULL,
    unit_price_krw_per_kg numeric(18,6) NOT NULL,
    line_amount_krw numeric(18,6) NOT NULL,
    contribution_profit_krw numeric(18,6) NOT NULL,
    contribution_margin_rate numeric(10,8),
    CONSTRAINT sale_items_check CHECK ((abs((line_amount_krw - (quantity_kg * unit_price_krw_per_kg))) < 0.1)),
    CONSTRAINT sale_items_line_amount_krw_check CHECK ((line_amount_krw >= (0)::numeric)),
    CONSTRAINT sale_items_quantity_kg_check CHECK ((quantity_kg >= (0)::numeric)),
    CONSTRAINT sale_items_unit_price_krw_per_kg_check CHECK ((unit_price_krw_per_kg >= (0)::numeric))
);


--
-- Name: TABLE sale_items; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON TABLE haetdeul.sale_items IS '판매 Detail. 판매 건에 포함된 품목별 수량·단가·금액·기여이익을 저장한다.';


--
-- Name: COLUMN sale_items.sale_item_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.sale_items.sale_item_id IS '판매 Detail ID.';


--
-- Name: COLUMN sale_items.sale_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.sale_items.sale_id IS '판매 Header ID.';


--
-- Name: COLUMN sale_items.item_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.sale_items.item_id IS '품목 고유 ID.';


--
-- Name: COLUMN sale_items.grade; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.sale_items.grade IS '품질등급.';


--
-- Name: COLUMN sale_items.quantity_kg; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.sale_items.quantity_kg IS '수량(kg).';


--
-- Name: COLUMN sale_items.unit_price_krw_per_kg; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.sale_items.unit_price_krw_per_kg IS '단가(원/kg).';


--
-- Name: COLUMN sale_items.line_amount_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.sale_items.line_amount_krw IS 'sale_items 테이블의 line_amount_krw 값.';


--
-- Name: COLUMN sale_items.contribution_profit_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.sale_items.contribution_profit_krw IS '기여이익(원).';


--
-- Name: COLUMN sale_items.contribution_margin_rate; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.sale_items.contribution_margin_rate IS '기여이익률(0~1).';


--
-- Name: sales; Type: TABLE; Schema: haetdeul; Owner: -
--

CREATE TABLE haetdeul.sales (
    sale_id text NOT NULL,
    sim_run_id text NOT NULL,
    customer_partner_id text NOT NULL,
    order_date date NOT NULL,
    sale_date date NOT NULL,
    collection_due_date date NOT NULL,
    total_quantity_kg numeric(18,6) NOT NULL,
    total_amount_krw numeric(18,6) NOT NULL,
    contribution_profit_krw numeric(18,6) NOT NULL,
    collection_status text NOT NULL,
    source_order_id text NOT NULL,
    note text,
    order_status character varying(30) DEFAULT 'DELIVERED'::character varying NOT NULL,
    CONSTRAINT sales_collection_status_check CHECK ((collection_status = ANY (ARRAY['OPEN'::text, 'PARTIAL'::text, 'COLLECTED'::text, 'CANCELLED'::text]))),
    CONSTRAINT sales_order_status_check CHECK (((order_status)::text = ANY ((ARRAY['CONFIRMED'::character varying, 'READY'::character varying, 'DELIVERED'::character varying, 'CANCELLED'::character varying])::text[])))
);


--
-- Name: TABLE sales; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON TABLE haetdeul.sales IS '판매/주문 Header. 거래처·일자·총수량·총금액·기여이익·회수상태를 저장한다.';


--
-- Name: COLUMN sales.sale_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.sales.sale_id IS '판매 Header ID.';


--
-- Name: COLUMN sales.sim_run_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.sales.sim_run_id IS '시뮬레이션 실행 ID.';


--
-- Name: COLUMN sales.customer_partner_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.sales.customer_partner_id IS '판매 고객 Partner ID.';


--
-- Name: COLUMN sales.order_date; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.sales.order_date IS '고객 주문일.';


--
-- Name: COLUMN sales.sale_date; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.sales.sale_date IS '판매/납품 기준일.';


--
-- Name: COLUMN sales.collection_due_date; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.sales.collection_due_date IS '판매대금 회수예정일.';


--
-- Name: COLUMN sales.total_quantity_kg; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.sales.total_quantity_kg IS '판매 Header 총수량. sale_items.quantity_kg 합계와 일치해야 한다.';


--
-- Name: COLUMN sales.total_amount_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.sales.total_amount_krw IS '판매 Header 총금액. sale_items.line_amount_krw 합계와 일치해야 한다.';


--
-- Name: COLUMN sales.contribution_profit_krw; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.sales.contribution_profit_krw IS '기여이익(원).';


--
-- Name: COLUMN sales.collection_status; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.sales.collection_status IS '판매대금 회수상태.';


--
-- Name: COLUMN sales.source_order_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.sales.source_order_id IS '원본 주문 ID.';


--
-- Name: COLUMN sales.note; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.sales.note IS '추가 설명 및 주의사항.';


--
-- Name: sim_runs; Type: TABLE; Schema: haetdeul; Owner: -
--

CREATE TABLE haetdeul.sim_runs (
    sim_run_id text NOT NULL,
    company_persona_id text NOT NULL,
    run_type text NOT NULL,
    period_start date NOT NULL,
    period_end date NOT NULL,
    as_of date NOT NULL,
    status text NOT NULL,
    financing_mode text NOT NULL,
    config_json jsonb NOT NULL,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    note text
);


--
-- Name: TABLE sim_runs; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON TABLE haetdeul.sim_runs IS '시뮬레이션/백테스트 최상위 실행 단위. 실행별 Persona와 config를 고정한다.';


--
-- Name: COLUMN sim_runs.sim_run_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.sim_runs.sim_run_id IS '시뮬레이션 실행 ID.';


--
-- Name: COLUMN sim_runs.company_persona_id; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.sim_runs.company_persona_id IS '적용 회사 Persona ID.';


--
-- Name: COLUMN sim_runs.run_type; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.sim_runs.run_type IS '시뮬레이션 실행유형.';


--
-- Name: COLUMN sim_runs.period_start; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.sim_runs.period_start IS '실행 기간 시작일.';


--
-- Name: COLUMN sim_runs.period_end; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.sim_runs.period_end IS '실행 기간 종료일.';


--
-- Name: COLUMN sim_runs.as_of; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.sim_runs.as_of IS '판단 시점 기준일.';


--
-- Name: COLUMN sim_runs.status; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.sim_runs.status IS '상태값.';


--
-- Name: COLUMN sim_runs.financing_mode; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.sim_runs.financing_mode IS '자금조달 Scenario.';


--
-- Name: COLUMN sim_runs.config_json; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.sim_runs.config_json IS '실행 당시 Persona/정책/가정을 고정한 설정 Snapshot.';


--
-- Name: COLUMN sim_runs.started_at; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.sim_runs.started_at IS '실행 시작시각.';


--
-- Name: COLUMN sim_runs.finished_at; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.sim_runs.finished_at IS '실행 종료시각.';


--
-- Name: COLUMN sim_runs.note; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON COLUMN haetdeul.sim_runs.note IS '추가 설명 및 주의사항.';


--
-- Name: v_current_finance_state; Type: VIEW; Schema: haetdeul; Owner: -
--

CREATE VIEW haetdeul.v_current_finance_state AS
 SELECT finance_state_id,
    sim_run_id,
    state_date,
    state_type,
    financing_mode,
    current_cash_krw,
    minimum_operating_cash_krw,
    committed_outflows_krw,
    unsettled_purchase_payables_krw,
    receivables_krw,
    inventory_book_value_krw,
    operational_inventory_value_krw,
    current_debt_krw,
    recommended_loan_amount_krw,
    financial_limit_krw,
    note
   FROM haetdeul.finance_states
  WHERE (finance_state_id = 'FIN-DAY30-LOAN'::text);


--
-- Name: VIEW v_current_finance_state; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON VIEW haetdeul.v_current_finance_state IS '현재 Agent/UI가 사용하는 Day30 대출 Baseline 재무상태 View.';


--
-- Name: v_current_inventory; Type: VIEW; Schema: haetdeul; Owner: -
--

CREATE VIEW haetdeul.v_current_inventory AS
 SELECT l.lot_id,
    l.item_id,
    i.item_name,
    l.grade,
    l.received_at,
    l.remaining_qty_kg,
    l.unit_cost_krw_per_kg,
    l.storage_zone,
    p.operational_limit_days,
    p.disposal_candidate_days,
    p.medium_grade_factor,
    ((
        CASE
            WHEN (l.grade = '중'::text) THEN floor(((p.operational_limit_days)::numeric * p.medium_grade_factor))
            ELSE (p.operational_limit_days)::numeric
        END - ((( SELECT sim_runs.as_of
           FROM haetdeul.sim_runs
          WHERE (sim_runs.sim_run_id = 'SIM-BURNIN-202512'::text)) - l.received_at))::numeric))::integer AS freshness_days_left,
    l.status
   FROM ((haetdeul.inventory_lots l
     JOIN haetdeul.items i ON ((i.item_id = l.item_id)))
     JOIN haetdeul.item_storage_policies p ON ((p.item_id = l.item_id)))
  WHERE ((l.sim_run_id = 'SIM-BURNIN-202512'::text) AND (l.remaining_qty_kg > (0)::numeric));


--
-- Name: VIEW v_current_inventory; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON VIEW haetdeul.v_current_inventory IS '현재 활성 재고 Lot과 as_of 기준 잔여신선도를 계산한 View.';


--
-- Name: v_current_logistics_capacity; Type: VIEW; Schema: haetdeul; Owner: -
--

CREATE VIEW haetdeul.v_current_logistics_capacity AS
 SELECT lc.guaranteed_capacity_plt,
    lc.effective_kg_per_pallet,
    lc.equivalent_capacity_ton,
    COALESCE(sum(ci.remaining_qty_kg), (0)::numeric) AS used_capacity_kg,
    ((lc.equivalent_capacity_ton * (1000)::numeric) - COALESCE(sum(ci.remaining_qty_kg), (0)::numeric)) AS free_capacity_kg,
    (COALESCE(sum(ci.remaining_qty_kg), (0)::numeric) / lc.effective_kg_per_pallet) AS used_plt_equivalent
   FROM (haetdeul.logistics_contracts lc
     LEFT JOIN haetdeul.v_current_inventory ci ON (true))
  WHERE (lc.logistics_contract_id = 'LOGI-BASE-5PL'::text)
  GROUP BY lc.guaranteed_capacity_plt, lc.effective_kg_per_pallet, lc.equivalent_capacity_ton;


--
-- Name: VIEW v_current_logistics_capacity; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON VIEW haetdeul.v_current_logistics_capacity IS '5PL 보장용량에서 현재 재고를 차감해 사용/여유 capacity를 계산한 View.';


--
-- Name: v_current_partner_demand; Type: VIEW; Schema: haetdeul; Owner: -
--

CREATE VIEW haetdeul.v_current_partner_demand AS
 SELECT p.partner_id,
    p.partner_name,
    p.order_cycle_days,
    sum(d.daily_demand_kg) AS daily_total_demand_kg,
    (sum(d.daily_demand_kg) * (p.order_cycle_days)::numeric) AS baseline_order_qty_kg,
    p.sales_collection_days,
    p.pricing_contract_type
   FROM (haetdeul.partners p
     JOIN haetdeul.partner_item_demands d ON ((d.partner_id = p.partner_id)))
  WHERE (p.active = true)
  GROUP BY p.partner_id, p.partner_name, p.order_cycle_days, p.sales_collection_days, p.pricing_contract_type;


--
-- Name: VIEW v_current_partner_demand; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON VIEW haetdeul.v_current_partner_demand IS '활성 고객의 일수요와 발주주기로 기준 주문량을 계산한 View.';


--
-- Name: v_dashboard_state; Type: VIEW; Schema: haetdeul; Owner: -
--

CREATE VIEW haetdeul.v_dashboard_state AS
 SELECT s.as_of,
    f.current_cash_krw,
    f.financial_limit_krw,
    f.receivables_krw,
    f.inventory_book_value_krw,
    lc.used_capacity_kg,
    lc.free_capacity_kg,
    lc.equivalent_capacity_ton,
    pd.baseline_order_qty_kg,
    ( SELECT count(*) AS count
           FROM haetdeul.partners
          WHERE ((partners.active = true) AND (partners.partner_type = 'CUSTOMER'::text))) AS active_customer_count,
    ( SELECT logistics_contracts.own_vehicle_count
           FROM haetdeul.logistics_contracts
          WHERE (logistics_contracts.logistics_contract_id = 'LOGI-BASE-5PL'::text)) AS own_vehicle_count,
    '5PL_NETWORK_ORCHESTRATION'::text AS logistics_model
   FROM (((haetdeul.sim_runs s
     CROSS JOIN haetdeul.v_current_finance_state f)
     CROSS JOIN haetdeul.v_current_logistics_capacity lc)
     CROSS JOIN haetdeul.v_current_partner_demand pd)
  WHERE (s.sim_run_id = 'SIM-BURNIN-202512'::text);


--
-- Name: VIEW v_dashboard_state; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON VIEW haetdeul.v_dashboard_state IS 'UI Mock을 대체하는 현재 회사상태 View. 현금·재고·5PL·고객 수치를 DB 원천값에서 제공한다.';


--
-- Name: v_ml_forecast_revisions; Type: VIEW; Schema: haetdeul; Owner: -
--

CREATE VIEW haetdeul.v_ml_forecast_revisions AS
 SELECT base_dt,
    item_nm,
    target_kind,
    count(*) AS "대체횟수",
    min(replaced_at) AS "첫대체",
    max(replaced_at) AS "마지막대체",
    string_agg(DISTINCT change_reason, ' / '::text) AS "바뀐것",
    string_agg(DISTINCT model_version, ' -> '::text) AS "거쳐간모델"
   FROM haetdeul.ml_price_forecasts_history
  GROUP BY base_dt, item_nm, target_kind;


--
-- Name: VIEW v_ml_forecast_revisions; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON VIEW haetdeul.v_ml_forecast_revisions IS '기준일별 덮어쓰기 횟수와 사유. 대체횟수가 0이 아니면 그날 예측이 바뀐 적이 있다';


--
-- Name: v_ml_price_forecast; Type: VIEW; Schema: haetdeul; Owner: -
--

CREATE VIEW haetdeul.v_ml_price_forecast AS
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


--
-- Name: VIEW v_ml_price_forecast; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON VIEW haetdeul.v_ml_price_forecast IS 'get_forecast 계약 형태. daily 는 D+1~D+18 연속 달력일. has_filled_rows 가 TRUE 면 채운 행이 섞여 있다';


--
-- Name: v_seed_validation; Type: VIEW; Schema: haetdeul; Owner: -
--

CREATE VIEW haetdeul.v_seed_validation AS
 SELECT '01_table_count_27'::text AS check_name,
    '27'::text AS expected,
    (count(*))::text AS actual,
    (count(*) = 27) AS passed
   FROM information_schema.tables
  WHERE (((tables.table_schema)::name = 'haetdeul'::name) AND ((tables.table_type)::text = 'BASE TABLE'::text))
UNION ALL
 SELECT '02_items_5'::text AS check_name,
    '5'::text AS expected,
    (count(*))::text AS actual,
    (count(*) = 5) AS passed
   FROM haetdeul.items
UNION ALL
 SELECT '03_partners_1_known_customer'::text AS check_name,
    '1'::text AS expected,
    (count(*))::text AS actual,
    (count(*) = 1) AS passed
   FROM haetdeul.partners
UNION ALL
 SELECT '04_market_quotes_150'::text AS check_name,
    '150'::text AS expected,
    (count(*))::text AS actual,
    (count(*) = 150) AS passed
   FROM haetdeul.market_quotes
UNION ALL
 SELECT '05_forecasts_empty_before_ml'::text AS check_name,
    '0'::text AS expected,
    (count(*))::text AS actual,
    (count(*) = 0) AS passed
   FROM haetdeul.forecasts
UNION ALL
 SELECT '06_purchases_16'::text AS check_name,
    '16'::text AS expected,
    (count(*))::text AS actual,
    (count(*) = 16) AS passed
   FROM haetdeul.purchases
UNION ALL
 SELECT '07_purchase_items_80'::text AS check_name,
    '80'::text AS expected,
    (count(*))::text AS actual,
    (count(*) = 80) AS passed
   FROM haetdeul.purchase_items
UNION ALL
 SELECT '08_sales_15'::text AS check_name,
    '15'::text AS expected,
    (count(*))::text AS actual,
    (count(*) = 15) AS passed
   FROM haetdeul.sales
UNION ALL
 SELECT '09_sale_items_75'::text AS check_name,
    '75'::text AS expected,
    (count(*))::text AS actual,
    (count(*) = 75) AS passed
   FROM haetdeul.sale_items
UNION ALL
 SELECT '10_inventory_lots_80'::text AS check_name,
    '80'::text AS expected,
    (count(*))::text AS actual,
    (count(*) = 80) AS passed
   FROM haetdeul.inventory_lots
UNION ALL
 SELECT '11_inventory_moves_230'::text AS check_name,
    '230'::text AS expected,
    (count(*))::text AS actual,
    (count(*) = 230) AS passed
   FROM haetdeul.inventory_moves
UNION ALL
 SELECT '12_deliveries_15'::text AS check_name,
    '15'::text AS expected,
    (count(*))::text AS actual,
    (count(*) = 15) AS passed
   FROM haetdeul.deliveries
UNION ALL
 SELECT '13_logistics_contract_1'::text AS check_name,
    '1'::text AS expected,
    (count(*))::text AS actual,
    (count(*) = 1) AS passed
   FROM haetdeul.logistics_contracts
UNION ALL
 SELECT '14_expenses_17'::text AS check_name,
    '17'::text AS expected,
    (count(*))::text AS actual,
    (count(*) = 17) AS passed
   FROM haetdeul.expenses
UNION ALL
 SELECT '15_receivables_15'::text AS check_name,
    '15'::text AS expected,
    (count(*))::text AS actual,
    (count(*) = 15) AS passed
   FROM haetdeul.receivables
UNION ALL
 SELECT '16_payables_16'::text AS check_name,
    '16'::text AS expected,
    (count(*))::text AS actual,
    (count(*) = 16) AS passed
   FROM haetdeul.payables
UNION ALL
 SELECT '17_daily_closings_30'::text AS check_name,
    '30'::text AS expected,
    (count(*))::text AS actual,
    (count(*) = 30) AS passed
   FROM haetdeul.daily_closings
UNION ALL
 SELECT '18_finance_states_3'::text AS check_name,
    '3'::text AS expected,
    (count(*))::text AS actual,
    (count(*) = 3) AS passed
   FROM haetdeul.finance_states
UNION ALL
 SELECT '19_agent_policies_7'::text AS check_name,
    '7'::text AS expected,
    (count(*))::text AS actual,
    (count(*) = 7) AS passed
   FROM haetdeul.agent_policies
UNION ALL
 SELECT '20_agent_runs_empty_before_runtime'::text AS check_name,
    '0'::text AS expected,
    (count(*))::text AS actual,
    (count(*) = 0) AS passed
   FROM haetdeul.agent_runs
UNION ALL
 SELECT '21_proposals_empty_before_runtime'::text AS check_name,
    '0'::text AS expected,
    (count(*))::text AS actual,
    (count(*) = 0) AS passed
   FROM haetdeul.proposals
UNION ALL
 SELECT '22_constraint_reviews_empty_before_runtime'::text AS check_name,
    '0'::text AS expected,
    (count(*))::text AS actual,
    (count(*) = 0) AS passed
   FROM haetdeul.constraint_reviews
UNION ALL
 SELECT '23_sim_runs_1'::text AS check_name,
    '1'::text AS expected,
    (count(*))::text AS actual,
    (count(*) = 1) AS passed
   FROM haetdeul.sim_runs
UNION ALL
 SELECT '24_purchase_header_detail_match'::text AS check_name,
    '0'::text AS expected,
    (count(*))::text AS actual,
    (count(*) = 0) AS passed
   FROM ( SELECT p.purchase_id
           FROM (haetdeul.purchases p
             JOIN haetdeul.purchase_items pi ON ((pi.purchase_id = p.purchase_id)))
          GROUP BY p.purchase_id, p.total_amount_krw
         HAVING (abs((p.total_amount_krw - sum(pi.line_amount_krw))) >= 0.1)) x
UNION ALL
 SELECT '25_sales_header_detail_match'::text AS check_name,
    '0'::text AS expected,
    (count(*))::text AS actual,
    (count(*) = 0) AS passed
   FROM ( SELECT s.sale_id
           FROM (haetdeul.sales s
             JOIN haetdeul.sale_items si ON ((si.sale_id = s.sale_id)))
          GROUP BY s.sale_id, s.total_amount_krw, s.total_quantity_kg
         HAVING ((abs((s.total_amount_krw - sum(si.line_amount_krw))) >= 0.1) OR (abs((s.total_quantity_kg - sum(si.quantity_kg))) >= 0.01))) x
UNION ALL
 SELECT '26_inventory_lot_move_balance'::text AS check_name,
    '0'::text AS expected,
    (count(*))::text AS actual,
    (count(*) = 0) AS passed
   FROM ( SELECT l.lot_id
           FROM (haetdeul.inventory_lots l
             LEFT JOIN haetdeul.inventory_moves m ON ((m.lot_id = l.lot_id)))
          GROUP BY l.lot_id, l.remaining_qty_kg
         HAVING (abs((l.remaining_qty_kg - (COALESCE(sum(
                CASE
                    WHEN (m.move_type = 'IN'::text) THEN m.quantity_kg
                    ELSE (0)::numeric
                END), (0)::numeric) - COALESCE(sum(
                CASE
                    WHEN (m.move_type = ANY (ARRAY['OUT'::text, 'DISPOSE'::text])) THEN m.quantity_kg
                    ELSE (0)::numeric
                END), (0)::numeric)))) >= 0.01)) x
UNION ALL
 SELECT '27_day30_inventory_375.4kg'::text AS check_name,
    '375.4'::text AS expected,
    (round(COALESCE(sum(inventory_lots.remaining_qty_kg), (0)::numeric), 3))::text AS actual,
    (abs((COALESCE(sum(inventory_lots.remaining_qty_kg), (0)::numeric) - 375.4)) < 0.01) AS passed
   FROM haetdeul.inventory_lots
  WHERE ((inventory_lots.sim_run_id = 'SIM-BURNIN-202512'::text) AND (inventory_lots.remaining_qty_kg > (0)::numeric))
UNION ALL
 SELECT '28_sales_total_equals_receivables'::text AS check_name,
    '73051531.25'::text AS expected,
    (round(sum(receivables.outstanding_amount_krw), 2))::text AS actual,
    (abs((sum(receivables.outstanding_amount_krw) - 73051531.25)) < 0.1) AS passed
   FROM haetdeul.receivables
UNION ALL
 SELECT '29_open_payables_zero'::text AS check_name,
    '0'::text AS expected,
    (round(sum(payables.outstanding_amount_krw), 2))::text AS actual,
    (abs(sum(payables.outstanding_amount_krw)) < 0.1) AS passed
   FROM haetdeul.payables
UNION ALL
 SELECT '30_no_market_lookahead'::text AS check_name,
    '0'::text AS expected,
    (count(*))::text AS actual,
    (count(*) = 0) AS passed
   FROM haetdeul.market_quotes
  WHERE (market_quotes.observed_source_date > market_quotes.quote_date)
UNION ALL
 SELECT '31_day30_loan_cash'::text AS check_name,
    '31993913.77'::text AS expected,
    (round(finance_states.current_cash_krw, 2))::text AS actual,
    (abs((finance_states.current_cash_krw - 31993913.77)) < 0.1) AS passed
   FROM haetdeul.finance_states
  WHERE (finance_states.finance_state_id = 'FIN-DAY30-LOAN'::text)
UNION ALL
 SELECT '32_minimum_operating_cash'::text AS check_name,
    '15902640'::text AS expected,
    (round(finance_states.minimum_operating_cash_krw, 2))::text AS actual,
    (abs((finance_states.minimum_operating_cash_krw - (15902640)::numeric)) < 0.1) AS passed
   FROM haetdeul.finance_states
  WHERE (finance_states.finance_state_id = 'FIN-DAY30-LOAN'::text)
UNION ALL
 SELECT '33_5pl_capacity_8plt'::text AS check_name,
    '8'::text AS expected,
    (logistics_contracts.guaranteed_capacity_plt)::text AS actual,
    (logistics_contracts.guaranteed_capacity_plt = (8)::numeric) AS passed
   FROM haetdeul.logistics_contracts
  WHERE (logistics_contracts.logistics_contract_id = 'LOGI-BASE-5PL'::text)
UNION ALL
 SELECT '34_5pl_equivalent_capacity_6.4t'::text AS check_name,
    '6.4'::text AS expected,
    (logistics_contracts.equivalent_capacity_ton)::text AS actual,
    (abs((logistics_contracts.equivalent_capacity_ton - 6.4)) < 0.0001) AS passed
   FROM haetdeul.logistics_contracts
  WHERE (logistics_contracts.logistics_contract_id = 'LOGI-BASE-5PL'::text)
UNION ALL
 SELECT '35_own_vehicle_zero'::text AS check_name,
    '0'::text AS expected,
    (logistics_contracts.own_vehicle_count)::text AS actual,
    (logistics_contracts.own_vehicle_count = 0) AS passed
   FROM haetdeul.logistics_contracts
  WHERE (logistics_contracts.logistics_contract_id = 'LOGI-BASE-5PL'::text)
UNION ALL
 SELECT '36_no_ui_mock_cash_82400000'::text AS check_name,
    '0'::text AS expected,
    (count(*))::text AS actual,
    (count(*) = 0) AS passed
   FROM haetdeul.finance_states
  WHERE (abs((finance_states.current_cash_krw - (82400000)::numeric)) < 0.1)
UNION ALL
 SELECT '37_no_ui_mock_capacity_60t'::text AS check_name,
    '0'::text AS expected,
    (count(*))::text AS actual,
    (count(*) = 0) AS passed
   FROM haetdeul.logistics_contracts
  WHERE (abs((logistics_contracts.equivalent_capacity_ton - (60)::numeric)) < 0.001)
UNION ALL
 SELECT '38_no_ui_mock_truck_3'::text AS check_name,
    '0'::text AS expected,
    (count(*))::text AS actual,
    (count(*) = 0) AS passed
   FROM haetdeul.logistics_contracts
  WHERE (logistics_contracts.own_vehicle_count = 3);


--
-- Name: VIEW v_seed_validation; Type: COMMENT; Schema: haetdeul; Owner: -
--

COMMENT ON VIEW haetdeul.v_seed_validation IS '27개 테이블 Seed와 데이터 정합성을 검증하는 View.';


--
-- Name: agent_policy_config policy_id; Type: DEFAULT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.agent_policy_config ALTER COLUMN policy_id SET DEFAULT nextval('haetdeul.agent_policy_config_policy_id_seq'::regclass);


--
-- Name: ml_price_forecasts_history history_id; Type: DEFAULT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.ml_price_forecasts_history ALTER COLUMN history_id SET DEFAULT nextval('haetdeul.ml_price_forecasts_history_history_id_seq'::regclass);


--
-- Name: persona_evidences persona_evidence_id; Type: DEFAULT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.persona_evidences ALTER COLUMN persona_evidence_id SET DEFAULT nextval('haetdeul.persona_evidences_persona_evidence_id_seq'::regclass);


--
-- Name: agent_policies agent_policies_pkey; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.agent_policies
    ADD CONSTRAINT agent_policies_pkey PRIMARY KEY (agent_type);


--
-- Name: agent_policy_config agent_policy_config_pkey; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.agent_policy_config
    ADD CONSTRAINT agent_policy_config_pkey PRIMARY KEY (policy_id);


--
-- Name: agent_runs agent_runs_pkey; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.agent_runs
    ADD CONSTRAINT agent_runs_pkey PRIMARY KEY (agent_run_id);


--
-- Name: company_personas company_personas_persona_version_key; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.company_personas
    ADD CONSTRAINT company_personas_persona_version_key UNIQUE (persona_version);


--
-- Name: company_personas company_personas_pkey; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.company_personas
    ADD CONSTRAINT company_personas_pkey PRIMARY KEY (persona_id);


--
-- Name: constraint_reviews constraint_reviews_pkey; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.constraint_reviews
    ADD CONSTRAINT constraint_reviews_pkey PRIMARY KEY (review_id);


--
-- Name: daily_closings daily_closings_pkey; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.daily_closings
    ADD CONSTRAINT daily_closings_pkey PRIMARY KEY (sim_run_id, close_date);


--
-- Name: deliveries deliveries_pkey; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.deliveries
    ADD CONSTRAINT deliveries_pkey PRIMARY KEY (delivery_id);


--
-- Name: evidences evidences_pkey; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.evidences
    ADD CONSTRAINT evidences_pkey PRIMARY KEY (evidence_id);


--
-- Name: evidences evidences_source_ref_key; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.evidences
    ADD CONSTRAINT evidences_source_ref_key UNIQUE (source_ref);


--
-- Name: expenses expenses_pkey; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.expenses
    ADD CONSTRAINT expenses_pkey PRIMARY KEY (expense_id);


--
-- Name: finance_states finance_states_pkey; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.finance_states
    ADD CONSTRAINT finance_states_pkey PRIMARY KEY (finance_state_id);


--
-- Name: finance_states uq_finance_states_axis_date; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.finance_states
    ADD CONSTRAINT uq_finance_states_axis_date UNIQUE (sim_run_id, financing_mode, state_date);


--
-- Name: forecasts forecasts_pkey; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.forecasts
    ADD CONSTRAINT forecasts_pkey PRIMARY KEY (forecast_id);


--
-- Name: inventory_lots inventory_lots_pkey; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.inventory_lots
    ADD CONSTRAINT inventory_lots_pkey PRIMARY KEY (lot_id);


--
-- Name: inventory_moves inventory_moves_pkey; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.inventory_moves
    ADD CONSTRAINT inventory_moves_pkey PRIMARY KEY (move_id);


--
-- Name: item_storage_policies item_storage_policies_pkey; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.item_storage_policies
    ADD CONSTRAINT item_storage_policies_pkey PRIMARY KEY (item_id);


--
-- Name: items items_item_code_key; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.items
    ADD CONSTRAINT items_item_code_key UNIQUE (item_code);


--
-- Name: items items_item_name_key; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.items
    ADD CONSTRAINT items_item_name_key UNIQUE (item_name);


--
-- Name: items items_pkey; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.items
    ADD CONSTRAINT items_pkey PRIMARY KEY (item_id);


--
-- Name: logistics_contracts logistics_contracts_pkey; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.logistics_contracts
    ADD CONSTRAINT logistics_contracts_pkey PRIMARY KEY (logistics_contract_id);


--
-- Name: logistics_runtime_fixture logistics_runtime_fixture_pkey; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.logistics_runtime_fixture
    ADD CONSTRAINT logistics_runtime_fixture_pkey PRIMARY KEY (fixture_id);


--
-- Name: market_quotes market_quotes_pkey; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.market_quotes
    ADD CONSTRAINT market_quotes_pkey PRIMARY KEY (quote_id);


--
-- Name: ml_price_forecasts_history ml_price_forecasts_history_pkey; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.ml_price_forecasts_history
    ADD CONSTRAINT ml_price_forecasts_history_pkey PRIMARY KEY (history_id);


--
-- Name: ml_price_forecasts ml_price_forecasts_pkey; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.ml_price_forecasts
    ADD CONSTRAINT ml_price_forecasts_pkey PRIMARY KEY (base_dt, item_nm, target_kind, offset_days);


--
-- Name: partner_item_demands partner_item_demands_pkey; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.partner_item_demands
    ADD CONSTRAINT partner_item_demands_pkey PRIMARY KEY (partner_id, item_id);


--
-- Name: partners partners_pkey; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.partners
    ADD CONSTRAINT partners_pkey PRIMARY KEY (partner_id);


--
-- Name: payables payables_pkey; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.payables
    ADD CONSTRAINT payables_pkey PRIMARY KEY (payable_id);


--
-- Name: payables payables_purchase_id_key; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.payables
    ADD CONSTRAINT payables_purchase_id_key UNIQUE (purchase_id);


--
-- Name: persona_evidences persona_evidences_company_persona_id_entity_type_entity_key_key; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.persona_evidences
    ADD CONSTRAINT persona_evidences_company_persona_id_entity_type_entity_key_key UNIQUE (company_persona_id, entity_type, entity_key, field_name, evidence_id);


--
-- Name: persona_evidences persona_evidences_pkey; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.persona_evidences
    ADD CONSTRAINT persona_evidences_pkey PRIMARY KEY (persona_evidence_id);


--
-- Name: proposals proposals_pkey; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.proposals
    ADD CONSTRAINT proposals_pkey PRIMARY KEY (proposal_id, scenario_id);


--
-- Name: purchase_items purchase_items_pkey; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.purchase_items
    ADD CONSTRAINT purchase_items_pkey PRIMARY KEY (purchase_item_id);


--
-- Name: purchases purchases_pkey; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.purchases
    ADD CONSTRAINT purchases_pkey PRIMARY KEY (purchase_id);


--
-- Name: receivables receivables_pkey; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.receivables
    ADD CONSTRAINT receivables_pkey PRIMARY KEY (receivable_id);


--
-- Name: receivables receivables_sale_id_key; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.receivables
    ADD CONSTRAINT receivables_sale_id_key UNIQUE (sale_id);


--
-- Name: sale_items sale_items_pkey; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.sale_items
    ADD CONSTRAINT sale_items_pkey PRIMARY KEY (sale_item_id);


--
-- Name: sales sales_pkey; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.sales
    ADD CONSTRAINT sales_pkey PRIMARY KEY (sale_id);


--
-- Name: sim_runs sim_runs_pkey; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.sim_runs
    ADD CONSTRAINT sim_runs_pkey PRIMARY KEY (sim_run_id);


--
-- Name: agent_policy_config uq_agent_policy_version; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.agent_policy_config
    ADD CONSTRAINT uq_agent_policy_version UNIQUE (policy_version, domain, policy_key);


--
-- Name: logistics_runtime_fixture uq_log_runtime_fixture; Type: CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.logistics_runtime_fixture
    ADD CONSTRAINT uq_log_runtime_fixture UNIQUE (sim_run_id, as_of, usage_scope);


--
-- Name: idx_ml_forecast_hist_key; Type: INDEX; Schema: haetdeul; Owner: -
--

CREATE INDEX idx_ml_forecast_hist_key ON haetdeul.ml_price_forecasts_history USING btree (base_dt, item_nm, target_kind, offset_days, replaced_at DESC);


--
-- Name: idx_ml_forecast_hist_replaced; Type: INDEX; Schema: haetdeul; Owner: -
--

CREATE INDEX idx_ml_forecast_hist_replaced ON haetdeul.ml_price_forecasts_history USING btree (replaced_at DESC);


--
-- Name: idx_ml_price_forecasts_kind_item_base; Type: INDEX; Schema: haetdeul; Owner: -
--

CREATE INDEX idx_ml_price_forecasts_kind_item_base ON haetdeul.ml_price_forecasts USING btree (target_kind, item_nm, base_dt DESC);


--
-- Name: ml_price_forecasts trg_ml_forecast_archive; Type: TRIGGER; Schema: haetdeul; Owner: -
--

CREATE TRIGGER trg_ml_forecast_archive BEFORE UPDATE ON haetdeul.ml_price_forecasts FOR EACH ROW EXECUTE FUNCTION haetdeul.f_ml_forecast_archive();


--
-- Name: agent_policies agent_policies_company_persona_id_fkey; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.agent_policies
    ADD CONSTRAINT agent_policies_company_persona_id_fkey FOREIGN KEY (company_persona_id) REFERENCES haetdeul.company_personas(persona_id);


--
-- Name: constraint_reviews constraint_reviews_proposal_id_scenario_id_fkey; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.constraint_reviews
    ADD CONSTRAINT constraint_reviews_proposal_id_scenario_id_fkey FOREIGN KEY (proposal_id, scenario_id) REFERENCES haetdeul.proposals(proposal_id, scenario_id);


--
-- Name: deliveries deliveries_customer_partner_id_fkey; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.deliveries
    ADD CONSTRAINT deliveries_customer_partner_id_fkey FOREIGN KEY (customer_partner_id) REFERENCES haetdeul.partners(partner_id);


--
-- Name: deliveries deliveries_sale_id_fkey; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.deliveries
    ADD CONSTRAINT deliveries_sale_id_fkey FOREIGN KEY (sale_id) REFERENCES haetdeul.sales(sale_id);


--
-- Name: expenses expenses_evidence_id_fkey; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.expenses
    ADD CONSTRAINT expenses_evidence_id_fkey FOREIGN KEY (evidence_id) REFERENCES haetdeul.evidences(evidence_id);


--
-- Name: expenses expenses_related_delivery_id_fkey; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.expenses
    ADD CONSTRAINT expenses_related_delivery_id_fkey FOREIGN KEY (related_delivery_id) REFERENCES haetdeul.deliveries(delivery_id);


--
-- Name: agent_runs fk_agent_runs_sim_run; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.agent_runs
    ADD CONSTRAINT fk_agent_runs_sim_run FOREIGN KEY (sim_run_id) REFERENCES haetdeul.sim_runs(sim_run_id);


--
-- Name: constraint_reviews fk_constraint_reviews_agent_run; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.constraint_reviews
    ADD CONSTRAINT fk_constraint_reviews_agent_run FOREIGN KEY (review_agent_run_id) REFERENCES haetdeul.agent_runs(agent_run_id);


--
-- Name: constraint_reviews fk_constraint_reviews_sim_run; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.constraint_reviews
    ADD CONSTRAINT fk_constraint_reviews_sim_run FOREIGN KEY (sim_run_id) REFERENCES haetdeul.sim_runs(sim_run_id);


--
-- Name: daily_closings fk_daily_closings_sim_run; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.daily_closings
    ADD CONSTRAINT fk_daily_closings_sim_run FOREIGN KEY (sim_run_id) REFERENCES haetdeul.sim_runs(sim_run_id);


--
-- Name: deliveries fk_deliveries_logistics_contract; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.deliveries
    ADD CONSTRAINT fk_deliveries_logistics_contract FOREIGN KEY (logistics_contract_id) REFERENCES haetdeul.logistics_contracts(logistics_contract_id);


--
-- Name: deliveries fk_deliveries_sim_run; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.deliveries
    ADD CONSTRAINT fk_deliveries_sim_run FOREIGN KEY (sim_run_id) REFERENCES haetdeul.sim_runs(sim_run_id);


--
-- Name: expenses fk_expenses_sim_run; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.expenses
    ADD CONSTRAINT fk_expenses_sim_run FOREIGN KEY (sim_run_id) REFERENCES haetdeul.sim_runs(sim_run_id);


--
-- Name: finance_states fk_finance_states_sim_run; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.finance_states
    ADD CONSTRAINT fk_finance_states_sim_run FOREIGN KEY (sim_run_id) REFERENCES haetdeul.sim_runs(sim_run_id);


--
-- Name: forecasts fk_forecasts_sim_run; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.forecasts
    ADD CONSTRAINT fk_forecasts_sim_run FOREIGN KEY (sim_run_id) REFERENCES haetdeul.sim_runs(sim_run_id);


--
-- Name: inventory_lots fk_inventory_lots_sim_run; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.inventory_lots
    ADD CONSTRAINT fk_inventory_lots_sim_run FOREIGN KEY (sim_run_id) REFERENCES haetdeul.sim_runs(sim_run_id);


--
-- Name: inventory_moves fk_inventory_moves_sim_run; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.inventory_moves
    ADD CONSTRAINT fk_inventory_moves_sim_run FOREIGN KEY (sim_run_id) REFERENCES haetdeul.sim_runs(sim_run_id);


--
-- Name: market_quotes fk_market_quotes_sim_run; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.market_quotes
    ADD CONSTRAINT fk_market_quotes_sim_run FOREIGN KEY (sim_run_id) REFERENCES haetdeul.sim_runs(sim_run_id);


--
-- Name: payables fk_payables_sim_run; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.payables
    ADD CONSTRAINT fk_payables_sim_run FOREIGN KEY (sim_run_id) REFERENCES haetdeul.sim_runs(sim_run_id);


--
-- Name: proposals fk_proposals_agent_run; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.proposals
    ADD CONSTRAINT fk_proposals_agent_run FOREIGN KEY (purchase_agent_run_id) REFERENCES haetdeul.agent_runs(agent_run_id);


--
-- Name: proposals fk_proposals_sim_run; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.proposals
    ADD CONSTRAINT fk_proposals_sim_run FOREIGN KEY (sim_run_id) REFERENCES haetdeul.sim_runs(sim_run_id);


--
-- Name: purchases fk_purchases_sim_run; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.purchases
    ADD CONSTRAINT fk_purchases_sim_run FOREIGN KEY (sim_run_id) REFERENCES haetdeul.sim_runs(sim_run_id);


--
-- Name: receivables fk_receivables_sim_run; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.receivables
    ADD CONSTRAINT fk_receivables_sim_run FOREIGN KEY (sim_run_id) REFERENCES haetdeul.sim_runs(sim_run_id);


--
-- Name: sales fk_sales_sim_run; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.sales
    ADD CONSTRAINT fk_sales_sim_run FOREIGN KEY (sim_run_id) REFERENCES haetdeul.sim_runs(sim_run_id);


--
-- Name: forecasts forecasts_evidence_id_fkey; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.forecasts
    ADD CONSTRAINT forecasts_evidence_id_fkey FOREIGN KEY (evidence_id) REFERENCES haetdeul.evidences(evidence_id);


--
-- Name: forecasts forecasts_item_id_fkey; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.forecasts
    ADD CONSTRAINT forecasts_item_id_fkey FOREIGN KEY (item_id) REFERENCES haetdeul.items(item_id);


--
-- Name: inventory_lots inventory_lots_item_id_fkey; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.inventory_lots
    ADD CONSTRAINT inventory_lots_item_id_fkey FOREIGN KEY (item_id) REFERENCES haetdeul.items(item_id);


--
-- Name: inventory_lots inventory_lots_purchase_item_id_fkey; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.inventory_lots
    ADD CONSTRAINT inventory_lots_purchase_item_id_fkey FOREIGN KEY (purchase_item_id) REFERENCES haetdeul.purchase_items(purchase_item_id);


--
-- Name: inventory_moves inventory_moves_lot_id_fkey; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.inventory_moves
    ADD CONSTRAINT inventory_moves_lot_id_fkey FOREIGN KEY (lot_id) REFERENCES haetdeul.inventory_lots(lot_id);


--
-- Name: inventory_moves inventory_moves_sale_item_id_fkey; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.inventory_moves
    ADD CONSTRAINT inventory_moves_sale_item_id_fkey FOREIGN KEY (sale_item_id) REFERENCES haetdeul.sale_items(sale_item_id);


--
-- Name: item_storage_policies item_storage_policies_item_id_fkey; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.item_storage_policies
    ADD CONSTRAINT item_storage_policies_item_id_fkey FOREIGN KEY (item_id) REFERENCES haetdeul.items(item_id);


--
-- Name: logistics_contracts logistics_contracts_company_persona_id_fkey; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.logistics_contracts
    ADD CONSTRAINT logistics_contracts_company_persona_id_fkey FOREIGN KEY (company_persona_id) REFERENCES haetdeul.company_personas(persona_id);


--
-- Name: logistics_contracts logistics_contracts_provider_partner_id_fkey; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.logistics_contracts
    ADD CONSTRAINT logistics_contracts_provider_partner_id_fkey FOREIGN KEY (provider_partner_id) REFERENCES haetdeul.partners(partner_id);


--
-- Name: market_quotes market_quotes_evidence_id_fkey; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.market_quotes
    ADD CONSTRAINT market_quotes_evidence_id_fkey FOREIGN KEY (evidence_id) REFERENCES haetdeul.evidences(evidence_id);


--
-- Name: market_quotes market_quotes_item_id_fkey; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.market_quotes
    ADD CONSTRAINT market_quotes_item_id_fkey FOREIGN KEY (item_id) REFERENCES haetdeul.items(item_id);


--
-- Name: partner_item_demands partner_item_demands_item_id_fkey; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.partner_item_demands
    ADD CONSTRAINT partner_item_demands_item_id_fkey FOREIGN KEY (item_id) REFERENCES haetdeul.items(item_id);


--
-- Name: partner_item_demands partner_item_demands_partner_id_fkey; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.partner_item_demands
    ADD CONSTRAINT partner_item_demands_partner_id_fkey FOREIGN KEY (partner_id) REFERENCES haetdeul.partners(partner_id);


--
-- Name: payables payables_purchase_id_fkey; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.payables
    ADD CONSTRAINT payables_purchase_id_fkey FOREIGN KEY (purchase_id) REFERENCES haetdeul.purchases(purchase_id);


--
-- Name: persona_evidences persona_evidences_company_persona_id_fkey; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.persona_evidences
    ADD CONSTRAINT persona_evidences_company_persona_id_fkey FOREIGN KEY (company_persona_id) REFERENCES haetdeul.company_personas(persona_id);


--
-- Name: persona_evidences persona_evidences_evidence_id_fkey; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.persona_evidences
    ADD CONSTRAINT persona_evidences_evidence_id_fkey FOREIGN KEY (evidence_id) REFERENCES haetdeul.evidences(evidence_id);


--
-- Name: proposals proposals_item_id_fkey; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.proposals
    ADD CONSTRAINT proposals_item_id_fkey FOREIGN KEY (item_id) REFERENCES haetdeul.items(item_id);


--
-- Name: purchase_items purchase_items_item_id_fkey; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.purchase_items
    ADD CONSTRAINT purchase_items_item_id_fkey FOREIGN KEY (item_id) REFERENCES haetdeul.items(item_id);


--
-- Name: purchase_items purchase_items_purchase_id_fkey; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.purchase_items
    ADD CONSTRAINT purchase_items_purchase_id_fkey FOREIGN KEY (purchase_id) REFERENCES haetdeul.purchases(purchase_id) ON DELETE CASCADE;


--
-- Name: purchase_items purchase_items_source_quote_id_fkey; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.purchase_items
    ADD CONSTRAINT purchase_items_source_quote_id_fkey FOREIGN KEY (source_quote_id) REFERENCES haetdeul.market_quotes(quote_id);


--
-- Name: purchases purchases_evidence_id_fkey; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.purchases
    ADD CONSTRAINT purchases_evidence_id_fkey FOREIGN KEY (evidence_id) REFERENCES haetdeul.evidences(evidence_id);


--
-- Name: purchases purchases_supplier_partner_id_fkey; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.purchases
    ADD CONSTRAINT purchases_supplier_partner_id_fkey FOREIGN KEY (supplier_partner_id) REFERENCES haetdeul.partners(partner_id);


--
-- Name: receivables receivables_sale_id_fkey; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.receivables
    ADD CONSTRAINT receivables_sale_id_fkey FOREIGN KEY (sale_id) REFERENCES haetdeul.sales(sale_id);


--
-- Name: sale_items sale_items_item_id_fkey; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.sale_items
    ADD CONSTRAINT sale_items_item_id_fkey FOREIGN KEY (item_id) REFERENCES haetdeul.items(item_id);


--
-- Name: sale_items sale_items_sale_id_fkey; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.sale_items
    ADD CONSTRAINT sale_items_sale_id_fkey FOREIGN KEY (sale_id) REFERENCES haetdeul.sales(sale_id) ON DELETE CASCADE;


--
-- Name: sales sales_customer_partner_id_fkey; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.sales
    ADD CONSTRAINT sales_customer_partner_id_fkey FOREIGN KEY (customer_partner_id) REFERENCES haetdeul.partners(partner_id);


--
-- Name: sim_runs sim_runs_company_persona_id_fkey; Type: FK CONSTRAINT; Schema: haetdeul; Owner: -
--

ALTER TABLE ONLY haetdeul.sim_runs
    ADD CONSTRAINT sim_runs_company_persona_id_fkey FOREIGN KEY (company_persona_id) REFERENCES haetdeul.company_personas(persona_id);


--
-- PostgreSQL database dump complete
--

\unrestrict Dp8ylbnM0ZMqDiePndhCz0JGMhE5efg3O7zdJq3b0elL1tFA7Z8szCyzeMyeSmR

