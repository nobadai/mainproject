/**
 * 재무 화면이 부르는 **거래처 여신 현황** 계약.
 *
 * ★ `lib/console_api.ts` 는 여러 도메인이 함께 쓰는 공용 클라이언트라 이 판에서 고치지
 *   않는다. 재무 화면만 쓰는 계약은 재무 화면 옆에 둔다 (`sales/sales_api.ts` 와 같은 자리).
 *
 * 🔴 **숫자를 여기서 만들지 않는다.** 가용 여신·사용률·수금 뒤 여신은 백엔드가 센 값이다.
 */

const CONSOLE_BASE = process.env.NEXT_PUBLIC_CONSOLE_BASE ?? "/api/console";
const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "/api";
const TIMEOUT_MS = 20_000;

export type Money = string | number;

export interface CreditCollection {
  due_date: string;
  amount_krw: Money;
  /** 결제 예정일이 기준일보다 앞선다 — 이미 받았어야 할 돈이다. */
  overdue: boolean;
  /** 이 건까지 예정대로 들어온다면 남는 여신. 한도가 없으면 `null` 이다. */
  available_credit_after_krw: Money | null;
}

export interface PartnerCredit {
  partner_id: string;
  partner_name: string | null;
  /** 거래처 계약 결제일수. `null` 은 모름이고 0일 결제와 다르다. */
  payment_days: number | null;
  /** `null` 은 한도가 정해지지 않았다는 뜻이다. 0원이 아니다. */
  credit_limit_krw: Money | null;
  credit_limit_evidence_grade: string | null;
  current_ar_krw: Money;
  overdue_ar_krw: Money;
  open_receivable_count: number;
  available_credit_krw: Money | null;
  /** 0~1 비율. 한도가 없거나 0원이면 `null` 이다. */
  credit_utilization_rate: Money | null;
  upcoming_collections: CreditCollection[];
}

export interface CreditResponse {
  sim_run_id: string;
  as_of: string;
  partners: PartnerCredit[];
}

export interface CreditLimitHistoryItem {
  partner_credit_limit_id: string;
  partner_id: string;
  credit_limit_krw: Money;
  effective_from: string;
  effective_to: string | null;
  evidence_grade: "OFFICIAL" | "VENDOR" | "SIM_FIXED";
  source_ref: string;
  recorded_by: string;
  policy_version: string;
  usage_scope: string;
  note: string | null;
  is_active: boolean;
  is_current: boolean;
}

export async function fetchCredit(simRun: string, asOf: string): Promise<CreditResponse> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const search = new URLSearchParams({ sim_run_id: simRun, as_of: asOf });
    let res: Response;
    try {
      res = await fetch(`${CONSOLE_BASE}/finance/credit?${search.toString()}`, {
        headers: { Accept: "application/json" },
        signal: controller.signal,
      });
    } catch {
      throw new Error(
        controller.signal.aborted
          ? `${TIMEOUT_MS / 1000}초 안에 응답이 오지 않아 끊었습니다.`
          : "서버에 닿지 못했습니다 — 잠시 후 다시 시도해 주세요.",
      );
    }
    if (!res.ok) throw new Error(`여신 현황을 불러오지 못했습니다 (${res.status})`);
    return (await res.json()) as CreditResponse;
  } finally {
    clearTimeout(timer);
  }
}

export async function registerCreditLimit(input: {
  partner_id: string; credit_limit_krw: string; effective_from: string;
  evidence_grade: "OFFICIAL" | "VENDOR" | "SIM_FIXED"; source_ref: string;
  recorded_by: string; note?: string;
}): Promise<void> {
  const res = await fetch(`${API_BASE}/finance/credit-limits`, {
    method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(input),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null) as { detail?: string } | null;
    throw new Error(body?.detail ?? "여신한도를 저장하지 못했습니다.");
  }
}

export async function fetchCreditLimitHistory(
  partnerId: string,
  asOf: string,
): Promise<CreditLimitHistoryItem[]> {
  const search = new URLSearchParams({ partner_id: partnerId, as_of: asOf });
  const res = await fetch(`${API_BASE}/finance/credit-limits?${search.toString()}`, {
    headers: { Accept: "application/json" },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null) as { detail?: string } | null;
    throw new Error(body?.detail ?? "여신한도 이력을 불러오지 못했습니다.");
  }
  return (await res.json()) as CreditLimitHistoryItem[];
}
async function financePost<T>(path: string, input: object, fallback: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(input),
  });
  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(body?.detail ?? fallback);
  }
  return (await res.json()) as T;
}

export function recordCollection(input: {
  sim_run_id: string; financing_mode: string; collection_date: string; receivable_id: string;
  collect_all: boolean; amount_krw?: string; source_ref: string; recorded_by: string; note?: string;
}): Promise<{ receivable_id: string; received_delta_krw: Money; outstanding_amount_krw: Money; status: string }> {
  return financePost("/finance/receivables/collections", input, "수금을 기록하지 못했습니다.");
}

export function recordCashAdjustment(input: {
  sim_run_id: string; financing_mode: string; adjustment_date: string;
  direction: "INFLOW" | "OUTFLOW"; category: "OWNER_INJECTION" | "OWNER_WITHDRAWAL" | "OTHER";
  amount_krw: string; source_ref: string; recorded_by: string; note?: string;
}): Promise<{ cash_adjustment_id: string; current_cash_krw: Money }> {
  return financePost("/finance/cash-adjustments", input, "자금 조정을 기록하지 못했습니다.");
}
