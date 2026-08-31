/**
 * 백엔드 `app/master` 스키마의 거울.
 *
 * ★ **여기서 값을 만들지 않는다.** 화면은 받은 것을 그리기만 하고, 수량·금액·결론은
 *   전부 서버가 정한다. 프론트가 기본값을 채우기 시작하면 서버가 "모른다" 고 답한 것이
 *   화면에서 0 이 되고, 그건 §1.2-10(0 과 모름은 다르다)을 화면이 어기는 것이다.
 */

export type IntentAction =
  | "PROCUREMENT_RUN"
  | "STATUS_QUERY"
  | "RERUN_WITH_CONDITION"
  | "SELECT_SCENARIO"
  | "UNKNOWN";

export type AgentName = "finance" | "inventory" | "purchase";

/** LLM 이 돌려주는 것. **수량·금액 칸이 없는 것이 안전장치의 전부다.** */
export interface Intent {
  action: IntentAction;
  agents: AgentName[];
  item: string | null;
  scenario_label: string | null;
  condition: string | null;
  confidence: "HIGH" | "MEDIUM" | "LOW";
}

export type AskOutcome =
  | "CLASSIFIED_ONLY"
  | "STATUS_ANSWERED"
  | "DECISION_RECORDED"
  | "NEEDS_CLARIFICATION";

export interface AnswerOut {
  text: string;
  narrative: string | null;
  llm_status: string;
  llm_attempts: number;
  llm_fallback_used: boolean;
}

export interface DecisionOut {
  decision_id: string;
  request_id: string;
  decision_seq: number;
  decision: "APPROVE" | "REJECT_ALL" | "REQUEST_CHANGE";
  scenario_label: string | null;
  condition_text: string | null;
  decided_by: string;
  follow_up_request_id: string | null;
  end_code_at_decision: string;
  /** 이 결정이 가리키는 실행. `null` 은 **"어느 실행인지 기록되지 않았다"** 이다. */
  history_run_id: string | null;
  created_at: string;
  is_current: boolean;
}

export interface AskResponse {
  request_id: string;
  as_of: string;
  outcome: AskOutcome;
  intent: Intent;
  clarification: string | null;
  confirm_required: boolean;
  status: unknown;
  decision: DecisionOut | null;
  /** 조건부 재요청으로 **다시 돈 실행.** 없으면 고리가 끊긴다. */
  run: ProcurementRunResponse | null;
  answer: AnswerOut | null;
  llm_status: string;
  /** 어느 API 를 탔나 — `ollama`(로컬) 인지 `gemini`(외부) 인지 화면이 구분해 적는다. */
  llm_provider: string | null;
  llm_model: string | null;
  llm_attempts: number;
  llm_fallback_used: boolean;
  note: string | null;
}

export interface Scenario {
  label?: string;
  total_qty_kg?: number;
  total_amount_krw?: number;
  coverage_days?: number;
  split_plan?: { qty_kg?: number }[];
  //: 승인 결과 화면이 편다. **모양은 매입이 정한다** — 여기서 좁게 잡으면
  //: 매입이 칸을 늘릴 때 화면이 조용히 못 읽는다. 인덱스 시그니처로 받고
  //: 쓰는 쪽에서 좁힌다 (`ApprovedPlan.tsx`).
  sourcing_plan?: unknown[];
  payment_schedule?: unknown[];
  [key: string]: unknown;
}

export type EndCode =
  | "E1_APPROVED"
  | "E2_HELD"
  | "E3_REJECTED"
  | "E4_NOT_STARTED"
  | "E5_NO_FEASIBLE_PLAN";

export interface ProcurementRunResponse {
  request_id: string;
  as_of: string;
  /**
   * 🔴 **이 실행이 이력에 남은 행의 id.**
   *
   * `plan[].run_id` 와 **다른 것이다** — 저쪽은 그 *부서 호출* 의 id 이고 이것은
   * *마스터 실행 한 번* 의 id 다.
   *
   * 승인·재요청 때 이 값을 **그대로 되돌려 준다.** 그래야 "내가 본 그것을
   * 승인했다" 가 기록된다. 안 보내면 서버가 최신 실행을 고르는데, 그 사이
   * 재실행이 있었으면 **본 것과 다른 안이 승인된 것으로 남는다** (라벨이 같아
   * 눈에 안 띈다).
   *
   * 적재 실패 시 `null` — 이력이 없어도 계산 결과는 온다.
   */
  history_run_id: string | null;
  end_code: EndCode;
  /** 마스터가 내린 결론의 사유. **안이 없을 때는 이것이 답 자체다.** */
  reason: string;
  /**
   * 매입 자신의 판정. `verdicts`(조언자 판정)와 다르다.
   *
   * 🔴 **안이 0개일 때 여기에 진짜 이유가 있다.** 마스터의 `reason` 은
   * *"유효한 안이 없다"* 까지만 말하고, **왜 없는지**는 매입이 안다 —
   * `no_proposal_reason` · `rejected_reasons`.
   */
  judgment: {
    /**
     * 닫힌 집합 — `stable` · `uncertain`. 매입 스키마의 `Literal` 이라
     * **새 값이 생기면 스키마가 먼저 바뀐다** (매입 2026-08-31 회신 ④).
     * 한국어 표기는 `lib/vocab.ts` 한 곳에 있고, 모르는 값은 원문 그대로 보인다.
     */
    situation?: string;
    /** 닫힌 집합 — `high` · `medium` · `low`. `Intent.confidence` 와 **다른 값이다.** */
    confidence?: string;
    /** 닫힌 집합 — `quantity` · `timing` · `mix`. 매입이 **연** 축이다. */
    allowed_axes?: string[];
    no_proposal_reason?: string | null;
    rejected_reasons?: { label?: string; reason?: string }[];
  };
  scenarios: Scenario[];
  /**
   * 조언자(재무·물류)가 시나리오를 보고 낸 판정. `judgment`(매입 자신의 판정)와 다르다.
   *
   * 🔴 **"왜 조건부인지" 가 여기 말고는 없다.** 마스터는 판정 라벨까지만 알고 이유는
   * `payload` 안에 있는데, 그 모양은 **부서마다 다르다** — 그래서 서버가 해석하지
   * 않고 그대로 싣는다. 화면도 해석하지 않는다 (`AdvisorVerdicts`).
   *
   * ★ 실측 2026-08-31 — 물류 `verdict: "conditional"` 인데 시나리오 셋은 전부 `ok`
   *   였다. 조건부의 원인은 안이 아니라 `hard_constraints` 의 `LOG-H02 UNRESOLVED`
   *   (창고 구역 용량 정책 미비)였다. **"안에 문제가 있다" 와 "검사를 못 돌렸다" 가
   *   같은 한 단어에 뭉쳐 있다.**
   */
  verdicts: Record<
    string,
    {
      business_status: string;
      runtime_status: string;
      /** 부서가 보낸 것 그대로. 모양이 부서마다 다르다 — 타입을 좁히지 않는다. */
      payload?: Record<string, unknown>;
      /** 개수만 온다. 내용은 부서 payload 안에 있다. */
      suggested_adjustments?: number;
      needs_followup?: boolean;
      /** 판정을 못 냈을 때 **유일하게 이유를 담는 칸**. */
      reasoning?: string | null;
    }
  >;
  findings: string[];
  concerns: string[];
  skipped_checks: string[];
  verification_skipped: boolean;
  purchase_attempts: number;
  single_option: boolean;
  /** 사람이 읽는 리포트. **규칙만으로 만든다** (`answer.py`). */
  report_text: string;
  /** `등급:소스` — MEASURED / DERIVED / MOCK / MISSING */
  input_sources: Record<string, string>;
  /** 🔴 비어 있지 않으면 이 결론을 실측으로 읽으면 안 된다. */
  mocked_inputs: string[];
  plan: {
    seq: number;
    agent: AgentName;
    mode: string;
    runtime_status: string;
    business_status: string;
  }[];
}

/** `/ask/execute` 는 둘 중 하나를 돌려준다 — `end_code` 로 가른다. */
export type ExecuteResponse = AskResponse | ProcurementRunResponse;

export function isProcurement(r: ExecuteResponse): r is ProcurementRunResponse {
  return "end_code" in r;
}

export interface RunHistory {
  request_id: string;
  /** 돌려받은 이 행의 id — 결정이 **이 실행**을 가리키는지 대조하는 데 쓴다. */
  run_id: string | null;
  as_of: string;
  cycle: string;
  runtime_status: string;
  /** 이 계획을 만든 실행의 시각. **같은 업무 키에 실행이 여럿이라 필요하다.** */
  created_at: string;
  elapsed_ms: number | null;
  plan: Record<string, unknown>[];
  decisions: DecisionOut[];
}

/** 하루치 마감 한 줄. **번인 구간의 실제 값이고 에이전트가 만든 것이 아니다.** */
export interface DailyClosing {
  close_date: string;
  day_no: number;
  /**
   * 🔴 **무차입 기준 현금.** 재무가 답하는 `available_cash` 와 다르다 — 그쪽은
   * 대출 실행분이 더해진 값이다. 둘을 같은 줄에 두면 화면이 거짓말을 한다.
   */
  base_cash_balance_krw: number | null;
  loan_cash_balance_krw: number | null;
  receivables_balance_krw: number | null;
  inventory_qty_kg: number | null;
  sales_recognized_krw: number | null;
  collection_cash_in_krw: number | null;
  purchase_cash_out_krw: number | null;
  closed: boolean;
}

/** `GET /master/burn-in` — 에이전트가 판단하기 전에 회사가 어떻게 왔는가. */
export interface BurnIn {
  sim_run_id: string;
  run_type: string;
  period_start: string;
  period_end: string;
  /** 에이전트가 **처음 판단하는 날**. 번인의 마지막 날과 같다. */
  as_of: string;
  status: string;
  financing_mode: string | null;
  note: string | null;
  closings: DailyClosing[];
}


/** `GET /master/runs/{id}/report` — 들고 나갈 수 있는 매입안 문서. */
export interface RunReport {
  request_id: string;
  filename: string;
  /** Markdown 전문. **화면이 조립하지 않는다** — 서버가 낸 것을 그대로 내려받는다. */
  markdown: string;
}
