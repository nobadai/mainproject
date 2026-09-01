/**
 * 부서가 내는 **닫힌 집합** 어휘 — 화면이 한국어로 옮기는 자리.
 *
 * ★ **매입이 2026-08-31 에 직접 준 목록이다.** 셋 다 매입 스키마의 `Literal` 이라
 *   **새 값이 생기면 스키마가 먼저 바뀐다.** 그래서 여기에 없는 값이 오면 그것은
 *   *"매입이 어휘를 늘렸는데 화면이 모른다"* 는 뜻이고, 화면이 그 사실을 드러낸다.
 *
 * 🔴 **모르는 값을 추측해서 번역하지 않는다.** 원문 옆에 `미등록` 을 붙여 그대로
 *   보인다 — 백엔드 `answer.py` 가 모르는 판정 라벨에 쓰는 규칙과 같다. 화면이
 *   그럴듯한 한국어를 지어내면 어휘가 갈린 사실이 **영영 안 보인다.**
 */

/** 매입이 읽은 시장 상황. */
export const SITUATION_LABEL: Record<string, string> = {
  stable: "안정",
  uncertain: "불확실",
};

/** 매입 자신의 확신도. LLM 의도 확신도(`LlmTrace`)와 **다른 값이다** — 대소문자도 다르다. */
export const CONFIDENCE_LABEL: Record<string, string> = {
  high: "높음",
  medium: "보통",
  low: "낮음",
};

/** 매입이 연 조정 축. 부서 제안 축(`_DEPT_AXES`)과 이름은 겹치지만 주체가 다르다. */
export const AXIS_LABEL: Record<string, string> = {
  quantity: "수량",
  timing: "시점",
  mix: "구성",
};

/** 등록된 값이면 한국어, 아니면 **원문 + 미등록**. 빈 값은 `null` — 아무것도 안 그린다. */
export function vocab(table: Record<string, string>, value: unknown): string | null {
  if (typeof value !== "string" || value === "") return null;
  return table[value] ?? `${value} (미등록)`;
}
