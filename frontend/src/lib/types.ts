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
  llm_model: string | null;
  llm_attempts: number;
  llm_fallback_used: boolean;
  note: string | null;
}

export interface Scenario {
  label?: string;
  total_qty_kg?: number;
  total_amount_krw?: number;
  split_plan?: { qty_kg?: number }[];
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
  reason: string;
  scenarios: Scenario[];
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
