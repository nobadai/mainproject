"use client";

/**
 * 판매안의 **상태를 어떻게 말할 것인가.**
 *
 * 프로젝트 규율대로 축을 나눈다. 한 배지에 섞으면 읽는 사람이 어느 축의 이야기인지
 * 되짚을 수 없다.
 *
 * ```text
 * 제시 상태   이 안이 사용자 앞에서 서는 자리      판매 상태 + 재무 판정을 함께 읽은 결과
 * 재무 판정   재무가 내린 업무 판정                PASS · REVIEW_REQUIRED · FAIL · 없음
 * 전략        무엇이 자세를 골랐고 모델이 어땠나   LLM · 규칙 · 실패 사유
 * ```
 *
 * 🔴 **미판정을 «탈락» 이라고 부르지 않는다.** 아무도 탈락시키지 않았는데 탈락이라고
 *    적으면, 사용자는 재무 자료를 채워야 할 날에 판매 조건을 바꾼다 — 그러면 같은
 *    자리에서 또 막힌다.
 */

import type { SalesStrategyView } from "./sales_api";

type Tone = "good" | "warn" | "bad" | "neutral";

const PRESENTATION: Record<string, { text: string; tone: Tone; help: string }> = {
  PRESENTABLE: {
    text: "제시 가능",
    tone: "good",
    help: "재무 판정을 받아 통과했습니다. 판매 확정으로 진행할 수 있습니다.",
  },
  REVIEW_REQUIRED: {
    text: "검토 필요",
    tone: "warn",
    help: "재무가 «확인 필요» 로 판정했습니다. 사람이 확인하기 전에는 확정할 수 없습니다.",
  },
  UNRESOLVED: {
    text: "판정 대기",
    tone: "neutral",
    //  🔴 이 문장이 «탈락» 과 달라야 하는 이유가 여기 다 있다.
    help: "아직 판정을 받지 못한 안입니다. 탈락한 것이 아니라 필요한 자료가 아직 없습니다.",
  },
  REJECTED: {
    text: "확정 불가",
    tone: "bad",
    help: "판정을 받았고 현재 조건으로는 진행할 수 없습니다. 조건을 바꿔야 합니다.",
  },
};

export function presentationText(state: string): string {
  return PRESENTATION[state]?.text ?? state;
}

export function presentationHelp(state: string): string {
  return PRESENTATION[state]?.help ?? "";
}

export function presentationColor(state: string): string {
  const tone = PRESENTATION[state]?.tone ?? "neutral";
  return tone === "neutral" ? "var(--color-mut2)" : `var(--color-t-${tone})`;
}

/** 화면 전체가 어떤 상태인가. **넷을 한 문구로 합치지 않는다.** */
const SCREEN_STATE: Record<string, string> = {
  EMPTY: "오늘은 판매안이 만들어지지 않았습니다. 후보를 세울 자료가 부족했습니다.",
  UNRESOLVED:
    "판매안은 만들어졌지만 아직 판정을 받지 못했습니다. 탈락이 아니라 필요한 자료를 기다리는 중입니다.",
  REJECTED: "판정이 모두 끝났고, 현재 조건으로 진행할 수 있는 안이 없습니다.",
  PRESENTABLE: "판정을 통과한 판매안이 있습니다.",
};

export function screenStateText(state: string): string {
  return SCREEN_STATE[state] ?? "";
}

/**
 * 판정이 왜 안 났는가. 저장된 코드에 사용자 말을 붙인다.
 *
 * ★ 모르는 코드는 **지우지 않고** 그대로 남긴다 — 새 사유가 생긴 날 그 사실이 화면에서
 *   조용히 사라지면 안 된다.
 */
const UNRESOLVED_REASONS: Record<string, string> = {
  FINANCIAL_VALIDATION: "재무 검증 결과를 아직 받지 못했습니다.",
  FINANCIAL_VALIDATION_PENDING: "재무 검증 결과를 아직 받지 못했습니다.",
  SELLABLE_SUPPLY_CONTEXT: "판매 가능한 재고 정보를 아직 받지 못했습니다.",
  DELIVERY_FEASIBILITY_CONTEXT: "납품 가능 여부를 아직 받지 못했습니다.",
  ADDITIONAL_SUPPLY_CONTEXT: "추가 확보 가능 물량을 아직 받지 못했습니다.",
};

export function unresolvedReasonText(code: string): string {
  return UNRESOLVED_REASONS[code] ?? code;
}

/**
 * 전략 모델에 무슨 일이 있었나.
 *
 * 🔴 **`HTTP_400` 과 `HTTP_429` 를 같은 말로 적지 않는다.** 앞은 우리가 고칠 것이
 *    있다는 뜻이고 뒤는 기다리면 풀린다는 뜻이다 — 사유가 없던 동안 우리 스키마 버그가
 *    «모델이 실패했다» 뒤에 숨어 실환경에서 Planner 가 한 번도 안 돈 채로 지나갔다.
 */
const FAILURE_REASONS: Record<string, string> = {
  HTTP_400: "모델 요청 계약 오류 (우리 쪽에서 고칠 것이 있습니다)",
  HTTP_429: "모델 호출 제한 (잠시 뒤 다시 시도하면 풀립니다)",
  PROVIDER_UNREACHABLE: "모델 제공자에 연결하지 못했습니다",
  CONTRACT_VIOLATION: "모델이 약속한 형식 밖의 답을 냈습니다",
};

const LLM_STATUS: Record<string, string> = {
  SUCCESS: "모델이 자세를 정했습니다",
  FALLBACK: "모델이 실패해 규칙으로 정했습니다",
  SKIPPED_TEMPLATE: "모델을 부르지 않고 규칙으로 정했습니다",
  DISABLED: "모델 사용이 꺼져 있습니다",
};

const STRATEGY_SOURCE: Record<string, string> = {
  LLM: "모델이 고름",
  TEMPLATE_FALLBACK: "규칙 기반",
};

/**
 * 전략 상태를 사용자 문장 목록으로.
 *
 * 🔴 **저장된 라벨만 쓴다.** provider 응답 본문이나 HTTP 원문은 이 함수에 들어오지도
 *    않는다 — 그런 것이 화면에 실리면 키나 내부 주소가 사용자 브라우저로 나간다.
 */
export function strategyLines(strategy: SalesStrategyView | null): string[] {
  if (!strategy) return [];
  const lines: string[] = [];
  const source = strategy.source ? STRATEGY_SOURCE[strategy.source] ?? strategy.source : null;
  const status = strategy.llm_status ? LLM_STATUS[strategy.llm_status] ?? null : null;
  if (source) lines.push(`자세 결정: ${source}`);
  if (status) lines.push(status);
  if (strategy.llm_failure_reason) {
    lines.push(
      FAILURE_REASONS[strategy.llm_failure_reason] ?? `모델 실패 사유: ${strategy.llm_failure_reason}`,
    );
  }
  //  ★ **깎였다는 사실 자체를 남긴다.** 모델이 고른 자세를 사실이 내렸다는 뜻이다.
  if (strategy.clamped_reason_codes.length > 0) {
    lines.push("사실 기준에 맞춰 조건을 낮춘 항목이 있습니다.");
  }
  //  ★ 자세는 갈렸는데 숫자가 수렴한 경우. 세 안이 같아 보이는 이유가 여기 있다.
  if (strategy.collapsed) {
    lines.push("세 안의 조건이 같은 값으로 모였습니다 — 사실 기준이 같은 한도를 걸었습니다.");
  }
  return lines;
}
