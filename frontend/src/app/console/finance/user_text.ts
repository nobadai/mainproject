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
  return table[value] ?? value;
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
