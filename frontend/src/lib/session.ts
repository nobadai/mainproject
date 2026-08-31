/**
 * 🔴 **진짜 인증이 아니다.**
 *
 * 백엔드에 인증이 없다. 결정 API 는 `decided_by` 를 **필수로 요구하지만 그 값을
 * 검증하지 않는다** — 화면이 아무 이름이나 보낼 수 있다.
 *
 * 계약에 *"승인자가 없는 승인은 승인이 아니다"* 라고 적어 놓고 정작 **그 사람이
 * 맞는지는 아무도 안 본다.** 토큰에서 `decided_by` 를 꺼내 쓰기 전까지, 승인 이력은
 * **"누군가 이 이름을 적었다"** 까지만 보증한다.
 *
 * 그래서 이 모듈은 **자리를 잡아 두는 것**이 목적이다. 서버 인증이 붙으면 이 파일만
 * 바뀌고 화면은 그대로다 — 승인 화면이 `session.name` 을 쓰는 구조는 같기 때문이다.
 */

const KEY = "haetdeul.session";

export type Role = "purchase" | "finance" | "approver";

export interface Session {
  employeeId: string;
  name: string;
  role: Role;
}

export const ROLE_LABEL: Record<Role, string> = {
  purchase: "매입 담당",
  finance: "재무 담당",
  approver: "승인권자",
};

/** 역할이 볼 수 있는 것. **화면에서만 가린다 — 서버는 누구든 부를 수 있다.** */
export const CAN: Record<Role, { finance: boolean; approve: boolean; procure: boolean }> = {
  purchase: { finance: false, approve: false, procure: true },
  finance: { finance: true, approve: false, procure: false },
  approver: { finance: true, approve: true, procure: true },
};

/* ── 외부 저장소 구독 ────────────────────────────────────────────────────
 *
 * `localStorage` 는 React 바깥의 저장소라 **`useSyncExternalStore` 로 읽는다.**
 * 효과 안에서 `setState` 로 끌어오면 렌더가 한 번 더 돌고, React 19 린트가 그걸
 * 잡는다 — 규칙을 끄는 것보다 맞는 API 를 쓰는 편이 낫다.
 *
 * ★ **스냅샷을 캐시한다.** `useSyncExternalStore` 는 스냅샷이 매번 새 객체면 무한
 *   루프로 본다. 원문(raw 문자열)이 그대로면 앞서 만든 객체를 돌려준다.
 * ------------------------------------------------------------------------ */

let cachedRaw: string | null = null;
let cachedSession: Session | null = null;
const listeners = new Set<() => void>();

function notify(): void {
  for (const listener of listeners) listener();
}

export function subscribeSession(listener: () => void): () => void {
  listeners.add(listener);
  // 다른 탭에서 로그아웃하면 이 탭도 따라 나가야 한다
  window.addEventListener("storage", notify);
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) window.removeEventListener("storage", notify);
  };
}

export function sessionSnapshot(): Session | null {
  const raw = (() => {
    try {
      return window.localStorage.getItem(KEY);
    } catch {
      return null;
    }
  })();
  if (raw === cachedRaw) return cachedSession;
  cachedRaw = raw;
  try {
    cachedSession = raw ? (JSON.parse(raw) as Session) : null;
  } catch {
    cachedSession = null;
  }
  return cachedSession;
}

/** 서버 렌더에는 저장소가 없다. **항상 같은 값을 돌려줘야** 하이드레이션이 안 어긋난다. */
export function serverSnapshot(): Session | null {
  return null;
}

export function saveSession(session: Session): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(session));
  } catch {
    /* 저장 못 해도 이번 세션은 메모리로 돈다 */
  }
  notify();
}

export function clearSession(): void {
  try {
    window.localStorage.removeItem(KEY);
  } catch {
    /* 무시 */
  }
  notify();
}
