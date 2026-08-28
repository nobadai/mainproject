export const n = (v: number) => Number(v).toLocaleString("ko-KR");
export const won = (v: number) => `${n(v)}원`;
export const pct = (v: number, d = 1) => `${(v * 100).toFixed(d)}%`;

/**
 * 상한에 바짝 붙으면 소수 한 자리는 `100.0%` 로 뭉개져 "딱 맞췄다"가 안 보인다.
 * 공격안이 19,999,650 / 20,000,000 이라 남은 여력이 350원인 게 요점이다.
 */
export const capPct = (ratio: number) => pct(ratio, ratio > 0.99 ? 3 : 1);

export const AXIS_KO: Record<string, string> = {
  quantity: "수량",
  timing: "시점",
  mix: "등급구성",
};

/** 화면에는 늘 셋을 그린다 — 만들지 않은 안도 자리를 남긴다. */
export const ALL_LABELS = ["보수", "기본", "공격"] as const;

/**
 * evidence_grade 는 **기본값(고정)에 배지를 안 붙인다.**
 * 모든 수치에 같은 배지를 달면 정보가 0인데 소음만 남는다 — 다를 때만 눈에 띈다.
 */
export const GRADE_KO: Record<string, { ko: string; why: string; dashed: boolean }> = {
  SIM_FIXED: { ko: "고정", why: "SIM_FIXED · 시뮬레이션 고정값", dashed: false },
  ASSUMED: { ko: "가정", why: "ASSUMED · 다른 값에서 파생한 가정값", dashed: true },
  OFFICIAL: { ko: "공식", why: "OFFICIAL · 공식 발간물", dashed: false },
};

/** 선행 데이터 미확정으로 검사를 미룬 고지인가 — 색이 아니라 라벨로 구분한다. */
export const isHold = (risk: string) => /보류|미확정/.test(risk);
