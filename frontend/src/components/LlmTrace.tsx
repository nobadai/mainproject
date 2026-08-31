import type { AskResponse, Intent, IntentAction } from "@/lib/types";

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

const AGENT_LABEL: Record<string, string> = {
  finance: "재무",
  inventory: "물류",
  purchase: "매입",
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
  const parts = [ACTION_LABEL[intent.action] ?? intent.action];
  if (intent.item) parts.push(intent.item);
  if (intent.agents.length > 0)
    parts.push(intent.agents.map((a) => AGENT_LABEL[a] ?? a).join("·"));
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
    <div className="mt-2 border-t border-line-soft pt-2">
      <p className="m-0 mb-1 text-[10.5px] font-semibold uppercase tracking-[0.12em] text-faint">
        마스터가 알아들은 것
      </p>
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-[12.5px]">{summarize(trace.intent)}</span>
        <span
          className={`rounded px-1.5 py-px text-[11px] font-medium ${confidence.style}`}
        >
          {confidence.text}
        </span>
        {trace.llm_model && (
          <span
            title={trace.llm_provider ? `provider ${trace.llm_provider}` : undefined}
            className="rounded bg-sunk px-1.5 py-px font-mono text-[11px] text-muted"
          >
            {trace.llm_model}
          </span>
        )}
        {trace.llm_attempts > 1 && (
          <span className="rounded bg-sunk px-1.5 py-px font-mono text-[11px] text-muted">
            {trace.llm_attempts}회 시도
          </span>
        )}
      </div>

      {/* 🔴 모델이 낸 답과 규칙이 대신 낸 답은 다르다 — 같아 보이게 두지 않는다 */}
      {trace.llm_fallback_used && (
        <p className="m-0 mt-1.5 text-[11.5px] text-warn">
          🔴 모델이 쓸 수 있는 답을 못 줘 규칙이 대신 정했습니다 (
          {trace.llm_status})
        </p>
      )}
    </div>
  );
}
