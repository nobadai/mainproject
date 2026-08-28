/**
 * `POST /master/request` 의 입출력 계약.
 *
 * 백엔드 정본은 `backend/app/master/schemas.py` 다. 여기 타입이 그것보다 넓거나
 * 좁으면 화면이 조용히 틀린다 — 스키마가 바뀌면 이 파일을 같이 고친다.
 */

// ─────────────────────────────────────────────────────────────
// 요청
// ─────────────────────────────────────────────────────────────

/**
 * 마스터가 **직접 조회하지 않아** 호출자가 실어 보내야 하는 셋 (정의서 §3.2.5 예외).
 *
 * ML 은 호출 구조 밖에서 독립 실행되므로 "해당 에이전트에게 요청"이 성립하지 않고,
 * 확정주문·정책값도 마스터 관할 Rule 이라 요청 본문으로 온다. 화면이 이 값을 들고
 * 있는 것은 **임시방편이 아니라 계약**이다.
 */
export interface CallerInput {
  item: string;
  forecast: Record<string, unknown>;
  confirmed_orders: Record<string, unknown>;
  policy_values: Record<string, unknown>;
}

export interface ProcurementRunRequest extends Partial<CallerInput> {
  as_of: string;
  policy_version: string;
  trigger?: "USER_REQUEST" | "ML_COMPLETE" | "SCHEDULE";
  request_id?: string;
  has_unmet_obligation?: boolean;
  budget?: number;
}

// ─────────────────────────────────────────────────────────────
// 시나리오 (응답 `scenarios[]` 의 원소 · 매입 스키마)
// ─────────────────────────────────────────────────────────────

export interface SplitRound {
  seq: number;
  date: string;
  qty_kg: number;
}

export interface SourcingLine {
  market: string;
  grade: string;
  qty_kg: number;
  grade_unit_price: number;
}

export interface PaymentRow {
  seq: number;
  purchase_date: string;
  payment_date: string;
  qty_kg: number;
  amount_krw: number;
  /** 상한가 기준 최악값. 매입 상한과 **기준이 다르다** — 나란히 두고 비교하지 않는다. */
  amount_max_krw: number;
  basis: string;
}

export type EvidenceGrade = "SIM_FIXED" | "ASSUMED" | "OFFICIAL" | (string & {});

export interface RationaleItem {
  source: string;
  claim: string;
  ref_id: string;
  evidence_grade: EvidenceGrade;
  evidence_detail: string;
}

export interface Scenario {
  label: string;
  strategy_type: "quantity" | "timing" | "mix";
  coverage_days: number;
  total_qty_kg: number;
  total_amount_krw: number;
  max_price: number;
  margin_warning: boolean;
  split_plan: SplitRound[];
  sourcing_plan: SourcingLine[];
  /**
   * ★ **분할 안에만 있다.** 일괄 안에는 키 자체가 없다 — 설계다.
   *   빈 배열로 채우면 "있는데 비었다"가 되어 누락과 구분되지 않는다.
   */
  payment_schedule?: PaymentRow[];
  expected_margin_rate: number;
  rationale: RationaleItem[];
  risks: string[];
}

// ─────────────────────────────────────────────────────────────
// 응답
// ─────────────────────────────────────────────────────────────

export type EndCode =
  | "E1_APPROVED"
  | "E2_HELD"
  | "E3_REJECTED"
  | "E4_NOT_STARTED"
  | "E5_NO_FEASIBLE_PLAN";

export interface PlanStep {
  seq: number;
  agent: string;
  mode: string;
  call_seq: number;
  run_id: string;
  runtime_status: string;
  business_status: string;
  used_tools: string[];
  finding_codes: string[];
  missing_data: string[];
}

/** 매입 제안의 판정부 — `scenarios` 를 뺀 제안 최상위 전부 (PR #75 · `flow.py:_judgment_of`). */
export interface JudgmentPayload {
  situation?: "stable" | "uncertain";
  allowed_axes?: string[];
  confidence?: string;
  context_docs_used?: string[];
  meta?: Record<string, unknown>;
  rejected_reasons?: unknown[];
  /** 안이 하나도 없을 때 그 사유가 여기 실린다. */
  no_proposal_reason?: string;
}

export interface MasterResponse {
  request_id: string;
  as_of: string;
  end_code: EndCode;
  reason: string;
  scenarios: Scenario[];
  /**
   * 매입 자신의 판정 (PR #75 로 응답에 실린다). `verdicts`(조언자·검증 판정)와 다르다.
   *
   * ⚠️ **게이트 임계값은 여기 없다.** CI·VOL·MIX 의 비교 문면은 매입 회신의
   * `evidences` 에 있는데 마스터 응답이 그것까지는 싣지 않는다. 화면은 없는 것을
   * 지어내지 않는다 — 클라이언트가 임계를 다시 계산하면 에이전트 판정을 흉내 낸 값이 된다.
   */
  judgment: JudgmentPayload;
  constraints: Record<string, Record<string, unknown>>;
  verdicts: Record<string, Record<string, unknown>>;
  blocked_by: string[];
  findings: string[];
  concerns: string[];
  skipped_checks: string[];
  verification_skipped: boolean;
  purchase_attempts: number;
  presentable: boolean;
  single_option: boolean;
  plan: PlanStep[];
  plan_signature: [string, string, number][];
  missing_adapters: string[];
}

// ─────────────────────────────────────────────────────────────
// 내장 표본 (API 실패 시)
// ─────────────────────────────────────────────────────────────

export interface Evidence {
  claim: string;
  source: string;
  ref_ids: string[];
  value: number;
  unit: string;
  evidence_grade: EvidenceGrade;
  evidence_detail: string;
}

export interface FallbackProposal {
  situation: "stable" | "uncertain";
  allowed_axes: string[];
  confidence: string;
  context_docs_used: string[];
  meta: { as_of: string; item: string; agent_version: string; [k: string]: unknown };
  scenarios: Scenario[];
  reasoning: string;
  evidences: Evidence[];
  run_id: string;
  used_tools: string[];
  missing_data: string[];
}

// ─────────────────────────────────────────────────────────────
// 화면이 실제로 그리는 모양
// ─────────────────────────────────────────────────────────────

export interface Judgment {
  situation: "stable" | "uncertain";
  allowedAxes: string[];
  confidence: string;
  contextDocs: string[];
}

export interface Gate {
  name: string;
  value: string;
  say: string;
  ref: string;
  blocked: boolean;
  chip: string;
}

export interface Direction {
  current: number;
  predicted: number;
  change: number;
  say: string;
  ref: string;
}

/** 화면 하나가 필요로 하는 전부. API 경로와 표본 경로가 **같은 모양**으로 수렴한다. */
export interface ViewModel {
  source: "api" | "fallback";
  /** 표본으로 내려온 이유. `source === "fallback"` 일 때만 채운다. */
  fallbackReason?: string;
  asOf: string;
  item: string;
  scenarios: Scenario[];
  financeCap: number;
  /** API 응답에는 아직 없다 — 없으면 화면이 "미노출"로 표시한다. */
  judgment: Judgment | null;
  gates: Gate[] | null;
  direction: Direction | null;
  reasoning: string;
  runId: string;
  usedTools: string[];
  endCode: EndCode | null;
  reasonText: string;
  /** 재무·물류가 실제로 돌려준 경계. API 경로는 응답에서, 표본 경로는 고정값에서 온다. */
  boundary: { finance: Record<string, unknown>; inventory: Record<string, unknown> };
  evidenceCount: number | null;
  /** 게이트를 판정 근거에서 만들었는가. API 경로는 `evidences` 가 없어 false 다. */
  gatesFromEvidence: boolean;
  missingData: string[];
  concerns: string[];
  skippedChecks: string[];
}
