/**
 * 실행 축(`sim_run_id`)을 화면이 들고 있는 자리.
 *
 * 🔴 **코드에 실행 이름을 박지 않는다.** `const SIM_RUN_ID = "SIM-BURNIN-202512"`
 *    같은 값이 한 줄 생기면, 화면은 사람이 무엇을 고르든 **늘 그 실행**을 보여 준다.
 *    그 사고는 오류 없이 조용하다 — 숫자가 나오고, 그 숫자가 남의 실행 것이다.
 *
 * ★ **기본값은 빈 값이다.** 실행이 안 정해졌으면 화면은 *"실행을 선택해 주세요"* 를
 *   띄우고 **API 를 부르지 않는다.** 아무 실행이나 골라 보여 주는 것보다 낫다.
 *
 * ⚠️ **실행 목록 API 가 아직 없다.** `sim_runs` 는 마스터 소유 표이고 그것을 나열하는
 *   read 계약이 저장소에 없다(2026-09-11 실측). 그래서 지금은 운영자가 값을 적고,
 *   `NEXT_PUBLIC_SIM_RUN_ID` 가 있으면 그것으로 시작한다 — **환경이 주는 설정이지
 *   코드가 정한 상수가 아니다.** 목록 API 가 생기면 이 파일이 그 값을 받아 고른다.
 *
 * ── 모양은 `demo_as_of.ts` 를 그대로 따른다 ──────────────────────────────
 *
 * `localStorage` 는 React 바깥의 저장소라 **`useSyncExternalStore` 로 읽는다.**
 * 새 관습을 만들지 않는다 — 읽는 쪽 코드가 같은 모양이면 지울 때도 같이 지운다.
 */

const KEY = "haetdeul.sim_run_id";

/** 환경이 주면 그 값으로 시작한다. 없으면 **빈 값** — 아무 실행도 고르지 않은 상태다. */
const SEED = process.env.NEXT_PUBLIC_SIM_RUN_ID ?? "";

let cached: string | null = null;
const listeners = new Set<() => void>();

export function simRunSnapshot(): string {
  if (cached !== null) return cached;
  try {
    cached = window.localStorage.getItem(KEY) ?? SEED;
  } catch {
    //  사생활 보호 모드처럼 저장소를 막아 둔 브라우저가 있다. 그때는 환경값으로 산다.
    cached = SEED;
  }
  return cached;
}

/** 서버 렌더에는 저장소가 없다. 씨앗만 보여 주고 값은 클라이언트에서 맞춘다. */
export function serverSimRun(): string {
  return SEED;
}

export function subscribeSimRun(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function setSimRun(value: string): void {
  const next = value.trim();
  cached = next;
  try {
    if (next) window.localStorage.setItem(KEY, next);
    else window.localStorage.removeItem(KEY);
  } catch {
    /* 저장은 못 해도 이번 세션은 돈다 */
  }
  listeners.forEach((listener) => listener());
}
