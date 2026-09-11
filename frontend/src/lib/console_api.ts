/**
 * 운영 콘솔 read API 클라이언트 (`/api/console/…`).
 *
 * ★ **`lib/screen.ts` 와 다른 계약이다.** 저쪽(`/api/screen/…`)은 화면용으로 미리
 *   문장·색·표를 만들어 주는 옛 계약이고, 이쪽은 **저장된 사실을 그대로** 준다.
 *   운영 콘솔은 이쪽만 쓴다 — 옛 API 는 지우지 않되 여기서 부르지 않는다.
 *
 * 🔴 **모든 호출이 `sim_run_id` 를 싣는다.** 실행 축이 없으면 부르지 않는다
 *    (`lib/run_context.ts`). 기본 실행으로 대신 답하는 길을 만들지 않는다.
 *
 * 🔴 **숫자를 여기서 만들지 않는다.** 합계·마진·연체는 백엔드가 낸 값을 그대로
 *    나른다. 화면이 다시 세면 두 곳이 서로 다른 답을 갖게 된다.
 */

const BASE = process.env.NEXT_PUBLIC_CONSOLE_BASE ?? "/api/console";

/** `lib/screen.ts` 와 같은 상한. 하는 일은 *"영영 안 끝나는 요청을 끝내는 것"* 이다. */
const TIMEOUT_MS = 20_000;

export class ConsoleError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

type Params = Record<string, string | number | undefined | null>;

async function get<T>(path: string, params: Params): Promise<T> {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") query.set(key, String(value));
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    let res: Response;
    try {
      res = await fetch(`${BASE}${path}?${query.toString()}`, {
        headers: { Accept: "application/json" },
        signal: controller.signal,
      });
    } catch {
      //  ⚠️ 끊은 것과 못 닿은 것은 다른 사고다. 뭉치면 엉뚱한 조치를 하게 된다.
      throw controller.signal.aborted
        ? new ConsoleError(0, `${TIMEOUT_MS / 1000}초 안에 응답이 오지 않아 끊었습니다.`)
        : new ConsoleError(0, "백엔드에 닿지 못했습니다 — 서버가 떠 있는지 확인해 주세요.");
    }
    const body = await res.text();
    if (!res.ok) {
      let detail = body;
      try {
        const parsed = JSON.parse(body) as { detail?: unknown };
        if (typeof parsed.detail === "string") detail = parsed.detail;
      } catch {
        /* JSON 이 아니면 원문 그대로 */
      }
      throw new ConsoleError(res.status, detail);
    }
    return JSON.parse(body) as T;
  } finally {
    clearTimeout(timer);
  }
}

/* ── 공통 ──────────────────────────────────────────────────────────────── */

/**
 * 돈과 수량. **백엔드는 `Decimal` 로 셈하고 전선에서는 문자열 또는 숫자로 온다.**
 * 화면은 그것을 산술에 쓰지 않고 **표기만** 한다 (`lib/vocab.ts` 의 규율과 같다).
 */
export type Money = string | number;

export interface RunScope {
  sim_run_id: string;
  as_of: string;
}

/* ── Finance ───────────────────────────────────────────────────────────── */

export interface PayableRow {
  payable_id: string;
  purchase_id: string | null;
  issued_date: string;
  due_date: string;
  original_amount_krw: Money;
  paid_amount_krw: Money;
  outstanding_amount_krw: Money;
  days_until_due: number;
  status: string;
}
export interface PayablesResponse extends RunScope {
  summary: {
    total_outstanding_krw: Money;
    due_today_krw: Money;
    due_next_7d_krw: Money;
    overdue_krw: Money;
  };
  rows: PayableRow[];
}

export interface ExpenseRow {
  expense_id: string;
  expense_date: string;
  raw_category: string;
  display_category: string;
  amount_krw: Money;
  source_ref: string | null;
  note: string | null;
}
export interface ExpensesResponse extends RunScope {
  summary: {
    total_expenses_krw: Money;
    category_totals: {
      raw_category: string;
      display_category: string;
      expense_count: number;
      total_amount_krw: Money;
    }[];
  };
  rows: ExpenseRow[];
}

export type AgingBucket = "CURRENT" | "1_7" | "8_30" | "30_PLUS" | "PAID";

export interface ReceivableRow {
  receivable_id: string;
  sale_id: string;
  partner_id: string | null;
  partner_name: string | null;
  original_amount_krw: Money;
  received_amount_krw: Money;
  outstanding_amount_krw: Money;
  due_date: string;
  days_overdue: number | null;
  aging_bucket: AgingBucket;
  status: string;
}
export interface ReceivablesResponse extends RunScope {
  summary: {
    current_krw: Money;
    days_1_7_krw: Money;
    days_8_30_krw: Money;
    days_30_plus_krw: Money;
    total_outstanding_krw: Money;
  };
  rows: ReceivableRow[];
}

export interface FinanceRun {
  run_id: string;
  request_id: string;
  sim_run_id: string;
  as_of: string;
  mode: string;
  runtime_status: string;
  business_status: string;
  verdict: string | null;
  llm_status: string;
  deterministic_result: Record<string, unknown> | null;
  evidence: string[] | null;
  interpretation: string | null;
  created_at: string;
}
export interface FinanceRunsResponse {
  sim_run_id: string;
  rows: FinanceRun[];
}

export interface FinanceStateView {
  finance_state_id: string;
  state_date: string;
  state_type: string;
  financing_mode: string;
  current_cash_krw: Money;
  minimum_operating_cash_krw: Money;
  operating_cash_buffer_krw: Money;
  receivables_krw: Money;
  inventory_book_value_krw: Money;
  current_debt_krw: Money;
  financial_limit_krw: Money;
}
export interface ClosingItem {
  close_date: string;
  day_no: number;
  purchase_cash_out_krw: Money;
  logistics_cash_out_krw: Money;
  payroll_interest_cash_out_krw: Money;
  sales_recognized_krw: Money;
  collection_cash_in_krw: Money;
  base_net_cash_krw: Money;
  base_cash_balance_krw: Money;
  loan_execution_krw: Money;
  loan_cash_balance_krw: Money;
  minimum_operating_cash_krw: Money | null;
  receivables_balance_krw: Money;
}
export interface FinanceSummaryResponse {
  meta: { sim_run_id: string; as_of: string; data_type: string | null };
  states: FinanceStateView[];
  cashflow_summary: {
    purchase_cash_out_krw: Money;
    logistics_cash_out_krw: Money;
    payroll_interest_cash_out_krw: Money;
    sales_recognized_krw: Money;
    collection_cash_in_krw: Money;
    base_net_cash_krw: Money;
    loan_execution_krw: Money;
  };
  recent_closings: ClosingItem[];
}
export interface FinanceCashflowResponse {
  meta: { sim_run_id: string; as_of: string; data_type: string | null };
  cashflow: ClosingItem[];
}

export const financeConsole = {
  summary: (sim_run_id: string, as_of: string) =>
    get<FinanceSummaryResponse>("/finance/summary", { sim_run_id, as_of }),
  cashflow: (sim_run_id: string, as_of: string, days = 30) =>
    get<FinanceCashflowResponse>("/finance/cashflow", { sim_run_id, as_of, days }),
  payables: (sim_run_id: string, as_of: string) =>
    get<PayablesResponse>("/finance/payables", { sim_run_id, as_of }),
  expenses: (sim_run_id: string, as_of: string) =>
    get<ExpensesResponse>("/finance/expenses", { sim_run_id, as_of }),
  receivables: (sim_run_id: string, as_of: string) =>
    get<ReceivablesResponse>("/finance/receivables", { sim_run_id, as_of }),
  runs: (sim_run_id: string, limit = 50) =>
    get<FinanceRunsResponse>("/finance/runs", { sim_run_id, limit }),
  latestRun: (sim_run_id: string) =>
    get<FinanceRun | null>("/finance/runs/latest", { sim_run_id }),
};

/* ── Sales ─────────────────────────────────────────────────────────────── */

export interface PartnerRow {
  partner_id: string;
  partner_name: string | null;
  partner_type: string | null;
  status: string;
  total_sales_krw: Money;
  total_sales_count: number;
  receivable_balance_krw: Money;
  overdue_balance_krw: Money;
  latest_sale_date: string | null;
}
export interface PartnersResponse extends RunScope {
  rows: PartnerRow[];
}

export interface PartnerDetail extends RunScope {
  basic: {
    partner_id: string;
    partner_name: string | null;
    partner_type: string | null;
    client_type: string | null;
    factory_region: string | null;
    sales_collection_days: number | null;
    pricing_contract_type: string | null;
    status: string;
  };
  summary: {
    total_sales_krw: Money;
    sales_count: number;
    contribution_profit_krw: Money;
    contribution_margin_rate: Money | null;
    receivable_balance_krw: Money;
    overdue_balance_krw: Money;
    latest_sale_date: string | null;
  };
  recent_sales: {
    sale_id: string;
    sale_date: string;
    item: string | null;
    quantity_kg: Money;
    unit_price_krw: Money | null;
    sales_amount_krw: Money;
    contribution_profit_krw: Money | null;
  }[];
  item_summary: {
    item: string;
    quantity_kg: Money;
    sales_amount_krw: Money;
    contribution_profit_krw: Money;
  }[];
  receivables: {
    receivable_id: string;
    sale_id: string;
    due_date: string;
    original_amount_krw: Money;
    received_amount_krw: Money;
    outstanding_amount_krw: Money;
    days_overdue: number | null;
    aging_bucket: AgingBucket;
    status: string;
  }[];
  /** 🔴 여신은 재무 정본이다. 판매가 `한도 − 채권` 으로 만들지 않는다. */
  credit: null;
  credit_status: string;
}

export interface CollectionRow {
  partner_id: string | null;
  partner_name: string | null;
  sale_id: string;
  receivable_id: string;
  original_amount_krw: Money;
  received_amount_krw: Money;
  outstanding_amount_krw: Money;
  due_date: string;
  days_overdue: number | null;
  aging_bucket: AgingBucket;
  status: string;
}
export interface CollectionsResponse extends RunScope {
  summary: { total_outstanding_krw: Money; overdue_krw: Money; collected_krw: Money };
  rows: CollectionRow[];
}

export interface SalesRun {
  run_id: string;
  request_id: string | null;
  sim_run_id: string;
  as_of: string;
  partner_id: string | null;
  partner_name: string | null;
  item: string | null;
  runtime_status: string;
  verdict: string | null;
  llm_status: string | null;
  master_end_code: string | null;
  created_at: string;
}
export interface SalesRunsResponse {
  sim_run_id: string;
  rows: SalesRun[];
}

export const salesConsole = {
  partners: (sim_run_id: string, as_of: string, query?: string) =>
    get<PartnersResponse>("/sales/partners", { sim_run_id, as_of, query }),
  partnerDetail: (sim_run_id: string, as_of: string, partner_id: string) =>
    get<PartnerDetail>(`/sales/partners/${encodeURIComponent(partner_id)}`, {
      sim_run_id,
      as_of,
    }),
  collections: (sim_run_id: string, as_of: string) =>
    get<CollectionsResponse>("/sales/collections", { sim_run_id, as_of }),
  runs: (sim_run_id: string, limit = 50) =>
    get<SalesRunsResponse>("/sales/runs", { sim_run_id, limit }),
};

/**
 * 표기 전용 포맷터. **여기서 계산하지 않는다** — 반올림도 통화 변환도 없다.
 *
 * ⚠️ `null` 은 `0` 이 아니다. 값이 없으면 «데이터 없음» 이고, 0 은 실제 0 이다.
 */
export function money(value: Money | null | undefined): string {
  if (value === null || value === undefined) return "데이터 없음";
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? `${parsed.toLocaleString("ko-KR")} 원` : String(value);
}

export function quantity(value: Money | null | undefined): string {
  if (value === null || value === undefined) return "데이터 없음";
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? `${parsed.toLocaleString("ko-KR")} kg` : String(value);
}

export function percent(value: Money | null | undefined): string {
  if (value === null || value === undefined) return "데이터 없음";
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? `${(parsed * 100).toFixed(1)}%` : String(value);
}

export const AGING_LABELS: Record<AgingBucket, string> = {
  CURRENT: "정상",
  "1_7": "1–7일 연체",
  "8_30": "8–30일 연체",
  "30_PLUS": "30일 초과",
  PAID: "수금 완료",
};

/* ── 실행 목록 ─────────────────────────────────────────────────────────── */

/**
 * 고를 수 있는 실행.
 *
 * 🔴 **정책 버전 칸이 없다.** `sim_runs` 가 그 값을 들고 있지 않다 — 이름만 내면
 *    받는 쪽이 «언젠가 올 값» 으로 읽고 자리를 비워 둔다.
 */
export interface ConsoleRun {
  sim_run_id: string;
  run_type: string | null;
  as_of: string | null;
  period_start: string | null;
  period_end: string | null;
  status: string | null;
  financing_mode: string | null;
  company_persona_id: string | null;
  started_at: string | null;
  finished_at: string | null;
  /** 마지막으로 기록이 쌓인 시각. 기록이 없으면 `null` — 0 이나 생성 시각이 아니다. */
  latest_activity_at: string | null;
  note: string | null;
}
export interface ConsoleRunsResponse {
  rows: ConsoleRun[];
}

export const consoleRuns = {
  list: (limit = 100) => get<ConsoleRunsResponse>("/runs", { limit }),
};
