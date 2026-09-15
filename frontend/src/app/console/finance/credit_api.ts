/**
 * 재무 화면이 부르는 **거래처 여신 현황** 계약.
 *
 * ★ `lib/console_api.ts` 는 여러 도메인이 함께 쓰는 공용 클라이언트라 이 판에서 고치지
 *   않는다. 재무 화면만 쓰는 계약은 재무 화면 옆에 둔다 (`sales/sales_api.ts` 와 같은 자리).
 *
 * 🔴 **숫자를 여기서 만들지 않는다.** 가용 여신·사용률·수금 뒤 여신은 백엔드가 센 값이다.
 */

const CONSOLE_BASE = process.env.NEXT_PUBLIC_CONSOLE_BASE ?? "/api/console";
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
