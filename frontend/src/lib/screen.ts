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

/**
 * 응답 상한. **이 파일은 읽기 전용**이라 걸리는 곳이 뻔하고, 그래서 값을 잴 수 있었다.
 *
 * 2026-09-11 실측 (걷기가 도는 중 · 같은 LAN 의 DB) —
 *
 *     /dashboard  2.14s   ← 여섯 중 제일 느리다
 *     /logistics  0.73s
 *     /purchase   0.97s
 *     /sales      0.17s
 *
 * ★ 20초는 그 최대의 **약 10배**다. 넉넉한 쪽으로 골랐다 — 이 상한이 하려는 일은
 *   *"느린 요청을 빨리 자르는 것"* 이 아니라 *"영영 안 끝나는 요청을 끝내는 것"* 이다
 *   (`#81`). 백엔드는 **접속**이 5초에 끊기지만, **붙은 뒤 안 끝나는 것**은 안 막는다.
 */
const TIMEOUT_MS = 20_000;

async function get<T>(path: string, params: Record<string, string>): Promise<T> {
  const qs = new URLSearchParams(params).toString();
  // 🔴 **본문까지 같은 상한 안에 둔다.** 헤더만 먼저 오고 본문이 안 끝나는 경우가 있어
  //    ``res.text()`` 를 마친 뒤에야 타이머를 끈다.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    return await read<T>(`${BASE}${path}?${qs}`, controller);
  } finally {
    clearTimeout(timer);
  }
}

async function read<T>(url: string, controller: AbortController): Promise<T> {
  let res: Response;
  try {
    res = await fetch(url, {
      headers: { Accept: "application/json" },
      signal: controller.signal,
    });
  } catch {
    // ⚠️ **끊은 것과 못 닿은 것은 다른 사고다.** 한 문장으로 뭉치면 화면을 보는 사람이
    //    "서버를 켜라" 는 엉뚱한 조치를 한다 — 서버는 떠 있고 느린 것이다.
    throw controller.signal.aborted
      ? new ScreenError(0, `${TIMEOUT_MS / 1000}초 안에 응답이 오지 않아 끊었습니다 — 서버가 떠 있으나 느립니다.`)
      : new ScreenError(0, "백엔드에 닿지 못했습니다 — 서버가 떠 있는지 확인해 주세요.");
  }
  let body: string;
  try {
    body = await res.text();
  } catch {
    throw controller.signal.aborted
      ? new ScreenError(0, `${TIMEOUT_MS / 1000}초 안에 본문이 다 오지 않아 끊었습니다 — 서버가 떠 있으나 느립니다.`)
      : new ScreenError(0, "응답 본문을 읽지 못했습니다.");
  }
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
  /** 그래프가 쓰는 원시 수치. 표와 같은 줄인데 글자가 아니라 수다. */
  points: {
    lead: number;
    target_dt: string;
    pred: number | null;
    lo: number | null;
    hi: number | null;
    actual: number | null;
    err_pct: number | null;
    anchor: number | null;
    gated: boolean;
  }[];
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
  /** 재무 STRESS 로 나가는 수 (amount_max_krw = qty × 이것). **컷이 아니다.** */
  max_price: number;
  /**
   * 🔴 컷 기준 — 이보다 비싼 안은 실제로 죽는다.
   *
   * `max_price` 와 방향이 반대라 한 수가 둘을 대신할 수 없다 — 밴드가 좁아지면 컷은
   * 엄격해지고 재무 STRESS 는 느슨해진다. 지금은 두 값이 같아 화면이 안 틀리지만
   * 컷 산식을 바꾸는 날 갈라진다.
   *
   * ⚠️ `null` 이면 **그 실행에 칸이 없던 것**이다. `max_price` 로 메우지 않는다 —
   * 없는 값을 그럴듯한 값으로 채우면 없었다는 사실이 지워진다.
   */
  cut_unit_price: number | null;
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
  tone: string;
  group: "in" | "out";
}
export interface FinanceTab {
  has_data: boolean;
  states: StateOption[];
  selected: string;
  requested_as_of: string;
  state_as_of: string | null;
  latest_closing_as_of: string | null;
  stats: Stat[];
  action_card: Card | null;
  state_indicator: string | null;
  state_cards: Card[];
  explain: Note;
  read_only: Note;
  cash_chart: Chart | null;
  flows: FlowCell[];
  balances: Stat[];
  balances_note: Note | null;
  closings: Table | null;
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
export const finance = (as_of: string, state = "base") =>
  get<FinanceTab>("/finance", { as_of, state });
export const logistics = (as_of: string, pane: string) =>
  get<LogisticsTab>("/logistics", { as_of, pane });
export const sales = (as_of: string) => get<SalesTab>("/sales", { as_of });
