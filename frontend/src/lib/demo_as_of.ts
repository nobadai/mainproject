/**
 * 🔴 **임시다. 2026-09-08 시연이 끝나면 지운다** (`#431`).
 *
 * 기준일은 화면이 정할 값이 아니다. 운영에서는 스케줄러가 `clock.today_in_seoul()` 로
 * 하루를 정하고 그 아래로는 `as_of` 가 **인자로만** 흐른다 (`app/master/clock.py` · `#422`).
 *
 * ★ 화면이 날짜를 고르면 **시간축의 주인이 둘**이 된다. 지금은 시연 때문에 그렇게
 *   두지만, 그 상태가 남으면 *"어느 날을 보고 있는가"* 를 두 곳이 각자 답하게 된다.
 *
 * 지우는 조건과 순서는 `#431` 에 있다.
 *
 * ── 모양은 `session.ts` 를 그대로 베꼈다 ────────────────────────────────
 *
 * `localStorage` 는 React 바깥의 저장소라 **`useSyncExternalStore` 로 읽는다.**
 * 새 관습을 만들지 않는다 — 읽는 쪽 코드가 세션과 같은 모양이면 지울 때도 같이 지운다.
 */

const KEY = "haetdeul.demo_as_of";

/**
 * 기본 기준일.
 *
 * ★ **`2026-01-20`.** 실측으로 그날 `E1_APPROVED` 가 나고 승인 → 원장 → 입고 →
 *   실물재고 사슬이 이미 이어져 있다. 시연은 그 사슬이 보이는 날에서 시작한다.
 *
 * 🔴 예전에는 이 값이 **두 곳에서 갈려 있었다** — `lib/api.ts` 는 `2025-12-31`,
 *    `components/console/useTab.ts` 는 `2026-01-06`. 같은 화면에서 마스터는 한 날로
 *    판단하고 탭 여섯은 다른 날을 보여 주고 있었다. **여기 하나로 모은다.**
 */
export const DEFAULT_AS_OF = process.env.NEXT_PUBLIC_AS_OF ?? "2026-01-20";

const listeners = new Set<() => void>();

function notify(): void {
  for (const listener of listeners) listener();
}

export function subscribeAsOf(listener: () => void): () => void {
  listeners.add(listener);
  // 다른 탭에서 날짜를 바꾸면 이 탭도 따라간다
  window.addEventListener("storage", notify);
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) window.removeEventListener("storage", notify);
  };
}

/**
 * 지금 고른 기준일.
 *
 * ★ 값이 **문자열**이라 스냅샷을 캐시할 필요가 없다. `useSyncExternalStore` 가
 *   같다고 보는 조건은 `Object.is` 이고, 같은 문자열은 같다.
 */
export function asOfSnapshot(): string {
  try {
    return window.localStorage.getItem(KEY) ?? DEFAULT_AS_OF;
  } catch {
    // 저장소를 못 읽는 환경(사생활 보호 모드 등)에서도 기본값으로 돈다
    return DEFAULT_AS_OF;
  }
}

/** 서버 렌더에는 저장소가 없다. **항상 같은 값을 돌려줘야** 하이드레이션이 안 어긋난다. */
export function serverAsOf(): string {
  return DEFAULT_AS_OF;
}

export function setDemoAsOf(asOf: string): void {
  try {
    window.localStorage.setItem(KEY, asOf);
  } catch {
    /* 저장 못 해도 이번 세션은 메모리로 돈다 */
  }
  notify();
}
