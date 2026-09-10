/**
 * 마스터 콘솔 서랍의 크기 — **사람이 정하고 브라우저가 기억한다.**
 *
 * ★ 예전에는 `min(58vh, 520px)` 로 **박혀 있었다.** 화면 주인이 대화를 넓게 보고
 *   싶어도 방법이 없었다. 이제 손잡이로 끌어 정하고, 그 값을 여기서 지킨다.
 *
 * ★ 모양은 `session.ts` · `demo_as_of.ts` 를 그대로 따랐다. `localStorage` 는
 *   **try/catch 로 감싼다** — 사생활 보호 창이나 저장 차단 설정에서는 읽는 것 자체가
 *   터진다. 못 읽으면 기본값으로 정상 동작한다. 새 관습을 만들지 않는다.
 *
 * ★ **저장된 값을 믿지 않는다.** 큰 화면에서 크게 잡아 둔 값이 작은 기기에서 그대로
 *   쓰이면 뒤 화면을 통째로 덮는다. 범위 밖이면 **기본값으로 되돌린다** — 잘라서
 *   맞추지 않는 이유는, 자르면 "왜 하필 이 높이인가" 를 아무도 설명할 수 없어서다.
 *
 * ★ 여기는 **수를 정하는 곳**이다. 화면을 그리는 일은 `MasterDock` 이 한다. 화면이
 *   쓰는 CSS 식도 여기서 만들어 내보낸다 — 같은 수가 두 파일에 적히지 않게.
 *
 * ★ **너비는 여기서 정하지 않는다.** 하루 동안 `wide` 라는 칸이 있었다 — 켜면 너비
 *   상한(820px)을 푸는 스위치였다. 화면 주인이 그것을 보고 **"그게 아니라 늘 꽉
 *   채워라"** 고 해서 상한 자체를 없앴고, 상한이 없으니 스위치도 할 일이 없어졌다.
 *   그래서 걷었다. 너비는 이제 화면이 CSS 로만 정한다 (`MasterDock`).
 *
 *   🔴 예전에 저장된 값에는 `{"height":617,"wide":true}` 처럼 그 칸이 남아 있다.
 *      **모르는 칸은 무시하고 높이만 살린다** — 모양이 안 맞는다고 통째로 버리면
 *      사람이 정해 둔 높이가 사라진다.
 */

const KEY = "haetdeul.dock_size";

/** 이보다 낮으면 대화가 안 보인다 — 되묻는 확인 한 덩이가 안 들어간다. */
export const MIN_HEIGHT = 200;

/**
 * 이보다 높으면 뒤 화면을 다 덮는다.
 *
 * ★ **세로 비율**이라 화면이 바뀌면 상한도 같이 바뀐다. 화면에서는 같은 비율을
 *   `min(…px, 88vh)` 로 한 번 더 건다 — 창을 줄이는 동안에는 JS 가 안 돌기 때문이다.
 */
export const MAX_HEIGHT_RATIO = 0.88;

/** 지금까지 박혀 있던 값 그대로 — `min(58vh, 520px)`. 여기가 **기본값**이다. */
const DEFAULT_RATIO = 0.58;
const DEFAULT_CAP = 520;

/** 손잡이를 화살표로 한 번 눌렀을 때 움직이는 폭(px). */
export const KEY_STEP = 24;

/** 저장소를 아직 안 본 첫 렌더가 쓰는 식. 위 상수로 만든 **같은 식**이다. */
export const DEFAULT_HEIGHT_CSS = `min(${Math.round(DEFAULT_RATIO * 100)}vh, ${DEFAULT_CAP}px)`;

/** 화면이 거는 상한. 창을 줄이는 동안 JS 없이도 값이 따라 줄어든다. */
export const MAX_HEIGHT_CSS = `${Math.round(MAX_HEIGHT_RATIO * 100)}vh`;

export interface DockSize {
  /** 펼친 판의 높이(px). **사람이 정하는 것은 이것 하나다.** */
  height: number;
}

/** 아무것도 저장돼 있지 않을 때의 높이. 예전에 박혀 있던 식 그대로다. */
export function defaultHeight(viewport: number): number {
  return Math.round(Math.min(viewport * DEFAULT_RATIO, DEFAULT_CAP));
}

/** 이 화면에서 허용되는 최대 높이. **하한보다 작아지지 않게** 한 번 더 막는다. */
export function maxHeight(viewport: number): number {
  return Math.max(MIN_HEIGHT, Math.round(viewport * MAX_HEIGHT_RATIO));
}

/** 끌고 있는 동안 쓰는 값 — 손이 범위를 넘어가면 **경계에서 멈춘다.** */
export function clampHeight(height: number, viewport: number): number {
  return Math.min(Math.max(Math.round(height), MIN_HEIGHT), maxHeight(viewport));
}

/**
 * 저장소에서 읽은 값을 화면에 쓸 수 있는 값으로.
 *
 * 🔴 **범위 밖이면 자르지 않고 기본값으로 되돌린다** (모듈 머리말 참고).
 */
export function sanitizeHeight(value: unknown, viewport: number): number {
  if (typeof value !== "number" || !Number.isFinite(value)) return defaultHeight(viewport);
  if (value < MIN_HEIGHT || value > maxHeight(viewport)) return defaultHeight(viewport);
  return Math.round(value);
}

/** 기억해 둔 크기. 없거나 못 읽거나 범위 밖이면 **기본값**이다. */
export function readDockSize(viewport: number): DockSize {
  let raw: string | null = null;
  try {
    raw = window.localStorage.getItem(KEY);
  } catch {
    // 저장소를 못 읽는 환경(사생활 보호 모드 등)에서도 기본값으로 돈다
    return { height: defaultHeight(viewport) };
  }
  let parsed: unknown = null;
  try {
    parsed = raw ? JSON.parse(raw) : null;
  } catch {
    parsed = null;
  }
  // 🔴 **칸을 하나씩 집어 온다.** 예전 `wide` 처럼 지금 모르는 칸이 섞여 있어도
  //    높이는 그대로 살아난다 (모듈 머리말 참고).
  const record = (parsed ?? {}) as Partial<DockSize>;
  return { height: sanitizeHeight(record.height, viewport) };
}

export function writeDockSize(size: DockSize): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(size));
  } catch {
    /* 저장 못 해도 이번 세션은 메모리로 돈다 */
  }
}
