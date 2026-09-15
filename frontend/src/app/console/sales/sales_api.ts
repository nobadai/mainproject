/**
 * 판매 화면이 직접 부르는 read/write 계약.
 *
 * ★ **왜 `lib/console_api.ts` 가 아니라 여기인가.** 저 파일은 여러 도메인이 함께 쓰는
 *   공용 클라이언트이고 이번 판의 수정 범위 밖이다. 판매 화면만 쓰는 계약은 판매
 *   화면 옆에 둔다 — 공용 파일을 건드리지 않고도 판매가 자기 화면을 완성할 수 있다.
 *
 * 🔴 **`sim_run_id` 에 기본값을 두지 않는다.** 실행 축이 없으면 부르지 않는다.
 *
 * 🔴 **숫자를 여기서 만들지 않는다.** 합계·마진·연체는 백엔드가 낸 값을 그대로 나른다.
 */

const CONSOLE_BASE = process.env.NEXT_PUBLIC_CONSOLE_BASE ?? "/api/console";
const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "/api";
const TIMEOUT_MS = 20_000;

/** 금액·수량은 정밀도를 지키려고 문자열로 올 수 있다. `lib/console_api.ts` 와 같은 약속이다. */
export type Money = string | number;

export class SalesApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function send<T>(url: string, init?: RequestInit): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    let res: Response;
    try {
      res = await fetch(url, {
        ...init,
        headers: { Accept: "application/json", ...(init?.headers ?? {}) },
        signal: controller.signal,
      });
    } catch {
      //  ⚠️ 끊은 것과 못 닿은 것은 다른 사고다. 뭉치면 엉뚱한 조치를 하게 된다.
      throw controller.signal.aborted
        ? new SalesApiError(0, `${TIMEOUT_MS / 1000}초 안에 응답이 오지 않아 끊었습니다.`)
        : new SalesApiError(0, "서버에 닿지 못했습니다 — 잠시 후 다시 시도해 주세요.");
    }
    const body = await res.text();
    if (!res.ok) {
      let detail = body;
      try {
        const parsed = JSON.parse(body) as { detail?: unknown };
        //  FastAPI 의 `detail` 은 문자열일 수도 검증 오류 배열일 수도 있다.
        if (typeof parsed.detail === "string") detail = parsed.detail;
        else if (Array.isArray(parsed.detail)) {
          detail = parsed.detail
            .map((item) => {
              const entry = item as { loc?: unknown[]; msg?: string };
              const field = Array.isArray(entry.loc) ? entry.loc.slice(1).join(".") : "";
              return field ? `${field}: ${entry.msg ?? ""}` : (entry.msg ?? "");
            })
            .join(" / ");
        }
      } catch {
        /* JSON 이 아니면 원문 그대로 — 원인을 숨기지 않는다 */
      }
      throw new SalesApiError(res.status, detail || `요청이 실패했습니다 (${res.status})`);
    }
    return JSON.parse(body) as T;
  } finally {
    clearTimeout(timer);
  }
}

function query(params: Record<string, string | number | undefined | null>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") search.set(key, String(value));
  }
  return search.toString();
}

/* ── 판매 현황 ─────────────────────────────────────────────────────────── */

export interface SalesSummaryResponse {
  meta: { sim_run_id: string; as_of: string; data_type: string | null };
  summary: {
    sales_count: number;
    customer_count: number;
    total_sales_quantity_kg: Money;
    total_sales_amount_krw: Money;
    contribution_profit_krw: Money;
    contribution_margin_pct: Money;
    received_amount_krw: Money;
    outstanding_receivables_krw: Money;
  };
  items: {
    item_id: string;
    item_name: string | null;
    line_count: number;
    total_quantity_kg: Money;
    sales_amount_krw: Money;
    contribution_profit_krw: Money;
    avg_unit_price_krw_per_kg: Money;
  }[];
}

export interface SalesTrendResponse {
  sim_run_id: string;
  as_of: string;
  rows: {
    sale_date: string;
    sales_count: number;
    quantity_kg: Money;
    sales_amount_krw: Money;
    contribution_profit_krw: Money;
  }[];
}

export const salesOverview = {
  summary: (simRun: string, asOf: string) =>
    send<SalesSummaryResponse>(
      `${CONSOLE_BASE}/sales/summary?${query({ sim_run_id: simRun, as_of: asOf })}`,
    ),
  trend: (simRun: string, asOf: string) =>
    send<SalesTrendResponse>(
      `${CONSOLE_BASE}/sales/trend?${query({ sim_run_id: simRun, as_of: asOf })}`,
    ),
  proposals: (simRun: string, asOf: string) =>
    send<SalesProposalsResponse>(
      `${CONSOLE_BASE}/sales/proposals?${query({ sim_run_id: simRun, as_of: asOf })}`,
    ),
};

/* ── 거래처 등록 ───────────────────────────────────────────────────────── */

export interface PartnerCreateInput {
  partner_id: string;
  partner_name: string;
  partner_type: string;
  client_type?: string | null;
  factory_region?: string | null;
  factory_city?: string | null;
  factory_area?: string | null;
  sales_collection_days?: number | null;
  pricing_contract_type?: string | null;
  active?: boolean;
  note?: string | null;
}

export interface PartnerProfileRow {
  partner_id: string;
  partner_name: string;
  partner_type: string;
  client_type: string | null;
  factory_region: string | null;
  factory_city: string | null;
  factory_area: string | null;
  sales_collection_days: number | null;
  pricing_contract_type: string | null;
  active: boolean;
  provisional: boolean;
  note: string | null;
  credit_source: string;
}

/** 표가 실제로 허용하는 유형. 🔴 여기 없는 값은 DB CHECK 가 거절한다. */
export const PARTNER_TYPES: { value: string; label: string }[] = [
  { value: "CUSTOMER", label: "고객사 (판매처)" },
  { value: "SUPPLIER", label: "공급처 (매입처)" },
  { value: "LOGISTICS_PROVIDER", label: "물류사" },
  { value: "MARKET_REFERENCE", label: "시세 참조처" },
  { value: "OTHER", label: "기타" },
];

export function createPartner(input: PartnerCreateInput): Promise<PartnerProfileRow> {
  return send<PartnerProfileRow>(`${API_BASE}/sales/partners`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

/* ── 사용자가 만드는 판매 후보 ─────────────────────────────────────────── */

export interface SalesCandidateRequest {
  as_of: string;
  item: string;
  partner_id?: string;
  quantity_kg?: string;
  unit_price_krw?: string;
  delivery_date?: string;
  payment_days?: string;
  allow_additional_sourcing: boolean;
  note?: string;
}

export interface SalesCandidateReply {
  status: string;
  user_message?: string;
  missing_data?: string[];
  missing_capabilities?: string[];
  scenarios?: { scenario_id: string; quantity_kg?: Money | null; unit_price_krw?: Money | null }[];
}

/** 후보만 생성한다. 실제 판매 확정은 기존 Master 승인 흐름만 사용한다. */
export function createSalesCandidates(input: SalesCandidateRequest): Promise<SalesCandidateReply> {
  const quantity = input.quantity_kg?.trim();
  const price = input.unit_price_krw?.trim();
  const paymentDays = input.payment_days?.trim();
  return send<SalesCandidateReply>(`${API_BASE}/sales/proposal`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      business_mode: "SPOT_SALES",
      user_request: {
        raw_text: input.note?.trim() || null,
        item: input.item.trim(),
        partner_id: input.partner_id?.trim() || null,
        requested_quantity_kg: quantity || null,
        preferred_unit_price_krw: price || null,
        preferred_delivery_date: input.delivery_date || null,
        preferred_payment_days: paymentDays || null,
        allow_additional_sourcing: input.allow_additional_sourcing,
      },
      execution_identity: { as_of: input.as_of },
    }),
  });
}

/* ── 거래처 상세의 품목 이름 ────────────────────────────────────────────── */

/**
 * 백엔드가 내려주는 **품목 이름** 칸.
 *
 * ★ **왜 여기서 다시 적는가.** 공용 `lib/console_api.ts` 의 `PartnerDetail` 은 이번 판의
 *   수정 범위 밖이고, 그 타입에는 `item_name` 이 아직 없다. 판매 화면만 쓰는 칸이라
 *   판매 쪽에서 넓혀 읽는다 — 공용 파일을 건드리지 않고도 이름을 쓸 수 있다.
 *
 * 🔴 **`null` 을 허용한다.** `items` 에 없는 품목은 이름이 없고, 그때는 화면이 코드를
 *    쓴다. 여기서 이름을 지어내면 새 품목이 남의 이름으로 팔린다.
 */
export interface NamedItem {
  item_name?: string | null;
}

/**
 * 품목 이름 칸까지 포함해 읽은 거래처 상세.
 *
 * ⚠️ **값 자체는 공용 계약 그대로다.** 넓히는 것은 두 목록의 원소 타입뿐이고, 나머지
 *   칸은 `lib/console_api.ts` 의 `PartnerDetail` 이 정본이다.
 */
export type WithItemNames<T> = Omit<T, "recent_sales" | "item_summary"> & {
  recent_sales: (T extends { recent_sales: (infer R)[] } ? R & NamedItem : never)[];
  item_summary: (T extends { item_summary: (infer I)[] } ? I & NamedItem : never)[];
};

/* ── 금일 판매안 ───────────────────────────────────────────────────────── */

export interface SalesProposal {
  request_id: string;
  /** 화면이 본 마스터 판매 실행. UI에 보이지 않으며 승인 때만 쓴다. */
  history_run_id: string | null;
  scenario_id: string;
  scenario_type: string | null;
  objective: string | null;
  item: string | null;
  partner_id: string | null;
  quantity_kg: Money | null;
  unit_price_krw: Money | null;
  /** 🔴 판매가 적어 보낸 매출액이다. 화면이 수량×단가로 다시 만들지 않는다. */
  reported_sales_amount_krw: Money | null;
  payment_days: number | null;
  delivery_date: string | null;
  status: string | null;
  rationale: string[];
  risks: string[];
  uncertainties: string[];
  /** 재무가 남긴 판정. `null` 은 아직 안 본 것이지 통과도 거절도 아니다. */
  finance_verdict: string | null;
  finance_status: string | null;
  /** 🔴 판정을 **가른** 규칙의 사유다. 통과 사유는 들어 있지 않다. */
  finance_reason_codes: string[];
  contribution_margin_krw: Money | null;
  contribution_margin_rate: Money | null;
  /** 🔴 여신 칸은 재무가 센 값이다. 화면이 한도에서 미수를 빼지 않는다. */
  current_partner_ar_krw: Money | null;
  available_credit_krw: Money | null;
  projected_partner_ar_krw: Money | null;
  credit_limit_krw: Money | null;
  /** 판매 전에 먼저 받아야 하는 미수금. `0` 은 필요 없음, `null` 은 모름이다. */
  required_collection_before_sale_krw: Money | null;
  credit_utilization_rate: Money | null;
  /** 계약상 결제 예정일 기준의 예상 회복일. 입금 보장일이 아니다. */
  expected_credit_recovery_date: string | null;
  /** 판매가 «아직 못 받았다» 고 적어 둔 검증. 판정이 없는 이유가 여기 있다. */
  missing_capabilities: string[];
  evidence_refs: string[];
  source_ref: string | null;
  cost_basis_amount_krw: Money | null;
  cost_basis_quantity_kg: Money | null;
  cost_basis_method: string | null;
  cost_basis_refs: string[];
  confirmed_quantity_kg: Money | null;
  conditional_quantity_kg: Money | null;
  additional_supply_required: boolean | null;
  ml_support_used: boolean | null;
  recommended: boolean;
  /** 추천한 안에 판매가 저장해 둔 이유. 없으면 `null` 이다 — 화면이 지어내지 않는다. */
  recommendation_reason: string | null;
  /** 실제 판매로 확정됐으면 주문 상태. `null` 은 «확정 안 됨» 이다 (추천·선택과 다르다). */
  sale_status: string | null;
}

export interface SalesProposalsResponse {
  sim_run_id: string;
  as_of: string;
  request_count: number;
  /** 팔 물량이 0이라 목록에서 뺀 안의 수. 지운 것이 아니라 센 것이다. */
  hidden_zero_quantity: number;
  rows: SalesProposal[];
}

export interface SalesDecisionResponse {
  revalidation_outcome: "PASSED" | "CONDITIONAL" | "FAILED" | "ERROR" | null;
  sale: { status: "CONFIRMED" | "BLOCKED"; reason: string | null } | null;
}

/**
 * 판매 전용 write endpoint를 만들지 않는다. 사용자의 결정은 기존 Master 결정 계약에
 * 기록되고, 서버가 선택한 한 안만 재검증한 뒤 판매 확정을 판단한다.
 */
export function approveSalesScenario(input: {
  requestId: string;
  scenarioId: string;
  historyRunId: string;
  decidedBy: string;
}): Promise<SalesDecisionResponse> {
  return send<SalesDecisionResponse>(`${API_BASE}/master/runs/${encodeURIComponent(input.requestId)}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      decision: "APPROVE",
      scenario_label: input.scenarioId,
      decided_by: input.decidedBy,
      history_run_id: input.historyRunId,
    }),
  });
}
