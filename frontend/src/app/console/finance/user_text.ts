/**
 * 기술 상태를 사용자가 읽는 말로 옮긴다. **값을 바꾸지 않는다.**
 *
 * 🔴 **여기서 판정을 고치지 않는다.** `FAIL` 을 «확인 필요» 로 부드럽게 만들면 화면이
 *    거절을 보류로 바꾼 것이 된다. 옮기는 것은 **이름뿐**이고, 원본 값은 기술 상세에
 *    그대로 남는다.
 *
 * 🔴 **모르는 값을 지어내지 않는다.** 표에 없는 상태가 오면 원본을 그대로 보여 준다 —
 *    «정상» 으로 뭉뚱그리면 새로 생긴 상태가 조용히 통과한다.
 */

/** 실행이 돌 수 있었는가. 업무 판정이 아니다. */
const RUNTIME: Record<string, string> = {
  READY: "정상 조회",
  RUNTIME_NOT_READY: "필요한 데이터가 부족합니다",
  ERROR: "처리 중 문제가 발생했습니다",
};

/** 업무 판정. 실행 성공 여부와 다른 축이다. */
const VERDICT: Record<string, string> = {
  PASS: "진행 가능",
  REVIEW_REQUIRED: "확인 필요",
  FAIL: "진행 어려움",
};

/** 봉투의 업무 상태. `skipped` 는 통과가 아니라 **판정을 못 낸 것**이다. */
const BUSINESS: Record<string, string> = {
  ok: "진행 가능",
  conditional: "확인 필요",
  reject: "진행 어려움",
  skipped: "판단 없음",
};

export type Tone = "good" | "warn" | "bad" | "neutral";

const VERDICT_TONE: Record<string, Tone> = {
  PASS: "good",
  ok: "good",
  REVIEW_REQUIRED: "warn",
  conditional: "warn",
  FAIL: "bad",
  reject: "bad",
};

const RUNTIME_TONE: Record<string, Tone> = {
  READY: "good",
  RUNTIME_NOT_READY: "warn",
  ERROR: "bad",
};

function say(table: Record<string, string>, value: string | null | undefined, blank: string) {
  if (value === null || value === undefined || value === "") return blank;
  return table[value] ?? blank;
}

export function runtimeText(value: string | null | undefined): string {
  return say(RUNTIME, value, "실행 기록 없음");
}

export function verdictText(value: string | null | undefined): string {
  //  ⚠️ `null` 은 «통과» 가 아니다. 판정을 못 낸 것이고, 그렇게 읽혀야 한다.
  return say(VERDICT, value, "판단 없음");
}

export function businessText(value: string | null | undefined): string {
  return say(BUSINESS, value, "판단 없음");
}

export function verdictTone(value: string | null | undefined): Tone {
  return value ? (VERDICT_TONE[value] ?? "neutral") : "neutral";
}

export function runtimeTone(value: string | null | undefined): Tone {
  return value ? (RUNTIME_TONE[value] ?? "neutral") : "neutral";
}

/** 화면 상단에 한 줄로 붙는 데이터 출처 설명. 내부 식별자를 쓰지 않는다. */
export const DATA_SOURCE_NOTE = "저장된 시뮬레이션 결과";

/**
 * 금액을 «만원» 단위로 줄여 축과 막대에 적는다. **표에는 쓰지 않는다** — 표는 원단위다.
 *
 * ⚠️ 반올림한 값이라 합계를 이것으로 다시 세면 안 된다. 눈금용이다.
 */
export function manwon(value: number): string {
  return `${Math.round(value / 10_000).toLocaleString("ko-KR")}만`;
}

/** `Money`(문자열 또는 숫자)를 그래프가 쓸 수 있는 수로 바꾼다. 없으면 `null` 이다. */
export function toNumber(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/** `2026-03-31` → `3/31`. 축 눈금용 짧은 표기다. */
export function shortDate(iso: string): string {
  const parts = iso.split("-");
  return parts.length === 3 ? `${Number(parts[1])}/${Number(parts[2])}` : iso;
}

/* ── 금액 · 비율 표기 ──────────────────────────────────────────────────── */

/**
 * 원 단위 금액. **소수점을 화면에서 지운다.**
 *
 * 🔴 **계산을 바꾸지 않는다.** DB 는 `numeric` 이라 `41375276.163` 처럼 소수가 딸려
 *    오고, 백엔드 값도 그대로 둔다. 바꾸는 것은 **읽는 방식**뿐이다 — 원화에 0.163원은
 *    없는 단위이고, 그 자리를 사용자가 유효숫자로 읽는다.
 *
 * ⚠️ **`null` 은 0 이 아니다.** 값이 없으면 «데이터 없음» 이고, 0 은 실제 0원이다.
 */
export function moneyWon(value: string | number | null | undefined): string {
  const parsed = toNumber(value);
  if (parsed === null) return "데이터 없음";
  return `${Math.round(parsed).toLocaleString("ko-KR")} 원`;
}

/**
 * **퍼센트 포인트**로 이미 계산된 값을 적는다. `35.77` → `35.8%`.
 *
 * 🔴 **비율 formatter 와 다른 계약이다.** 공용 `percent()` 는 `0.3577` 같은 0~1 비율을
 *    받아 안에서 100 을 곱한다. 백엔드 `_pct()` 는 이미 곱해서 `35.77` 을 주므로,
 *    거기에 `percent()` 를 쓰면 **3577.0%** 가 된다 — 실제로 그렇게 나왔다.
 *    이름으로 계약을 가른다.
 */
export function percentPoint(value: string | number | null | undefined): string {
  const parsed = toNumber(value);
  if (parsed === null) return "데이터 없음";
  return `${parsed.toFixed(1)}%`;
}

/* ── 저장된 코드값을 업무 말로 ──────────────────────────────────────────── */

/**
 * 🔴 **모르는 값을 «정상» 으로 만들지 않는다.** 표에 없으면 원본을 그대로 보여 준다.
 *    새 상태가 생긴 날 그것이 조용히 성공으로 둔갑하면, 화면은 틀린 채로 멀쩡해 보인다.
 */
function label(table: Record<string, string>, value: string | null | undefined, blank: string) {
  if (value === null || value === undefined || value === "") return blank;
  return table[value] ?? blank;
}

/** 채권 상태. 정본은 재무의 수금 전이 규칙이다. */
const RECEIVABLE: Record<string, string> = {
  OPEN: "수금 예정",
  PARTIAL: "일부 수금",
  COLLECTED: "수금 완료",
};

/** 채무 상태. */
const PAYABLE: Record<string, string> = {
  OPEN: "지급 예정",
  PARTIAL: "일부 지급",
  SETTLED: "정산 완료",
};

/** 거래처 유형. `partners_partner_type_check` 가 허용하는 다섯 값이다. */
const PARTNER_TYPE: Record<string, string> = {
  CUSTOMER: "고객사",
  SUPPLIER: "공급처",
  LOGISTICS_PROVIDER: "물류사",
  MARKET_REFERENCE: "시세 참조처",
  OTHER: "기타",
};

/** 거래 여부. `partners.active` 를 화면 말로 옮긴 값이다. */
const PARTNER_STATUS: Record<string, string> = {
  ACTIVE: "거래 중",
  INACTIVE: "거래 중지",
};

/** 조달 방식. `finance_states.financing_mode` 가 쓰는 값이다. */
const FINANCING_MODE: Record<string, string> = {
  LOAN_BASELINE: "기준 차입 시나리오",
  BASE_NO_LOAN: "무차입 기준",
};

/** 가격 계약 형태. */
const PRICING_CONTRACT: Record<string, string> = {
  MARKET_LINKED_COST_PLUS_CM: "시세 연동 원가 + 목표마진",
};

export const receivableStatusText = (v: string | null | undefined) =>
  label(RECEIVABLE, v, "상태 없음");
export const payableStatusText = (v: string | null | undefined) => label(PAYABLE, v, "상태 없음");
export const partnerTypeText = (v: string | null | undefined) => label(PARTNER_TYPE, v, "유형 미상");
export const partnerStatusText = (v: string | null | undefined) =>
  label(PARTNER_STATUS, v, "상태 미상");
export const financingModeText = (v: string | null | undefined) =>
  label(FINANCING_MODE, v, "조달 방식 미상");
export const pricingContractText = (v: string | null | undefined) =>
  label(PRICING_CONTRACT, v, "계약 형태 미상");

/**
 * 사람이 읽는 품목 이름.
 *
 * ★ **백엔드가 주는 `item_name` 이 먼저다.** 화면에 표를 두는 것은 그 값이 없을 때의
 *   마지막 수단이고, 그 표에도 없으면 **코드를 그대로** 보여 준다 — 이름을 지어내면
 *   새 품목이 남의 이름으로 팔린다.
 */
const ITEM_NAMES: Record<string, string> = {
  "ITEM-BAECHU": "배추",
  "ITEM-MU": "무",
  "ITEM-DAEPA": "대파",
  "ITEM-YANGPA": "양파",
  "ITEM-GAMJA": "감자",
  "ITEM-MANEUL": "마늘",
  "ITEM-PIMANUL": "피마늘",
  "ITEM-GEONGOCHU": "건고추",
};

export function itemText(
  itemName: string | null | undefined,
  itemId: string | null | undefined,
): string {
  if (itemName) return itemName;
  if (!itemId) return "품목 미상";
  if (ITEM_NAMES[itemId]) return ITEM_NAMES[itemId];
  //  🔴 모르는 **내부 품목 코드**만 가린다 — `ITEM-…` 이 사용자 이름처럼 보이면 안 된다.
  //     판매안처럼 이미 사람이 읽는 이름(`배추`)이 오는 자리는 그대로 둔다.
  return /^ITEM-/i.test(itemId) ? "등록되지 않은 품목" : itemId;
}

/**
 * 화면에 적을 거래처 이름.
 *
 * 🔴 **내부 시연 이름을 사용자 이름처럼 보여 주지 않는다.** 저장된 이름에 `Persona` ·
 *    `demo` 같은 꼬리표가 붙어 있으면 그 꼬리표만 떼고 남은 이름을 쓴다. 이름이 통째로
 *    내부 표기면 «이름 미등록» 으로 두되, **거래처를 뭉치지 않으려고** 코드를 짧게
 *    덧붙인다 — 전부 같은 이름이면 어느 거래처인지 구분할 수 없다.
 */
const INTERNAL_NAME = /\s*(persona|demo|sample|dummy|test)\b[\w-]*/gi;

export function partnerText(
  partnerName: string | null | undefined,
  partnerId: string | null | undefined,
): string {
  const cleaned = (partnerName ?? "").replace(INTERNAL_NAME, "").trim();
  if (cleaned) return cleaned;
  if (!partnerId) return "이름 미등록 거래처";
  return `이름 미등록 거래처 (${partnerId})`;
}

/**
 * 판매 재무 판정을 가른 사유. **왜 진행 가능하고 왜 어려운지를 한 줄로 말한다.**
 *
 * 🔴 **통과 사유는 여기 없다.** 백엔드가 이미 FAIL·REVIEW_REQUIRED 규칙의 사유만
 *    골라 보내고, 표에 없는 코드가 오면 원본을 그대로 보여 준다.
 */
const SALES_REASONS: Record<string, string> = {
  SALES_MARGIN_BELOW_MINIMUM: "기여이익률이 최소선에 못 미칩니다",
  SALES_MARGIN_BELOW_WARNING: "기여이익률이 경고선 아래입니다",
  SALES_MARGIN_RATE_UNCOMPUTABLE: "원가를 몰라 기여이익률을 계산하지 못했습니다",
  SALES_CREDIT_LIMIT_EXCEEDED: "거래처 여신 한도를 초과합니다",
  SALES_AMOUNT_MISMATCH: "제안한 매출액과 다시 계산한 금액이 다릅니다",
  SALES_PAYMENT_TERM_EXCEEDS_LIMIT: "결제일수가 허용 범위를 넘습니다",
  SALES_PAYMENT_TERM_TYPE_UNSUPPORTED: "지원하지 않는 결제 방식입니다",
  SALES_PAYMENT_DAYS_ABSENT: "결제일수가 적혀 있지 않습니다",
  SALES_PARTNER_HAS_OVERDUE_AR: "이 거래처에 연체된 미수금이 있습니다",
  SALES_CASHFLOW_DEPENDS_ON_PROJECTED_INFLOW: "아직 들어오지 않은 수금에 기대는 계획입니다",
  SALES_COLLECTION_OUTSIDE_HORIZON: "수금 예정일이 확인 가능한 기간 밖입니다",
  SALES_COLLECTION_RISK_MODE_UNSUPPORTED: "회수 위험 판정 방식을 지원하지 않습니다",
  SALES_COST_BASIS_UNAVAILABLE: "권위 있는 재고원가가 없어 판정을 닫았습니다",
  SALES_INPUT_INCOMPLETE: "제안에 필요한 값이 빠져 있습니다",
};

export function salesReasonText(code: string): string {
  return SALES_REASONS[code] ?? "세부 조건을 확인해 주세요.";
}

/** 아직 받지 못한 검증. 왜 «재무 검토 전» 인지를 말한다. */
const CAPABILITIES: Record<string, string> = {
  FINANCIAL_VALIDATION: "재무 검토",
  SELLABLE_SUPPLY_CONTEXT: "판매 가능 재고 확인",
  DELIVERY_FEASIBILITY_CONTEXT: "납품 가능 여부 확인",
  ADDITIONAL_SUPPLY_CONTEXT: "추가 조달 확인",
};

export function capabilityText(code: string): string {
  return CAPABILITIES[code] ?? "필요한 확인";
}

/* ── 여신 · 결제 조건 문구 ──────────────────────────────────────────────── */

/**
 * 0~1 비율을 퍼센트로 **적기만** 한다. 비율 자체는 백엔드가 센 값이다.
 *
 * ⚠️ `percentPoint` 와 계약이 다르다 — 그쪽은 이미 100 을 곱한 값을 받는다.
 */
export function ratioPercent(value: string | number | null | undefined): string {
  const parsed = toNumber(value);
  if (parsed === null) return "데이터 없음";
  return `${(parsed * 100).toFixed(1)}%`;
}

/** `2026-01-15` → `2026년 1월 15일`. */
export function longDate(iso: string | null | undefined): string {
  if (!iso) return "날짜 없음";
  const parts = iso.split("-");
  if (parts.length !== 3) return iso;
  return `${Number(parts[0])}년 ${Number(parts[1])}월 ${Number(parts[2])}일`;
}

/**
 * 결제 조건 한 줄. 🔴 **0일은 «당일 결제» 라는 정해진 조건이고, `null` 은 모름이다.**
 */
export function paymentTermText(days: number | null | undefined): string {
  if (days === null || days === undefined) return "결제 조건 미정";
  if (days === 0) return "판매 당일 결제";
  return `판매 후 ${days}일 내 결제 예정`;
}

/**
 * 판매 전에 먼저 받아야 하는 미수금. **백엔드가 센 값을 문장으로만 옮긴다.**
 *
 * 🔴 **0 은 «추가 회수 필요 없음» 이고 `null` 은 «판단할 정보 없음» 이다.** 둘을 같게
 *    적으면 여신 정보가 없는 거래처가 «바로 팔 수 있다» 로 읽힌다.
 */
export function collectionNeedText(required: string | number | null | undefined): string {
  const parsed = toNumber(required);
  if (parsed === null) return "여신 정보가 없어 선회수 필요 여부를 판단하지 못했습니다.";
  if (parsed <= 0) return "추가 회수 필요 없음 — 현재 여신 범위에서 판매할 수 있습니다.";
  return `기존 미수금 ${moneyWon(required)}을 먼저 회수하면 현재 여신한도 안에서 판매할 수 있습니다.`;
}

/** 여신 한도가 어떤 성격의 값인지. 모르는 등급은 원문을 적지 않는다. */
const CREDIT_GRADE: Record<string, string> = {
  SIM_FIXED: "시뮬레이션 고정 한도",
  CONTRACT: "계약 한도",
};

export function creditGradeText(value: string | null | undefined): string {
  return label(CREDIT_GRADE, value, "한도 근거 미상");
}

/* ── 판매 대화 문구 ─────────────────────────────────────────────────────── */

/**
 * 재무 검토 상태를 **한 문장으로.** 판정 코드를 세서 고르기만 한다.
 *
 * ★ 백엔드 `app/sales/adapter.py` 의 `review_sentence` 와 같은 규칙이다 — 대화 요약과
 *   마스터 조회 답이 다른 말을 하지 않게 한다.
 * 🔴 모르는 판정은 «검토 필요» 쪽으로 센다. 통과로 뭉치지 않는다.
 */
export function salesReviewSentence(verdicts: (string | null)[]): string {
  const passed = verdicts.filter((v) => v === "PASS").length;
  const failed = verdicts.filter((v) => v === "FAIL").length;
  const pending = verdicts.length - passed - failed;
  if (verdicts.length > 0 && failed === verdicts.length) {
    return `현재 조건으로 바로 진행하기 어려운 판매안이 ${failed}개 있습니다.`;
  }
  if (verdicts.length > 0 && passed === verdicts.length) {
    return "현재 조건에서 진행 가능한 판매안이 준비되어 있습니다.";
  }
  if (pending > 0) {
    return passed > 0
      ? `재무 검토가 필요한 판매안이 있습니다. 진행 가능한 판매안은 ${passed}개입니다.`
      : "재무 검토가 필요한 판매안이 있습니다.";
  }
  return `진행 가능한 판매안 ${passed}개와 현재 조건으로 진행하기 어려운 판매안 ${failed}개가 있습니다.`;
}

/**
 * 서버가 준 사유를 **사람이 읽을 수 있을 때만** 옮긴다.
 * 🔴 한글이 없거나 코드 모양(`SALES_…`, `FAILED`)이 섞였으면 적지 않는다 — 원문은 기록에 남는다.
 */
export function readableReason(text: string | null | undefined): string | null {
  if (!text) return null;
  const trimmed = text.trim();
  if (!/[가-힣]/.test(trimmed)) return null;
  if (/[A-Z][A-Z0-9]*_[A-Z0-9_]+|\b[A-Z]{4,}\b/.test(trimmed)) return null;
  return trimmed;
}

/**
 * 비용 생명주기 상태의 사용자 말.
 *
 * ★ «미지급» 이 아니라 «지급 예정» 이라고 부른다. 앞은 연체처럼 읽히고, 뒤는 아직
 *   날짜가 오지 않았다는 뜻이다 — 원장이 말하는 것은 뒤쪽이다.
 */
const EXPENSE_STATUS: Record<string, string> = {
  ACCRUED: "지급 예정",
  PAID: "지급 완료",
  CANCELLED: "취소됨",
};

export function expenseStatusText(value: string | null | undefined): string {
  return say(EXPENSE_STATUS, value, "상태 없음");
}
