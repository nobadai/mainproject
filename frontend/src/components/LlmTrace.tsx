import type { AskResponse, DomainAction, Intent, IntentAction } from "@/lib/types";

/**
 * ① 의도 분류가 무엇을 했는지 — **화면이 안 보여주면 LLM 은 없는 것처럼 보인다.**
 *
 * 확인 게이트에는 되물을 말만 있어서 *"그냥 버튼 하나"* 로 읽힌다. 실제로는 그
 * 앞에 모델이 발화문을 읽고 **행동·품목·부서·확신도**를 정한다. 그걸 안 적으면
 * 사람은 화면이 규칙만으로 돈다고 생각한다.
 *
 * ★ **값을 만들지 않는다.** 전부 `/master/ask` 응답에 이미 실려 있던 것을 그대로
 *   적는다. 화면이 라벨만 입힌다.
 *
 * 🔴 **잘된 것만 적지 않는다.** 확신도가 낮아서 되묻는 것인데 확신도를 감추면
 *   *"왜 또 물어보나"* 로만 읽힌다. 재시도·규칙 대체도 같이 적는다 — 모델이
 *   답을 못 줘서 규칙이 대신한 답과, 모델이 낸 답은 **다른 것이다.**
 */

const ACTION_LABEL: Record<IntentAction, string> = {
  PROCUREMENT_RUN: "매입안 생성",
  STATUS_QUERY: "부서 상태 조회",
  RERUN_WITH_CONDITION: "조건 변경 재요청",
  SELECT_SCENARIO: "안 선택",
  DOMAIN_ACTION: "도메인 업무",
  // 🔴 **"못 알아들음" 이 아니다.** 마스터는 *"가격을 묻는구나"* 를 알아듣고
  //    *"그 자리는 없다"* 고 답한다. 그런데 라벨이 "못 알아들음" 이면 바로 아래
  //    문장과 **모순된다** — 읽는 사람은 "못 알아들었다면서 왜 가격 얘기를 하지" 가 된다.
  //
  //    `UNKNOWN` 은 두 경우를 덮는다 — 발화가 불분명한 것("그거 있잖아")과 알아들었지만
  //    할 수 있는 일이 아닌 것("배추 가격"). 확신도로는 못 가른다(둘 다 HIGH 로 온다).
  //    **둘 다 참인 말**을 쓴다 — 백엔드도 같은 어휘다:
  //    *"아직 안 만들었다" 가 아니라 "실행할 것이 없다" 다* (`ask_service.py`).
  UNKNOWN: "실행할 것 없음",
};

/** `DOMAIN_ACTION`은 운반용 최상위 의도다. 사람에게는 실제 업무를 보여 준다. */
const DOMAIN_ACTION_LABEL: Record<DomainAction, string> = {
  FINANCE_SUMMARY_GET: "자금 현황 조회",
  FINANCE_CASH_ADJUSTMENT_CREATE: "자금 변동 등록",
  FINANCE_CREDIT_LIMIT_GET: "여신 한도 조회",
  FINANCE_CREDIT_LIMIT_UPSERT: "여신 한도 변경",
  FINANCE_COLLECTION_CREATE: "수금 등록",
  FINANCE_EXPENSE_LIST: "비용 내역 조회",
  FINANCE_EXPENSE_CREATE: "비용 등록",
  FINANCE_EXPENSE_SETTLE: "비용 지급",
  FINANCE_EXPENSE_CANCEL: "비용 취소",
  FINANCE_CASHFLOW_GET: "자금 흐름 조회",
  FINANCE_RECEIVABLES_GET: "미수금 조회",
  FINANCE_PAYABLES_GET: "지급 예정 조회",
  FINANCE_REPORT_GENERATE: "재무 보고서 생성",
  SALES_PROPOSAL_CREATE: "판매 후보 생성",
  SALES_PROPOSALS_TODAY: "오늘 판매안 조회",
  SALES_CONFIRMED_TODAY: "확정 판매 조회",
  SALES_REPORT_GENERATE: "판매 보고서 생성",
  LOGISTICS_REPORT_GENERATE: "재고·물류 보고서 생성",
  PARTNER_LIST: "거래처 목록 조회",
  PARTNER_CREATE: "거래처 등록",
  PARTNER_DETAIL_GET: "거래처 상세 조회",
  PARTNER_UPDATE: "거래처 수정",
};

/** 부서 이름. **한 벌만 둔다** — 두 벌을 두면 언젠가 갈린다. */
export const AGENT_LABEL: Record<string, string> = {
  finance: "재무",
  inventory: "물류",
  ml: "가격 예측",
  purchase: "매입",
  sales: "판매",
};

/** 확신도는 **되묻는 이유 그 자체**라 등급마다 색을 다르게 준다. */
const CONFIDENCE = {
  HIGH: { text: "확신 높음", style: "bg-accent-wash text-accent-ink" },
  MEDIUM: { text: "확신 보통", style: "bg-sky-wash text-sky" },
  LOW: { text: "확신 낮음", style: "bg-gold-wash text-gold" },
} as const;

/**
 * 🔴 `UNKNOWN` 에는 같은 말을 쓸 수 없다. *"못 알아들음 · 확신 높음"* 은
 * **"못 알아들었는데 확신은 높다"** 로 읽힌다. 뜻은 그 반대다 — 모델이
 * **범위 밖인 것을 확실히 알아본 것**이다.
 *
 * 그리고 이 자리는 색이 반대다. 다른 action 에서 `HIGH` 는 좋은 소식이라
 * 강조색을 주지만, 여기서는 *"확실히 못 한다"* 라 강조할 것이 아니다.
 */
const UNKNOWN_CONFIDENCE = {
  HIGH: { text: "확실", style: "bg-sunk text-muted" },
  MEDIUM: { text: "아마도", style: "bg-sky-wash text-sky" },
  LOW: { text: "판단하지 못함", style: "bg-gold-wash text-gold" },
} as const;

function summarize(intent: Intent): string {
  // ⚠️ 화면 타입(`AgentName`)에 가격 예측이 없다. 공용 계약이라 넓히지 않고 여기서 읽는다.
  const agents = intent.agents as readonly string[];
  // ★ 가격 전망만 물은 것은 "부서 상태 조회" 가 아니다. 부서 이름을 뒤에 또 붙이지 않는다.
  const priceOnly =
    intent.action === "STATUS_QUERY" && agents.length === 1 && agents[0] === "ml";
  const domainLabel =
    intent.action === "DOMAIN_ACTION" && intent.domain_action
      ? DOMAIN_ACTION_LABEL[intent.domain_action]
      : null;
  const parts = [
    priceOnly ? "가격 전망 조회" : (domainLabel ?? ACTION_LABEL[intent.action] ?? intent.action),
  ];
  if (intent.item) parts.push(intent.item);
  // 🔴 표에 없는 부서 코드는 그 조각을 뺀다 — 코드 이름을 사람 화면에 내보내지 않는다.
  const names = priceOnly || domainLabel ? [] : agents.map((a) => AGENT_LABEL[a]).filter(Boolean);
  if (names.length > 0) parts.push(names.join("·"));
  if (intent.scenario_label) parts.push(`'${intent.scenario_label}'`);
  if (intent.condition) parts.push(`'${intent.condition}'`);
  return parts.join(" · ");
}

type Trace = Pick<
  AskResponse,
  "intent" | "llm_status" | "llm_provider" | "llm_model" | "llm_attempts" | "llm_fallback_used"
>;

export function LlmTrace({ trace }: { trace: Trace }) {
  const confidence =
    trace.intent.action === "UNKNOWN"
      ? UNKNOWN_CONFIDENCE[trace.intent.confidence]
      : CONFIDENCE[trace.intent.confidence];

  return (
    <details className="mt-2 border-t border-line-soft pt-2 text-[12px] text-muted">
      <summary className="cursor-pointer select-none text-faint marker:text-faint">
        요청 해석 보기
      </summary>
      <dl className="m-0 mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1.5">
        <dt className="text-faint">요청 해석</dt>
        <dd className="m-0 text-ink">{summarize(trace.intent)}</dd>
        {trace.intent.domain_action && (
          <>
            <dt className="text-faint">내부 동작</dt>
            <dd className="m-0 font-mono text-[11px] text-muted">{trace.intent.domain_action}</dd>
          </>
        )}
        <dt className="text-faint">인식 신뢰도</dt>
        <dd className="m-0">
          <span className={`rounded px-1.5 py-px text-[11px] font-medium ${confidence.style}`}>
            {confidence.text.replace("확신 ", "")}
          </span>
        </dd>
      </dl>

      {/* 개발/지원 시에는 남기되, 기본 결과 흐름을 방해하지 않는다. */}
      {trace.llm_fallback_used && (
        <p className="m-0 mt-2 text-[11.5px] text-warn">
          요청을 정확히 알아듣지 못해 기본 규칙으로 정했습니다
        </p>
      )}
    </details>
  );
}
