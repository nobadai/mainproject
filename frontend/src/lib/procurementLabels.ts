/**
 * 매입 결과 화면의 **사람 말 사전과 변환.**
 *
 * ★ 실제 서비스 사용자에게 필요한 것만 사람 말로 보인다 (2026-09-15 결정).
 *   내부 코드 · 영어 필드명 · id · 검사 기록은 화면에 싣지 않는다.
 *   사전에 없는 항목은 코드째 보이지 않고 빠진다.
 *
 * ★ React 를 모른다. 순수 함수만 두어 표본 응답으로 따로 돌려 볼 수 있다.
 */

type Evidence = {
  agent: string;
  mode: string;
  claim: string;
  value: number | string;
  unit: string;
};

type ScenarioLike = { label?: string };

// ─── 값 형식 ──────────────────────────────────────────────────────────────

const nf = (n: number, digits = 0) =>
  n.toLocaleString("ko-KR", { maximumFractionDigits: digits });

/** 원. 1만 원 이상은 만 원 단위로 반올림한다. */
export function formatWon(v: number): string {
  if (Math.abs(v) >= 10000) return `${nf(Math.round(v / 10000))}만 원`;
  return `${nf(Math.round(v))}원`;
}

export function formatKg(v: number): string {
  return `${nf(Math.round(v))}kg`;
}

/** 단위와 항목을 보고 사람이 읽는 값으로. 읽을 수 없는 것은 `null`. */
export function formatEvidenceValue(base: string, value: number | string, unit: string): string | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  if (base === "purchase_payment_days") {
    return value === 0 ? "매입 당일" : `매입 후 ${nf(value)}일`;
  }
  if (base === "payment_pressure") return `${nf(value, 2)}배`;
  switch (unit) {
    case "krw":
    case "KRW":
      return formatWon(value);
    case "KRW/kg":
    case "krw/kg":
      return `${nf(Math.round(value))}원/kg`;
    case "kg":
      return formatKg(value);
    case "days":
    case "day":
      return `${nf(value, 1)}일`;
    case "day_of_month":
      return `매월 ${nf(value)}일`;
    case "ratio":
      return value >= 0 && value <= 1 ? `${nf(value * 100, 1)}%` : `${nf(value, 2)}배`;
    default:
      return null;
  }
}

// ─── 근거 사전 ────────────────────────────────────────────────────────────

/** 항목 이름. `{item}` 은 품목 이름으로 바뀐다. 여기 없는 항목은 화면에 안 나온다. */
export const CLAIM_LABEL: Record<string, string> = {
  // 재무
  finance_cap_amount_krw: "매입에 쓸 수 있는 한도",
  base_projected_cash_min: "앞으로 가장 적을 때의 현금",
  available_cash: "지금 쓸 수 있는 현금",
  minimum_cash_balance_krw: "지켜야 할 최소 현금",
  payment_pressure: "지급 부담(최소 현금 대비)",
  critical_payment_dates: "최소 현금 아래로 내려가는 지급일 금액",
  payroll_payment_day: "급여일",
  purchase_payment_days: "매입 대금 지급",
  margin_defense_floor_rate: "최소 마진율",
  scenario_projected_cash_min: "이 안 실행 뒤 최저 현금",
  stress_projected_cash_min: "회수가 늦어질 때 최저 현금",
  // 물류
  warehouse_free_kg: "창고 빈 자리",
  guaranteed_capacity_kg: "창고 보장 용량",
  burst_capacity_kg: "창고 최대 용량",
  used_capacity_kg: "현재 쓰는 창고",
  rental_cap_kg: "외부 창고 임차 가능량",
  inbound_lead_days: "입고까지 걸리는 날",
  daily_inbound_capacity_kg: "하루 입고 가능량",
  inbound_transport_capacity_kg: "하루 입고 운송량",
  shared_daily_outbound_capacity_kg: "하루 출고 가능량",
  "item_storage_policies.operational_limit_days": "{item} 보관 가능 기간",
  "item_storage_policies.medium_grade_factor": "{item} 중품 보관 기간 비율",
  // 매입 (안별)
  coverage_days: "며칠치를 사나",
  total_qty_kg: "매입량",
  total_amount_krw: "매입 금액",
  max_price: "이보다 비싸면 안 사는 단가",
  cut_unit_price: "이보다 비싸면 안 사는 단가",
};

/** 사용자에게 뜻이 없는 내부 값. */
const HIDDEN_CLAIMS = new Set([
  "policy_version_used",
  "cap_by_date_window_days",
  "cap_by_date_policy",
  "soft_warnings",
  "verdict",
  "cap_by_date",
  "expected_arrival_dates",
  "situation",
  "allowed_axes",
  "scenarios",
]);

const HIDDEN_UNITS = new Set([
  "identity",
  "enum_code",
  "warning_count",
  "date_count",
  "non_ok_input_count",
  "version",
  "count",
]);

/** 안 이름. 「보수」 → 「보수안」. */
export function scenarioName(label: string | undefined | null, index?: number): string {
  if (label) return label.endsWith("안") ? label : `${label}안`;
  return index == null ? "안" : `${index + 1}번째 안`;
}

type ParsedClaim = {
  base: string;
  labelKey: string;
  item: string | null;
  scenarioIndex: number | null;
};

/** `scenarios[0].total_qty_kg` · `item_storage_policies[배추].x` 를 푼다. */
export function parseClaim(claim: string): ParsedClaim {
  const idx = claim.match(/^(?:scenarios|verdicts)\[(\d+)\]\.(.+)$/);
  if (idx) {
    return { base: idx[2], labelKey: idx[2], item: null, scenarioIndex: Number(idx[1]) };
  }
  const item = claim.match(/^([A-Za-z_]+)\[([^\]]+)\]\.(.+)$/);
  if (item) {
    return {
      base: item[3],
      labelKey: `${item[1]}.${item[3]}`,
      item: item[2],
      scenarioIndex: null,
    };
  }
  return { base: claim, labelKey: claim, item: null, scenarioIndex: null };
}

export type EvidenceRow = {
  agent: string;
  /** 안 이름. 안에 딸리지 않은 값은 `null`. */
  scenario: string | null;
  label: string;
  value: string;
};

export type EvidenceGroup = {
  agent: string;
  scenario: string | null;
  rows: EvidenceRow[];
};

/**
 * 근거 → 사용자에게 보일 줄.
 *
 * - 이 실행의 품목이 아닌 품목별 근거는 뺀다. 품목을 모르면 품목별 근거는 모두 뺀다.
 * - 내부 값 · 사전에 없는 항목 · 읽을 수 없는 값은 뺀다.
 * - 같은 부서가 안에 딸리지 않은 자리에서 같은 값을 이미 말했으면 안별 줄은 뺀다.
 * - 한 묶음 안에서 같은 이름·같은 값은 한 번만.
 */
export function evidenceGroups(
  evidences: Evidence[],
  scenarios: ScenarioLike[],
  item: string | null,
): EvidenceGroup[] {
  const candidates: (EvidenceRow & { base: string; index: number | null })[] = [];

  for (const e of evidences ?? []) {
    const p = parseClaim(e.claim);
    if (HIDDEN_CLAIMS.has(p.base) || HIDDEN_UNITS.has(e.unit)) continue;
    if (p.item != null && (item == null || p.item !== item)) continue;
    const template = CLAIM_LABEL[p.labelKey];
    if (!template) continue;
    const value = formatEvidenceValue(p.base, e.value, e.unit);
    if (value == null) continue;
    const scenario =
      p.scenarioIndex == null ? null : scenarioName(scenarios?.[p.scenarioIndex]?.label, p.scenarioIndex);
    candidates.push({
      agent: e.agent,
      scenario,
      label: template.replace("{item}", p.item ?? ""),
      value,
      base: p.base,
      index: p.scenarioIndex,
    });
  }

  const common = new Set(
    candidates.filter((c) => c.scenario == null).map((c) => `${c.agent}|${c.base}|${c.value}`),
  );

  const groups: EvidenceGroup[] = [];
  const seen = new Set<string>();
  for (const c of candidates) {
    if (c.scenario != null && common.has(`${c.agent}|${c.base}|${c.value}`)) continue;
    const key = `${c.agent}|${c.scenario ?? ""}|${c.label}|${c.value}`;
    if (seen.has(key)) continue;
    seen.add(key);
    let g = groups.find((x) => x.agent === c.agent && x.scenario === c.scenario);
    if (!g) {
      g = { agent: c.agent, scenario: c.scenario, rows: [] };
      groups.push(g);
    }
    g.rows.push({ agent: c.agent, scenario: c.scenario, label: c.label, value: c.value });
  }

  // 같은 이름에 값이 둘이면 (예측 상단 단가와 매입 컷 단가가 갈린 날) 구분해 적는다.
  for (const g of groups) {
    const byLabel = new Map<string, number>();
    for (const r of g.rows) byLabel.set(r.label, (byLabel.get(r.label) ?? 0) + 1);
    let n = 0;
    for (const r of g.rows) {
      if ((byLabel.get(r.label) ?? 0) > 1) {
        n += 1;
        if (n > 1) r.label = `${r.label} (예측 상단 기준)`;
      }
    }
  }

  // 부서끼리 모으고, 부서 안에서는 공통 값 다음에 안 순서대로.
  const agentOrder: string[] = [];
  for (const g of groups) if (!agentOrder.includes(g.agent)) agentOrder.push(g.agent);
  const scenarioOrder = (s: string | null) => {
    if (s == null) return -1;
    const i = (scenarios ?? []).findIndex((x, k) => scenarioName(x.label, k) === s);
    return i < 0 ? 999 : i;
  };
  return groups.sort(
    (a, b) =>
      agentOrder.indexOf(a.agent) - agentOrder.indexOf(b.agent) ||
      scenarioOrder(a.scenario) - scenarioOrder(b.scenario),
  );
}

// ─── 판정 ────────────────────────────────────────────────────────────────

export const STATUS_LABEL: Record<string, string> = {
  ok: "통과",
  conditional: "조건부",
  reject: "거절",
};

export function statusLabel(status: unknown): string {
  return (typeof status === "string" && STATUS_LABEL[status]) || "판정 없음";
}

const isRecord = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const str = (v: unknown): string | null => (typeof v === "string" && v !== "" ? v : null);

export type FinanceScenarioSummary = {
  name: string;
  status: string;
  reason: string | null;
  cashMin: string | null;
  stressCashMin: string | null;
  cap: string | null;
  payments: { date: string; amount: string }[];
  adjustmentNote: string | null;
};

export type FinanceSummary = {
  /** 안마다 한도가 같으면 여기 한 번. */
  sharedCap: string | null;
  scenarios: FinanceScenarioSummary[];
};

export function financeSummary(payload: unknown): FinanceSummary | null {
  if (!isRecord(payload) || !Array.isArray(payload.verdicts)) return null;
  const rows = payload.verdicts.filter(isRecord);
  if (rows.length === 0) return null;

  const caps = rows.map((r) => num(r.finance_cap_amount_krw));
  const shared = caps.every((c) => c != null && c === caps[0]) ? caps[0] : null;

  return {
    sharedCap: shared == null ? null : formatWon(shared),
    scenarios: rows.map((r, i) => {
      const cap = num(r.finance_cap_amount_krw);
      const cashMin = num(r.scenario_projected_cash_min);
      const stress = num(r.stress_projected_cash_min);
      const adjustments = Array.isArray(r.suggested_adjustments) ? r.suggested_adjustments.length : 0;
      const payments = (Array.isArray(r.payment_schedule) ? r.payment_schedule : [])
        .filter(isRecord)
        .flatMap((p) => {
          const date = str(p.payment_date);
          const amount = num(p.amount_krw);
          return date && amount != null ? [{ date, amount: formatWon(amount) }] : [];
        });
      return {
        name: scenarioName(str(r.scenario_id) ?? str(r.label), i),
        status: statusLabel(r.verdict),
        reason: str(r.reason),
        cashMin: cashMin == null ? null : formatWon(cashMin),
        stressCashMin: stress == null ? null : formatWon(stress),
        cap: shared == null && cap != null ? formatWon(cap) : null,
        payments,
        adjustmentNote:
          adjustments > 0 ? `재무가 이 안을 고치자는 제안 ${adjustments}건을 냈습니다.` : null,
      };
    }),
  };
}

/** 물류 점검 항목. 여기 없는 항목은 화면에 안 나온다. */
export const LOGISTICS_CHECK_LABEL: Record<string, string> = {
  "LOG-H01": "창고 용량",
  "LOG-H02": "구역별 용량",
  "LOG-H03": "하루 입고량",
  "LOG-H04": "입고 운송량",
  "LOG-H05": "입고 소요일",
};

export const CHECK_STATUS_LABEL: Record<string, string> = {
  PASS: "통과",
  UNRESOLVED: "확인 못 함(정보 없음)",
  FAIL: "넘침",
};

export type LogisticsSummary = {
  scenarios: { name: string; status: string }[];
  arrivals: string[];
  freeByDate: { date: string; free: string }[];
  checks: { name: string; status: string; attention: boolean }[];
  conditionalNote: string | null;
};

/** 받침이 있으면 「을」, 없으면 「를」. */
function objectParticle(word: string): string {
  const code = word.charCodeAt(word.length - 1) - 0xac00;
  if (code < 0 || code > 11171) return "을(를)";
  return code % 28 === 0 ? "를" : "을";
}

export function logisticsSummary(payload: unknown, businessStatus: string): LogisticsSummary | null {
  if (!isRecord(payload)) return null;

  const scenarios = (Array.isArray(payload.scenario_results) ? payload.scenario_results : [])
    .filter(isRecord)
    .map((s, i) => ({
      name: scenarioName(str(s.label), i),
      status: statusLabel(s.verdict),
      ok: s.verdict === "ok",
    }));

  const arrivals = (Array.isArray(payload.expected_arrival_dates) ? payload.expected_arrival_dates : [])
    .map(str)
    .filter((d): d is string => d != null);

  const freeByDate = Object.entries(isRecord(payload.cap_by_date) ? payload.cap_by_date : {}).flatMap(
    ([date, kg]) => {
      const n = num(kg);
      return n == null ? [] : [{ date, free: formatKg(n) }];
    },
  );

  const checks = (Array.isArray(payload.hard_constraints) ? payload.hard_constraints : [])
    .filter(isRecord)
    .flatMap((c) => {
      const code = str(c.code);
      const name = code ? LOGISTICS_CHECK_LABEL[code] : undefined;
      if (!name) return [];
      const status = str(c.status);
      return [
        {
          name,
          status: (status && CHECK_STATUS_LABEL[status]) || "판정 없음",
          attention: status !== "PASS",
          unresolved: status === "UNRESOLVED",
        },
      ];
    });

  const unresolved = checks.filter((c) => c.unresolved).map((c) => c.name);
  const status = businessStatus || str(payload.verdict) || "";
  let conditionalNote: string | null = null;
  if (
    status === "conditional" &&
    scenarios.length > 0 &&
    scenarios.every((s) => s.ok) &&
    unresolved.length > 0
  ) {
    const names = unresolved.join("·");
    conditionalNote = `안에는 문제가 없고, ${names}${objectParticle(names)} 확인하지 못해 조건부입니다.`;
  }

  if (scenarios.length + arrivals.length + freeByDate.length + checks.length === 0) return null;

  return {
    scenarios: scenarios.map(({ name, status: s }) => ({ name, status: s })),
    arrivals,
    freeByDate,
    checks: checks.map(({ name, status: s, attention }) => ({ name, status: s, attention })),
    conditionalNote,
  };
}
