/**
 * 화면용 API 클라이언트 — 여섯 탭이 쓰는 값.
 *
 * ★ **에이전트 API(`lib/api.ts`)와 다른 것입니다.** 저쪽은 마스터를 돌리고,
 *   이쪽은 저장된 값을 읽기만 합니다. 주소로 갈라 둡니다 —
 *   `/master/ask` 는 돌리는 것, `/api/…` 는 보는 것.
 *
 * ★ **여기 있는 타입은 백엔드 `app/api/primitives.py` 를 그대로 옮긴 것입니다.**
 *   한쪽만 고치면 화면이 조용히 빈 칸이 됩니다. 둘을 같이 고치세요.
 */

const BASE = process.env.NEXT_PUBLIC_SCREEN_BASE ?? "/api/screen";

export class ScreenError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function get<T>(path: string, params: Record<string, string>): Promise<T> {
  const qs = new URLSearchParams(params).toString();
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}?${qs}`, { headers: { Accept: "application/json" } });
  } catch {
    throw new ScreenError(0, "백엔드에 닿지 못했습니다 — 서버가 떠 있는지 확인해 주세요.");
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
    throw new ScreenError(res.status, detail);
  }
  return JSON.parse(body) as T;
}

/* ── 부품 ─────────────────────────────────────────────────────────────── */

export type Tone = "neutral" | "good" | "warn" | "bad" | "info" | "sim";
export type Align = "left" | "right" | "center";

export interface Stat {
  label: string;
  value: string;
  unit: string | null;
  detail: string | null;
  tone: Tone;
  raw: number | null;
}
export interface Badge {
  text: string;
  tone: Tone;
}
export interface Note {
  text: string;
  tone: Tone;
}
export interface Column {
  key: string;
  label: string;
  align: Align;
  mono: boolean;
}
export type Cell = string | number | null;
export interface Table {
  columns: Column[];
  rows: Record<string, Cell>[];
  note: Note | null;
  empty_text: string;
}
export interface Series {
  name: string;
  data: (number | null)[];
  tone: Tone;
  dashed: boolean;
  width: number;
  opacity: number;
  end_dot: boolean;
}
export interface Band {
  name: string;
  hi: (number | null)[];
  lo: (number | null)[];
  tone: Tone;
}
export interface Marker {
  index: number;
  value: number;
  label: string;
  tone: Tone;
}
export interface Chart {
  label: string;
  y_min: number;
  y_max: number;
  y_ticks: number[];
  y_unit: string;
  series: Series[];
  bands: Band[];
  markers: Marker[];
  note: Note | null;
  /** 회색 칸이 무엇인지. 탭마다 뜻이 다르다 (휴장 · 게이트 구간). */
  shade_label: string;
  /** 공용 날짜축을 안 쓰는 그래프의 가로 눈금. 비면 날짜축을 쓴다. */
  x_labels: string[];
  /** 세로 눈금 글자를 직접 줄 때. 비면 숫자 + `y_unit`. */
  y_labels: string[];
}
export interface Day {
  date: string;
  dow: string;
  market_open: boolean;
  survey: boolean;
}
export interface CalendarAxis {
  as_of: string;
  as_of_index: number;
  days: Day[];
}
export interface Source {
  filled: boolean;
  owner: string;
  note: string | null;
}
export interface Card {
  key: string;
  title: string;
  subtitle: string | null;
  source_ref: string | null;
  lead: Note | null;
  flow: string[];
  stats: Stat[];
  table: Table | null;
  chart: Chart | null;
  bullets: string[];
  footer: string | null;
}
export interface Pane {
  key: string;
  label: string;
  stats: Stat[];
  cards: Card[];
}

/* ── 탭 ───────────────────────────────────────────────────────────────── */

export interface ItemCard {
  item: string;
  grade: string;
  spec: string | null;
  target_date: string;
  predicted: number;
  lower: number;
  upper: number;
  unit: string;
  ci_width: number;
  review: boolean;
  use_recommended: boolean;
  /** 모델이 아니라 어제값 그대로인가 (리드타임 3 미만). */
  gated?: boolean;
}

export interface DashboardTab {
  axis: CalendarAxis;
  badges: Badge[];
  stats: Stat[];
  forecast_cards: ItemCard[];
  purchase: Table;
  purchase_note: Note;
  cash_chart: Chart;
  stock_chart: Chart;
  sources: Source[];
}

export interface KindOption {
  kind: string;
  label: string;
  role: string;
}
export interface BaseDateOption {
  base_dt: string;
  total: number;
  scored: number;
  /** 2026-08-28 이전 예측. 다른 날과 나란히 놓고 비교하면 안 된다. */
  pre_fix: boolean;
}
export interface ForecastTab {
  kinds: KindOption[];
  selected_kind: string;
  items: string[];
  selected: string;
  base_dates: BaseDateOption[];
  selected_base_dt: string;
  base_dates_truncated: boolean;
  notice: Note | null;
  cards: ItemCard[];
  axis: CalendarAxis;
  chart: Chart;
  rows: Table;
  gate_lead: number;
  quality_note: string | null;
  accuracy: Table;
  quality: Table;
  caveat: Note;
  source: Source;
}

export interface Reason {
  source: string;
  text: string;
  ref: string | null;
  carried: boolean;
}
export interface Plan {
  key: string;
  coverage: string;
  knob: string;
  qty_kg: number;
  amount_krw: number;
  unit_price: number;
  grade: string;
  max_price: number;
  legs: Table;
  payments: Table;
  reasons: Reason[];
  risks: string[];
  pending: boolean;
  approved: boolean;
}
export interface PurchaseTab {
  stats: Stat[];
  plans: Plan[];
  plans_note: Note;
  committed: Table;
  committed_note: Note;
  source: Source;
}

export interface StateOption {
  key: string;
  label: string;
  explain: string;
}
export interface FlowCell {
  label: string;
  value: string;
  term: string;
  tone: string;
}
export interface FinanceTab {
  states: StateOption[];
  selected: string;
  stats: Stat[];
  explain: Note;
  read_only: Note;
  cash_chart: Chart;
  flows: FlowCell[];
  balances: Stat[];
  balances_note: Note;
  closings: Table;
  tables_read: string[];
  source: Source;
}

export interface LogisticsTab {
  panes: Pane[];
  selected: string;
  principle: Note;
  source: Source;
}

export interface SalesTab {
  stats: Stat[];
  read_only: Note;
  cards: Card[];
  source: Source;
}

/* ── 부르는 곳 ────────────────────────────────────────────────────────── */

export const dashboard = (as_of: string) => get<DashboardTab>("/dashboard", { as_of });
export const forecast = (
  as_of: string,
  item: string,
  kind = "auc",
  base_dt?: string,
) => get<ForecastTab>("/forecast", { as_of, item, kind, ...(base_dt ? { base_dt } : {}) });
export const purchase = (as_of: string) => get<PurchaseTab>("/purchase", { as_of });
export const finance = (as_of: string, state: string) =>
  get<FinanceTab>("/finance", { as_of, state });
export const logistics = (as_of: string, pane: string) =>
  get<LogisticsTab>("/logistics", { as_of, pane });
export const sales = (as_of: string) => get<SalesTab>("/sales", { as_of });
