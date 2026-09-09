/**
 * Korean → English display labels for the ML console.
 *
 * ★ **The backend still speaks Korean.** Verdicts (`정상`/`주의`/`이상`), verify
 *   verdicts and graph prompts are produced by our Python agents, and the same
 *   strings are archived in report files and read by our Korean console at
 *   `localhost:3100`. Translating them at the source would rewrite history and
 *   break that screen, so the translation lives **here, at the display layer**.
 *
 * ★ **Never compare against the English text.** Branching logic must keep
 *   testing the Korean value the backend actually sends — the English string is
 *   for human eyes only. An unknown value falls through unchanged rather than
 *   disappearing: a label we forgot should look odd, not empty.
 */

/** Agent verdict: how bad is it. */
export const VERDICT: Record<string, string> = {
  정상: "정상",
  주의: "주의",
  이상: "이상",
};

/** Price series we forecast. */
export const KIND: Record<string, string> = {
  auc: "경락가",
  whsl: "중도매가",
  rtl: "소매가",
};

/** The three crops we forecast. Korean names are the API keys. */
export const ITEM: Record<string, string> = {
  배추: "배추",
  무: "무",
  양파: "양파",
};

/** Retrain verify table — is the candidate model better than the live one. */
export const VERIFY: Record<string, string> = {
  "후보가 낫다": "후보 모델이 낫음",
  "후보가 나쁘다": "후보 모델이 못함",
  "판정 불가": "알 수 없음",
  "표본 부족": "데이터 부족",
};

/** Retrain state machine — which step is running. */
export const NODE: Record<string, string> = {
  judge: "판단 중",
  ask_build: "후보 모델을 만들까요?",
  build: "후보 만드는 중",
  verify: "성능 비교 중",
  ask_apply: "새 모델로 바꿀까요?",
  apply: "교체하는 중",
};

/** The two questions the state machine asks a human. */
export const ASK: Record<string, string> = {
  "후보를 만들까요": "후보 모델을 만들까요?",
  "바꿀까요": "새 모델로 바꿀까요?",
};

/** Batch run status. */
export const RUN_STATE: Record<string, string> = {
  ok: "성공",
  success: "성공",
  fail: "실패",
  failed: "실패",
  running: "진행 중",
};

/** Report kind, as it appears in the saved-report history. */
export const REPORT_KIND: Record<string, string> = {
  claude_check: "일일 점검",
  데이터품질: "데이터 이상",
  배치장애조사: "자동작업 실패",
  드리프트감지: "예측 밀림",
  뉴스요약: "뉴스 요약",
  수집검사: "수집 누락",
  재학습판정: "재학습 판단",
  재학습검증: "새 모델 검증",
  예측설명: "예측 이유",
};

/** Translate, or hand back what came in. An unknown value must stay visible. */
export const en = (table: Record<string, string>, value: string | null | undefined) =>
  value == null ? "" : (table[value] ?? value);

/**
 * A backend prompt matched loosely — the state machine's `ask` text carries
 * extra words around the question, so an exact lookup misses.
 */
export function askEn(ask: string | null | undefined): string {
  if (!ask) return "";
  for (const [kr, text] of Object.entries(ASK)) if (ask.includes(kr)) return text;
  return ask;
}
