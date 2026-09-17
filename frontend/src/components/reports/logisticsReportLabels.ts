/**
 * 재고·물류 운영 보고서의 **사용자 표시명 한 벌.**
 *
 * ★ **표현만 옮긴다.** 여기 있는 것은 전부 Backend 가 이미 판정해 내려준 값을 사람 말로
 *   바꾸는 사전이다. 숫자를 보고 상태를 새로 매기지 않는다 —
 *   `remaining_freshness_days` 로 회전 상태를 다시 정하는 것 같은 일은 하지 않는다.
 *
 * ★ **키는 실제 계약값이다.** 각 표의 주석에 정본 위치를 적어 둔다. 계약에 없는 상태를
 *   지어내지 않고, 계약이 늘면 여기도 같이 는다.
 *
 * 🔴 **모르는 값은 원문을 그대로 두지 않는다.** 사전에 없으면 「—」다. 내부 코드가
 *    사용자 표에 새는 것보다 «정보 없음» 이 낫다.
 */

/**
 * 회전 Signal. 정본 `backend/app/logistics/turnover.py::TurnoverStatus`.
 *
 * ⚠️ **신선도 상태가 아니라 회전 상태다.** 세 값은 Persona 05 §4 어휘 그대로이고
 *    `STORAGE_TARGET_EXCEEDED` 는 회사 내부 회전목표 초과일 뿐 **판매불가가 아니다** —
 *    「만료」로 옮기면 없는 판정을 만드는 것이 된다.
 */
export const TURNOVER_STATUS_LABEL: Record<string, string> = {
  NORMAL: "정상",
  SELL_PRIORITY: "판매 우선 검토",
  STORAGE_TARGET_EXCEEDED: "회전목표 초과",
};

/**
 * Receipt 진행 상태. 정본 `backend/app/logistics/receipts.py::ReceiptStatus`.
 *
 * ★ 사용자가 볼 흐름은 **입고 예정 → 창고 도착 → 검수 → 재고 반영** 넷이다. Receipt 가
 *   선 순간부터는 「입고 예정」이 아니라 이 어휘로 말한다.
 */
export const RECEIPT_STATUS_LABEL: Record<string, string> = {
  ARRIVED: "창고 도착",
  INSPECTING: "검수 중",
  INSPECTED: "검수 완료",
  PUTAWAY_DONE: "재고 반영 완료",
  CLOSED: "종료",
};

/** 검수 판정. 정본 `backend/app/logistics/inspections.py::InspectionVerdict`. */
export const INSPECTION_VERDICT_LABEL: Record<string, string> = {
  PASS: "합격",
  HOLD: "보류",
  REJECT: "거절",
};

/** 예약 상태. 정본 `backend/app/logistics/outbound.py::ReservationStatus`. */
export const RESERVATION_STATUS_LABEL: Record<string, string> = {
  RESERVED: "예약",
  PARTIALLY_ALLOCATED: "일부 할당",
  ALLOCATED: "할당 완료",
  RELEASED: "예약 해제",
  CANCELLED: "취소",
};

/**
 * 도착 전 입고 예정 목록을 **확인했는가**.
 * 정본 `backend/app/logistics/schemas.py::RuntimeSourceStatus`.
 *
 * ⚠️ 내부 이름은 `in_transit` 이지만 **차량 운송을 추적하는 값이 아니다.** 「입고 일정에
 *    올라 있고 아직 창고에 도착하지 않은 건」이라, 사용자에게는 「입고 예정」으로 말한다.
 *
 * 🔴 `CONFIRMED_ZERO`(0건 확인)와 `UNRESOLVED`(못 확인)는 다른 사실이다. 사용자에게도
 *    다른 문장으로 말한다 — 「0건」과 「모름」을 같은 칸에 담지 않는다.
 */
export const IN_TRANSIT_STATUS_TEXT: Record<string, string> = {
  CONFIRMED: "입고 예정 목록을 확인했습니다.",
  CONFIRMED_ZERO: "예정된 입고 없음",
  UNRESOLVED: "이 날짜의 입고 예정 목록을 확인하지 못했습니다 — 0건이라는 뜻이 아닙니다.",
};

/**
 * 도착 전 물량 한 줄의 도착 상태. 정본은 Backend `_logistics_arrival_display_state` 다.
 *
 * 🔴 **화면이 날짜를 보고 상태를 정하지 않는다.** 여기서는 Backend 가 준 값을 옮기기만
 *    한다. 도착 «자격» 판정(진행 가능 · 처리 보류 · 판정 불가)의 주인은 물류이고 그 결과는
 *    위쪽 카드로 따로 나간다.
 */
export const ARRIVAL_DISPLAY_STATE_LABEL: Record<string, string> = {
  SCHEDULED: "도착 예정",
  OVERDUE: "도착 지연",
};

/** 판매가능량을 못 낸 이유. 정본 `schemas.py::AvailableQtyUnresolvedReason`. */
export const AVAILABLE_UNRESOLVED_TEXT: Record<string, string> = {
  OUTBOUND_COMMITMENTS_UNRESOLVED: "확정 출고 물량을 확인하지 못했습니다",
  RUNTIME_SNAPSHOT_UNAVAILABLE: "이 날짜의 물류 스냅샷이 없습니다",
};

/** 사전에 있으면 사람 말로, 없으면 「—」. **내부 코드를 그대로 내보내지 않는다.** */
export function label(table: Record<string, string>, value: unknown, empty = "—"): string {
  if (value === null || value === undefined || value === "") return empty;
  return table[String(value)] ?? empty;
}

/**
 * Lot 사용자 표시명. **raw `lot_id` 를 쪼개지 않는다.**
 *
 * 구조화 칸(`item_name` · `received_at`)으로만 만들고, 같은 품목·같은 입고일 Lot 이
 * 여럿일 때만 Backend 가 매긴 안정된 순번(`display_index`)을 덧붙인다.
 */
export function lotDisplayName(row: {
  item_name?: unknown;
  item_id?: unknown;
  received_at?: unknown;
  display_index?: unknown;
  display_group_size?: unknown;
}): string {
  const item = row.item_name ?? row.item_id;
  const name = item === null || item === undefined || item === "" ? "품목 미상" : String(item);
  const received = monthDay(row.received_at);
  const head = received === "—" ? name : `${name} · ${received} 입고`;
  const size = Number(row.display_group_size);
  const index = Number(row.display_index);
  if (Number.isFinite(size) && size > 1 && Number.isFinite(index)) return `${head} · #${index}`;
  return head;
}

/** `2026-08-28` → 「08/28」. 모양이 다르면 「—」. */
export function monthDay(value: unknown): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value ?? ""));
  return match ? `${match[2]}/${match[3]}` : "—";
}

/** 일수 한 칸. 🔴 **단위를 뗀 숫자를 내보내지 않는다.** `0일` 과 「모름」은 다르다. */
export function days(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  return Number.isFinite(number) ? `${number.toLocaleString("ko-KR")}일` : "—";
}

/**
 * Lot 관리 조치. 🔴 **boolean 두 칸을 사용자가 조합하게 하지 않는다.**
 *
 * ★ `sell_priority` · `disposal_candidate` 만 본다. `remaining_freshness_days` 를 보고
 *   둘 중 무엇도 새로 판단하지 않는다 — 판정의 주인은 Backend `turnover` 다.
 */
export function lotAction(sellPriority: unknown, disposalCandidate: unknown): string {
  const sell = sellPriority === true;
  const disposal = disposalCandidate === true;
  if (sell && disposal) return "우선 출고 · 폐기 검토 대상";
  if (sell) return "우선 출고 대상";
  if (disposal) return "폐기 검토 대상";
  return "특이사항 없음";
}

/** 건수 한 칸. 숫자가 아니면 0 으로 세지 않고 `null`(모름)로 돌려준다. */
function count(value: unknown): number | null {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

/**
 * 「입고일 + 품목」 묶음의 **재고 처리 결과** → 사람 말.
 *
 * 🔴 **완료의 형태가 둘이다** (#805). 재고가 선 완료(`stock_applied`)와 수용 0 으로
 *    «만들 재고가 없어» 끝난 완료(`settled_without_stock`)는 둘 다 정상 완료다.
 *    종전처럼 「재고 반영 0 / 1건」 한 칸만 적으면 뒤쪽이 **미처리 건으로 잘못 읽힌다.**
 *    그래서 「아직」·「실패」·「미처리」라는 말을 이 칸에 쓰지 않는다.
 *
 * 🔴 **화면이 판정하지 않는다.** 네 건수는 Backend `_logistics_receipt_rollup` 이
 *    정본 계약값(`stock_applied` · `settled_without_stock`)을 그대로 센 것이고,
 *    여기서는 더하고 빼서 문장으로 옮기기만 한다.
 *
 * ⚠️ 「처리 중」은 **「재고 반영 대기」가 아니다.** 두 완료 형태 중 어느 쪽에도 아직
 *    닿지 않았다는 사실뿐이라, 검수 전일 수도 있다 — 어느 단계인지는 옆 「검수 결과」
 *    칸이 말한다. 여기서 단계를 단정하지 않는다.
 *
 * 🔴 「확인 못 함」(`settled_unknown_count`)은 **0 건이 아니라 모름이다.** 그날 입고
 *    일정을 못 읽어 두 완료 형태를 가릴 수 없었던 건이다.
 */
export function stockApplyText(row: {
  receipt_count?: unknown;
  stock_applied_count?: unknown;
  settled_without_stock_count?: unknown;
  settled_unknown_count?: unknown;
}): string {
  const total = count(row.receipt_count);
  if (total === null) return "—";
  const applied = count(row.stock_applied_count) ?? 0;
  const settled = count(row.settled_without_stock_count) ?? 0;
  const unknown = count(row.settled_unknown_count) ?? 0;

  const parts: string[] = [];
  if (applied > 0) parts.push(`재고 반영 완료 ${applied}건`);
  if (settled > 0) parts.push(`반영할 재고 없음 ${settled}건`);
  if (unknown > 0) parts.push(`확인 못 함 ${unknown}건`);
  //  남은 건 = 전체 − 두 완료 − 모름. 음수가 나오면(계약이 어긋난 경우) 지어내지 않는다.
  const rest = total - applied - settled - unknown;
  if (rest > 0) parts.push(`처리 중 ${rest}건`);
  return parts.length === 0 ? "—" : parts.join(" · ");
}

/** 검수 결과 묶음 → 사람 말. 섞여 있으면 **섞여 있다고 그대로 보여 준다.** */
export function verdictText(verdicts: unknown, unknownCount: unknown): string {
  const list = Array.isArray(verdicts) ? verdicts : [];
  const known = list.map((value) => label(INSPECTION_VERDICT_LABEL, value)).filter((v) => v !== "—");
  const pending = Number(unknownCount);
  if (Number.isFinite(pending) && pending > 0) known.push(`검수 전 ${pending}건`);
  return known.length === 0 ? "—" : known.join(" · ");
}
