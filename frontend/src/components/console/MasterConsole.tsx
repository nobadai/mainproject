"use client";

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";

import { Panel } from "@/components/Badges";
import { DecisionModal } from "@/components/DecisionModal";
import { ProcurementResult } from "@/components/ProcurementResult";
import { DomainReportPreview, ReportDownload } from "@/components/ReportDownload";
import { RunHistoryPanel } from "@/components/RunHistory";
import { ApprovedPlan } from "@/components/ApprovedPlan";
import { PurchaseRecordCard } from "@/components/PurchaseRecordCard";
import { LlmTrace } from "@/components/LlmTrace";
import { DayAdvance } from "@/components/console/DayAdvance";
import { DomainReadResult } from "@/components/console/DomainReadResult";
import { SalesConversation } from "@/components/console/SalesConversation";
import { Markdownish } from "@/components/console/ml/Markdownish";
import { ApiError, ask, execute } from "@/lib/api";
import { useSimRun } from "@/components/console/RunPicker";
//  🔴 시연용 기준일 (`#431`). 시연이 끝나면 이 줄을 지우고 `AS_OF` 로 되돌린다.
import { asOfSnapshot, serverAsOf, subscribeAsOf } from "@/lib/demo_as_of";
import { formatKoreanDate, userErrorText } from "@/lib/procurementLabels";
//  🔴 말로 한 승인이 **그날 서 있는 안**을 짚을 때 읽는다. 매입 화면과 **같은 조회**라
//     콘솔이 보는 안과 매입 탭이 보이는 안이 갈리지 않는다.
import { purchase } from "@/lib/screen";
import { CAN, type Session } from "@/lib/session";
import {
  isProcurement,
  type AskResponse,
  type DecisionOut,
  type DomainActionAnswer,
  type Intent,
  type ProcurementRunResponse,
  type Scenario,
} from "@/lib/types";

/**
 * 마스터에게 묻는 자리 — **화면 아래 서랍(dock) 안에 들어간다.**
 *
 * ★ 예전에는 이것이 화면 전체였습니다 (`/console` 한 장). 데모 기준으로
 *   바꾸면서 **주연과 조연이 바뀌었습니다** — 이제 표와 그래프가 화면을
 *   차지하고, 대화는 필요할 때 아래에서 꺼내 씁니다.
 *
 * ★ **한 줄도 안 버렸습니다.** `/ask` → 되묻기 → `/ask/execute` 로 이어지는
 *   흐름은 지금 mainproject 에서 실제로 도는 유일한 것이라, 자리만 옮겼습니다.
 *
 * ★ **2단계다.** `/ask` 가 `confirm_required` 로 되묻고, 사람이 누르면 `/ask/execute`
 *   가 돈다. 그때 **받은 `intent` 를 그대로 되돌려보낸다** — 서버는 재분류하지 않고,
 *   그래야 사용자가 확인한 것이 실행된다.
 *
 * ★ **값을 만들지 않는다.** 수량·금액·결론은 전부 서버가 정하고 화면은 그리기만 한다.
 */

type Turn =
  | { kind: "me"; text: string }
  //   `trace` 는 **①(의도 분류)가 무엇을 했는지**다. 되묻는 답에도 실어야 한다 —
  //   "못 알아들었습니다" 만 적으면 모델이 안 돈 것처럼 보인다.
  | {
      kind: "bot";
      text: string;
      trace?: LlmTraceData;
      note?: string | null;
      //   가격 예측이 쓴 본문. 있으면 문서로 그린다 — 다른 부서 답 모양은 그대로다.
      markdown?: string | null;
      //   가격 예측만 물었으면 규칙 머리글(`text`)은 본문과 겹치므로 감춘다.
      hideText?: boolean;
    }
  // 🔴 `done` 이 필요한 이유 — 누른 뒤에도 버튼이 살아 있으면 **같은 실행을 두 번**
  //    돌릴 수 있다. 실측에서 첫 매입 확인을 다시 눌러 같은 업무 키로 재실행됐고,
  //    그게 바로 `DECISION-COLLISION` 이 잡는 상황이다.
  | {
      kind: "confirm";
      text: string;
      intent: Intent;
      requestId: string;
      trace: LlmTraceData;
      //   `/ask` 에 보낸 말. 확인 뒤 가격 예측 조회가 이 원문으로 답한다.
      utterance: string;
      done?: boolean;
    }
  | { kind: "run"; run: ProcurementRunResponse }
  //   판매가 답한 조회. 요약 → 판매안 확인 → 카드 → 선택 → 최종 확인 → 확정까지 이 턴이 끈다.
  //   마스터가 만든 원문 답 · 실행 축 · 분류 흔적은 실제 서비스 화면에 싣지 않는다 (2026-09-15 결정).
  | {
      kind: "sales";
      asOf: string;
      detail: { text: string; note?: string | null; trace?: LlmTraceData };
    }
  //   승인 직후 "무엇을 하기로 한 것인가". **"오늘 산 것" 이 아니다** — 승인은
  //   기록이고 발주는 이 시스템 밖이다 (`ApprovedPlan` 이 그 사실을 적는다).
  //
  // 🔴 `scenario` 는 **있을 때만 온다.** 모달로 누른 승인은 안 전체를 들고 있지만,
  //    말로 한 승인은 라벨만 안다 — 그때 빈 안을 지어내 넘기면 화면이 0 과 빈 칸을
  //    «사실» 로 그린다. 없으면 `ApprovedPlan` 을 안 그리고 한 줄만 적는다.
  //    `item` 은 그 한 줄에 쓸 품목이다 (그날 서 있던 안의 품목 또는 분류가 읽은 품목).
  | { kind: "approved"; scenario?: Scenario; decision: DecisionOut; item?: string }
  | { kind: "domain"; result: DomainActionAnswer; trace?: LlmTraceData; note?: string | null }
  | { kind: "error"; text: string };

/**
 * 화면이 쥐고 있을 ① 분류 흔적. **응답에 이미 있던 것만 추린다** — 여기서 값을
 * 만들면 화면이 서버와 다른 이야기를 하게 된다.
 */
type LlmTraceData = Pick<
  AskResponse,
  "intent" | "llm_status" | "llm_provider" | "llm_model" | "llm_attempts" | "llm_fallback_used"
>;

/** Browser-only chat state. Keep only what is needed to redraw a conversation after refresh. */
const CHAT_STORAGE_KEY = "haetdeul.master.chat.v1";
type ReportPeriodState = "idle" | "awaiting" | "custom";
type PersistedTurn =
  | Extract<Turn, { kind: "me" | "bot" | "domain" | "sales" | "error" }>;

function persistedTurns(turns: Turn[]): PersistedTurn[] {
  return turns.flatMap((turn): PersistedTurn[] => {
    switch (turn.kind) {
      case "me":
      case "error":
        return [turn];
      case "bot":
        return [{ kind: "bot", text: turn.text, markdown: turn.markdown, hideText: turn.hideText }];
      case "domain":
        // `result.data` is the structured report/read payload required for a restored card.
        // Trace/provider details and the full ask response deliberately stay out of storage.
        return [{ kind: "domain", result: turn.result }];
      case "sales":
        return [{ kind: "sales", asOf: turn.asOf, detail: { text: turn.detail.text, note: turn.detail.note } }];
      default:
        // Confirmations and procurement execution state must be obtained live, not resumed from storage.
        return [];
    }
  });
}

function readChatSnapshot(): { turns: PersistedTurn[]; reportPeriod: ReportPeriodState; dateFrom: string; dateTo: string } | null {
  if (typeof window === "undefined") return null;
  try {
    const snapshot: unknown = JSON.parse(localStorage.getItem(CHAT_STORAGE_KEY) ?? "null");
    if (!snapshot || typeof snapshot !== "object") return null;
    const value = snapshot as { turns?: unknown; reportPeriod?: unknown; dateFrom?: unknown; dateTo?: unknown };
    const turns = Array.isArray(value.turns)
      ? value.turns.filter((turn): turn is PersistedTurn =>
          Boolean(turn && typeof turn === "object" && "kind" in turn && ["me", "bot", "domain", "sales", "error"].includes(String((turn as { kind?: unknown }).kind))),
        )
      : [];
    const reportPeriod: ReportPeriodState = value.reportPeriod === "awaiting" || value.reportPeriod === "custom"
      ? value.reportPeriod
      : "idle";
    return {
      turns,
      reportPeriod,
      dateFrom: typeof value.dateFrom === "string" ? value.dateFrom : "",
      dateTo: typeof value.dateTo === "string" ? value.dateTo : "",
    };
  } catch {
    return null;
  }
}

/**
 * 판매가 답했는가. **구조화된 조회 답(`status.answers.sales`)으로 가른다** — 문장을 긁지 않는다.
 *
 * ⚠️ 화면 타입의 부서 목록(`AgentName`)에 판매가 아직 없다. 공용 계약이라 넓히지 않고 여기서 읽는다.
 */
function salesAnswered(intent: Intent | undefined, status: unknown): boolean {
  const answers = (status as { answers?: Record<string, unknown> } | null | undefined)?.answers;
  const agents = (intent?.agents ?? []) as readonly string[];
  return Boolean(agents.includes("sales") && answers && "sales" in answers);
}

/**
 * 가격 예측 본문이 있는 답의 화면 칸. **구조화된 `answer.markdown` 으로 가른다** — 문장을 긁지 않는다.
 *
 * ⚠️ 화면 타입의 부서 목록(`AgentName`)에 가격 예측이 없다. 판매와 같은 이유로 넓히지 않고 여기서 읽는다.
 */
function mlParts(intent: Intent | undefined, answer: AskResponse["answer"] | undefined) {
  const markdown = answer?.markdown ?? null;
  const agents = (intent?.agents ?? []) as readonly string[];
  return { markdown, hideText: Boolean(markdown) && agents.every((a) => a === "ml") };
}

/**
 * **분류가 못 돌았는가.** 「못 알아들었다」와 갈라야 하는 것이 이것이다.
 *
 * 🔴 두 상태가 지금까지 한 화면이었다 (2026-09-16 실측 — 공용 키가 분당 한도에 걸려
 *    5개 중 4개가 막혔다). 분류기가 못 돈 것인데 화면은 되묻는 문장만 보여 줘서,
 *    사람이 **자기 말이 이상한 줄 알고 말을 바꿨다.** 말은 멀쩡했다.
 *
 *    llm_status="SUCCESS" + action="UNKNOWN"   진짜 못 알아들었다 → 말을 바꾸면 된다
 *    llm_status="FALLBACK"                     분류기가 못 돌았다 → 말을 바꿔도 소용없다
 *
 * ⚠️ `DISABLED`(일부러 끈 것)는 여기 안 걸린다 — 서버가 그때 `fallback=False` 로 낸다
 *    (`app/master/llm/runtime.py`). 끈 배포의 되묻기는 지금 동작이 맞다.
 */
function classifyFailed(res: Pick<AskResponse, "llm_status" | "llm_fallback_used">): boolean {
  return res.llm_status === "FALLBACK" || res.llm_fallback_used === true;
}

/**
 * 분류가 못 돌았을 때 화면에 내는 문장. **마지막 줄이 요점이다** — 사람이 말을
 * 바꾸지 않게 해야 한다.
 */
const CLASSIFY_FAILED_TEXT =
  "말을 알아듣는 기능이 잠시 멈췄습니다. 몇 초 뒤 다시 눌러 주세요.\n" +
  "— 입력하신 말은 문제가 없습니다.";

/**
 * 되묻는 자리에 실제로 적을 문장. **`clarification` 을 그대로 쓰던 자리는 전부 여기를 지난다.**
 *
 * ★ 문구를 두 곳에 적지 않는다 — 이 함수 하나가 주인이고 부르는 쪽은 그대로 쓴다.
 */
function clarificationText(
  res: Pick<AskResponse, "llm_status" | "llm_fallback_used" | "clarification">,
  fallback: string,
): string {
  if (classifyFailed(res)) return CLASSIFY_FAILED_TEXT;
  return res.clarification ?? fallback;
}

/** 분류가 못 돈 뒤 보내기를 더 잠가 두는 시간(초). **자동 재시도는 없다 — 사람이 누른다.** */
const FALLBACK_COOLDOWN_SEC = 5;

/**
 * 바닥에서 이만큼 안이면 «바닥을 보고 있다» 로 본다(px).
 *
 * **이 값은 알림 버튼에만 쓴다.** 스크롤을 움직일지 말지는 여기서 안 정한다 —
 * 판은 «내 글» 일 때만 내려간다.
 *
 * 딱 0 으로 두면 안 된다 — 한 줄 반쯤 남은 자리, 소수점 높이, 확대 배율 때문에
 * 바닥까지 내려도 1~2px 이 남는 일이 흔하다. 그러면 바닥인데도 버튼이 뜬다.
 */
const STICK_PX = 40;

function traceOf(res: AskResponse): LlmTraceData {
  return {
    intent: res.intent,
    llm_status: res.llm_status,
    llm_provider: res.llm_provider,
    llm_model: res.llm_model,
    llm_attempts: res.llm_attempts,
    llm_fallback_used: res.llm_fallback_used,
  };
}

/**
 * 매입안 이름을 가른 자리. 매입 API 가 `key = "{품목} · {안 이름}"` 으로 짓는다
 * (`app/api/purchase/query._plan` — 이 탭에 품목 축이 없어 이름 앞에 넣는다).
 *
 * 🔴 **맨 앞 하나만 가른다.** 안 이름에 같은 구분자가 들어와도 품목은 앞 한 칸이다.
 * ⚠️ 이 규칙이 바뀌면 여기가 조용히 빗나간다. 매입 스키마에 품목 칸이 서는 날
 *   이 둘을 그 칸 읽기로 바꾼다 — 서버 쪽 `app/api/plan_state.py` 가 같은 대기 중이다.
 */
const PLAN_KEY_SEP = " · ";

function planItem(key: string): string {
  const at = key.indexOf(PLAN_KEY_SEP);
  return at < 0 ? "" : key.slice(0, at);
}

function planLabel(key: string): string {
  const at = key.indexOf(PLAN_KEY_SEP);
  return at < 0 ? key : key.slice(at + PLAN_KEY_SEP.length);
}

const SHORTCUT: Record<string, string> = {
  purchase: "오늘 배추 얼마나 사야 해?",
  inventory: "창고에 얼마나 남았어?",
  finance: "지금 자금 상황 알려줘",
  sales: "판매 진행 상황 알려줘",
  ml: "내일 배추 경락가 얼마야?",
};

export function MasterConsole({ session }: { session: Session }) {
  //  🔴 시연용 기준일 (`#431`). `ask` · `execute` 가 실제로 싣는 값과 같은 곳을 읽는다
  //     — 머리에 적힌 날짜와 서버에 보내는 날짜가 갈리면 안 된다.
  const asOf = useSyncExternalStore(subscribeAsOf, asOfSnapshot, serverAsOf);
  const simRun = useSimRun();
  //  세션 판정(하이드레이션 · 로그인 리다이렉트)은 **셸이 이미 했다**
  //  (`app/console/layout.tsx`). 여기까지 왔으면 사람이 있다.
  const [tab, setTab] = useState<"master" | "runs">("master");
  const [chatSnapshot] = useState(() => readChatSnapshot());
  const [turns, setTurns] = useState<Turn[]>(() => chatSnapshot?.turns ?? []);
  const [reportPeriod, setReportPeriod] = useState<ReportPeriodState>(() => chatSnapshot?.reportPeriod ?? "idle");
  const [reportDates, setReportDates] = useState(() => ({ from: chatSnapshot?.dateFrom ?? "", to: chatSnapshot?.dateTo ?? "" }));
  const [resetOpen, setResetOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  // 🔴 분류가 못 돈 뒤 남은 잠금 시간(초). **연타가 한도를 더 깎는다** — 실측에서
  //    5개를 연달아 쏘니 4개가 막혔고, 8초씩 띄우니 4개 다 됐다 (2026-09-16).
  //    **자동으로 다시 쏘지 않는다.** 기다렸다 사람이 누른다.
  const [cooldown, setCooldown] = useState(0);

  // 승인 모달 — 어느 실행의 어느 안인지 함께 들고 있어야 한다
  const [picked, setPicked] = useState<{
    scenario: Scenario;
    requestId: string;
    historyRunId: string | null;
  } | null>(null);
  // 🔴 재요청·승인은 **어느 실행에 대한 것인지**를 화면이 실어야 한다 — 발화문엔 없다.
  //
  //    업무 키만으로는 부족하다. 한 키에 실행이 여러 행이라(실측 75행) 그 사이
  //    재실행이 있으면 **본 것과 다른 안이 승인된 것으로 남는다.** 그래서 업무 키와
  //    실행 행 id 를 **짝으로** 들고 다닌다.
  const [runs, setRuns] = useState<
    { requestId: string; historyRunId: string | null }[]
  >([]);
  const runIds = runs.map((r) => r.requestId);
  const last = runs.at(-1) ?? null;

  function rememberRun(run: { request_id: string; history_run_id: string | null }) {
    setRuns((prev) =>
      prev.some((r) => r.requestId === run.request_id)
        ? prev
        : [...prev, { requestId: run.request_id, historyRunId: run.history_run_id }],
    );
  }
  const [modalBusy, setModalBusy] = useState(false);
  const [modalError, setModalError] = useState<string | null>(null);

  const tail = useRef<HTMLDivElement>(null);
  const composer = useRef<HTMLInputElement>(null);

  /* ── 자기가 보낸 글에만 내려간다 ──────────────────────────────────────
   *
   * 예전엔 `turns` 가 늘 때마다 **무조건** 바닥으로 내려갔다. 위로 올려 지난 답을
   * 읽는 중에 새 글이 하나만 붙어도 읽던 자리가 끌려 내려갔다.
   *
   * **규칙은 하나뿐이다 — 내려가는 것은 `kind === "me"` 일 때뿐이다.**
   * 답이 도착했을 때는 **어디에 있든 판을 움직이지 않는다.** 바닥 근처면 따라
   * 내려가던 규칙은 없앴다 (2026-09-17 지시).
   *
   * ★ 새 글은 **아래에 붙는다.** 그러면 브라우저가 `scrollTop` 을 그대로 두므로
   *   위에 보이던 내용은 한 픽셀도 안 움직인다 — **아무것도 안 하는 것**이
   *   자리를 지키는 것이다. 그래서 `scrollTop` 보정을 따로 두지 않는다.
   *
   * 알림 버튼만 «바닥에서 얼마나 떨어졌나» 를 본다. 🔴 효과가 도는 시점은 새 글이
   * **이미 붙은 뒤**라 그대로 재면 늘어난 높이만큼 부풀려진다. 그래서 직전 높이
   * (`seenHeight`)를 들고 있다가 **붙기 전의 틈**을 되살려 잰다.
   * ------------------------------------------------------------------ */

  const scroller = useRef<HTMLDivElement>(null);
  /** 마지막으로 본 판의 전체 높이. 새 글이 붙기 «전» 의 틈을 되살리는 데 쓴다. */
  const seenHeight = useRef(0);
  const [unread, setUnread] = useState(false);

  function onScroll() {
    const el = scroller.current;
    if (!el) return;
    seenHeight.current = el.scrollHeight;
    //  바닥까지 내려왔으면 알릴 것이 없다 — 버튼은 스스로 사라진다
    if (el.scrollHeight - el.scrollTop - el.clientHeight <= STICK_PX) setUnread(false);
  }

  /**
   * 판을 바닥으로 민다.
   *
   * 🔴 **상태를 안 건드린다.** 효과 안에서 부르는 자리라, 여기서 `setUnread` 를
   *    하면 `react-hooks/set-state-in-effect` 에 걸린다. 버튼은 아래 `onScroll` 이
   *    바닥에 닿는 순간 스스로 지운다.
   */
  const scrollToTail = useCallback(() => {
    tail.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, []);

  useEffect(() => {
    const el = scroller.current;
    if (turns.length === 0 || !el) return;

    //  새 글이 붙기 **전** 의 바닥까지 거리. `scrollTop` 은 아래에 붙는 동안
    //  안 바뀌므로, 직전 높이로 재면 붙기 전의 틈이 그대로 나온다.
    const gapBefore = seenHeight.current - el.scrollTop - el.clientHeight;
    seenHeight.current = el.scrollHeight;

    //  ① 내가 보낸 글이면 바닥으로. ② 그 밖에는 **판을 건드리지 않는다** —
    //     바닥에서 멀면 «새 메시지» 만 알린다.
    if (turns.at(-1)?.kind === "me") scrollToTail();
    else if (gapBefore > STICK_PX) setUnread(true);
  }, [turns, scrollToTail]);

  useEffect(() => {
    localStorage.setItem(CHAT_STORAGE_KEY, JSON.stringify({
      turns: persistedTurns(turns),
      reportPeriod,
      dateFrom: reportDates.from,
      dateTo: reportDates.to,
    }));
  }, [turns, reportDates, reportPeriod]);

  //  남은 초를 1초씩 깎는다. 0 이 되면 잠금이 풀린다 — 여기서 다시 보내지 않는다.
  useEffect(() => {
    if (cooldown <= 0) return;
    const timer = setTimeout(() => setCooldown((n) => (n > 0 ? n - 1 : 0)), 1000);
    return () => clearTimeout(timer);
  }, [cooldown]);

  //  보내기가 막혀 있나. 요청이 도는 동안과 분류 실패 뒤 몇 초. **Enter 도 이것을 본다.**
  const locked = busy || cooldown > 0;

  const can = CAN[session.role];

  function push(...items: Turn[]) {
    setTurns((prev) => [...prev, ...items]);
  }

  /**
   * 실패를 화면에 적는다. **서버가 준 사유를 삼키지 않는다.**
   *
   * 🔴 사람 말로 된 사유는 **그대로 보인다** (`userErrorText` 가 가른다) —
   *    「'초공격' 은 이 실행이 내놓은 안이 아니다. 제시된 안: 보수, 기본, 공격」 처럼
   *    **무엇을 고쳐야 하는지 알려주는 문장**이 사라지면 사람이 손 쓸 데가 없다.
   *
   * ★ 코드가 섞인 사유는 그대로 올리지 않는다 (「사람 말만」). 다만 그때도
   *   **「잠시 뒤 다시 시도해 주세요」 로 덮지 않는다** — 요청 자체가 틀린 것이라
   *   기다렸다 다시 눌러도 같다. 그 문구는 **연결이 끊겼거나 서버가 탈 났을 때**의 말이다.
   */
  function fail(error: unknown) {
    const status = error instanceof ApiError ? error.status : null;
    const wrongRequest = status !== null && status >= 400 && status < 500;
    push({
      kind: "error",
      text: userErrorText(
        status,
        error instanceof Error ? error.message : "",
        wrongRequest
          ? "요청을 처리하지 못했습니다 — 다시 눌러도 같습니다. 화면에서 직접 해 주세요."
          : "요청을 처리하지 못했습니다. 잠시 뒤 다시 시도해 주세요.",
      ),
    });
  }

  /** ① 발화문 분류. **확인이 필요하면 아무것도 실행하지 않는다.** */
  async function send(text: string, context?: { dateFrom?: string; dateTo?: string }) {
    const utterance = text.trim();
    if (!utterance || locked) return;
    setReportPeriod("idle");
    setReportDates({ from: "", to: "" });
    setDraft("");
    push({ kind: "me", text: utterance });
    setBusy(true);
    try {
      const res: AskResponse = await ask(utterance, { simRunId: simRun || undefined, dateFrom: context?.dateFrom, dateTo: context?.dateTo });
      //  분류가 못 돌았으면 연달아 누르지 못하게 몇 초 더 잠근다.
      if (classifyFailed(res)) setCooldown(FALLBACK_COOLDOWN_SEC);
      const slots = res.intent.slots;
      const awaitingReportPeriod =
        res.outcome === "NEEDS_CLARIFICATION" &&
        res.intent.domain_action === "FINANCE_REPORT_GENERATE" &&
        !context?.dateFrom &&
        !slots?.period &&
        !slots?.start_date &&
        !slots?.end_date;
      if (awaitingReportPeriod) {
        setReportPeriod("awaiting");
        push({ kind: "bot", text: clarificationText(res, "어느 기간의 재무 보고서를 생성할까요?") });
      } else if (res.confirm_required) {
        push({
          kind: "confirm",
          text: clarificationText(res, "진행할까요?"),
          intent: res.intent,
          requestId: res.request_id,
          trace: traceOf(res),
          utterance,
        });
      } else if (res.domain_result) {
        push({ kind: "domain", result: res.domain_result, trace: traceOf(res), note: res.note });
      } else if (res.answer && salesAnswered(res.intent, res.status)) {
        push({
          kind: "sales",
          asOf: String(res.as_of),
          detail: { text: res.answer.text, note: res.note, trace: traceOf(res) },
        });
      } else if (res.answer) {
        //   조회면 `note` 가 **어느 실행·기준일을 읽었나** 다. 답 아래에 같이 보인다.
        push({
          kind: "bot",
          text: res.answer.text,
          trace: traceOf(res),
          note: res.note,
          ...mlParts(res.intent, res.answer),
        });
      } else {
        //  🔴 여기가 분류 실패가 떨어지는 자리다 (`outcome="NEEDS_CLARIFICATION"`).
        //     되묻는 문장만 적으면 사람이 자기 말을 고치러 간다 — `clarificationText` 가 가른다.
        push({
          kind: "bot",
          text: clarificationText(res, res.note ?? "답을 받지 못했습니다."),
          trace: traceOf(res),
        });
      }
    } catch (error) {
      fail(error);
    } finally {
      setBusy(false);
    }
  }

  /**
   * 그날 매입 화면에 **서 있는 안** 중 말한 라벨의 안을 찾는다.
   *
   * 🔴 **라벨은 문자열 그대로 견준다.** 부분 일치나 비슷한 말 맞히기를 넣으면
   *    「보수」를 말했는데 「보수적」 안이 승인되는 날이 온다 — 승인은 되돌리기가
   *    기록으로 남는 일이라, 못 찾는 쪽이 낫다.
   *
   * 🔴 **못 찾거나 여럿이면 `null` 이고, 부르는 쪽은 실행하지 않는다.** 하나로 좁혀지지
   *    않은 채 보내면 서버가 고르게 되는데, 그건 사람이 확인한 것과 다를 수 있다.
   *
   * ★ 기준일은 화면 머리에 적힌 그 날이고(`asOf`), 실행 축은 `GET /api/purchase` 가
   *   다른 네 탭과 같은 자리에서 정한다 (`app/api/shown_run.py`). **화면이 축을
   *   새로 지어내지 않는다.**
   */
  async function standingRun(intent: Intent) {
    const label = (intent.scenario_label ?? "").trim();
    if (!label) {
      push({
        kind: "error",
        text: "어느 안을 말씀하시는지 찾지 못했습니다. 매입 화면에서 골라 주세요.",
      });
      return null;
    }

    const plans = (await purchase(asOf)).plans;
    const hits = plans.filter(
      (plan) =>
        plan.request_id !== null &&
        plan.state !== "반려" &&
        //  `key` 는 `"배추 · 기본"` 이라 라벨과 다르다. 안 이름은 라벨로만 견준다.
        planLabel(plan.key) === label &&
        (!intent.item || planItem(plan.key) === intent.item),
    );

    if (hits.length === 0) {
      push({
        kind: "error",
        text: `그날 매입안에서 '${label}' 을 찾지 못했습니다. 매입 화면에서 골라 주세요.`,
      });
      return null;
    }
    if (hits.length > 1) {
      push({
        kind: "error",
        text: `'${label}' 안이 여럿입니다 — 어느 품목인지 말씀해 주세요.`,
      });
      return null;
    }
    return {
      requestId: hits[0].request_id as string,
      historyRunId: hits[0].history_run_id,
      //   승인한 뒤 "무엇을 승인했나" 한 줄에 쓸 품목. 찾아 온 안의 이름에서 그대로 읽는다.
      item: planItem(hits[0].key),
    };
  }

  /** ② 확인한 의도를 실행한다. `intent` 를 **그대로** 돌려보낸다. */
  async function confirm(
    turn: Extract<Turn, { kind: "confirm" }>,
    index: number,
  ) {
    if (busy || !session || turn.done) return;
    const rerun = turn.intent.action === "RERUN_WITH_CONDITION";
    //   말로 한 승인. **모달로 누른 승인(`approve`)과 같은 셋을 실어야 한다** —
    //   빠뜨리면 서버가 422 로 거절하고, 눌러서 한 승인과 말로 한 승인이 갈린다.
    const select = turn.intent.action === "SELECT_SCENARIO";
    /**
     * 🔴 **발화문에 없어 화면이 실어야 하는 둘** — 어느 실행의 안인가(`target_*`)와
     *    누가 승인하는가(`decided_by`). `lib/api.ts` 의 「SELECT · RERUN 필수」가
     *    그것이고, 서버(`ask_service._record_selection`)도 없으면 거절한다.
     */
    const needsTarget = rerun || select;

    setBusy(true);
    try {
      //   이 대화에서 방금 만든 안이 있으면 **그것이 먼저다.**
      let target = last;
      //   승인 뒤 한 줄에 쓸 품목. 분류가 읽어 낸 것이 먼저 서고, 그날 서 있는 안을
      //   찾아오면 그 안의 품목으로 바뀐다. **둘 다 없으면 빈 채로 둔다** — 지어내지 않는다.
      let item = (turn.intent.item ?? "").trim();

      // 🔴 없으면 **그날 매입 화면에 서 있는 안**에서 라벨로 찾는다 (2026-09-16).
      //
      //    9/11 시연이 이 모양이다 — 걷기가 그날 안을 세워 두고, 사람이 콘솔을 새로
      //    열어 말로 고른다. 전에는 `last` 가 없어 여기서 멈췄다.
      //
      //    ★ 찾는 것은 **화면**이다. 서버(`/ask/execute`)는 대상이 없으면 422 를 내고
      //      추측하지 않는다 — 그 규칙은 그대로 산다.
      if (select && !target) {
        const found = await standingRun(turn.intent);
        //   못 찾았으면 위에서 사람 말로 적었다. **실행하지 않는다.**
        if (!found) return;
        target = { requestId: found.requestId, historyRunId: found.historyRunId };
        if (found.item) item = found.item;
      }

      // 🔴 그래도 대상이 없으면 **추측하지 않고 멈춘다.** 서버도 같은 이유로 422 다.
      if (needsTarget && !target) {
        push({
          kind: "error",
          text: rerun
            ? "다시 만들 대상이 없습니다 — 먼저 매입안을 한 번 만들어야 조건을 붙일 수 있습니다."
            : "어느 안을 말씀하시는지 찾지 못했습니다. 매입 화면에서 골라 주세요.",
        });
        return;
      }

      // 한 번 누른 확인은 닫는다 — 두 번 눌러 같은 실행이 두 번 도는 것을 막는다
      setTurns((prev) =>
        prev.map((t, i) => (i === index ? { ...t, done: true } : t)),
      );
      push({ kind: "me", text: "네" });
      const res = await execute({
        intent: turn.intent,
        requestId: turn.requestId,
        // 재요청·안 선택에만 싣는다 — 조회·매입 실행에는 대상 실행이 없다
        targetRequestId: needsTarget ? (target?.requestId ?? undefined) : undefined,
        targetHistoryRunId: needsTarget ? (target?.historyRunId ?? undefined) : undefined,
        decidedBy: needsTarget ? session.name : undefined,
        actor: session.name,
        utterance: turn.utterance,
      });

      if (isProcurement(res)) {
        rememberRun(res);
        push({ kind: "run", run: res });
      } else if (res.domain_result) {
        push({ kind: "domain", result: res.domain_result, note: res.note });
      } else if (res.run) {
        // 재요청 — 결정 기록과 **새로 나온 안**이 함께 온다
        rememberRun(res.run);
        push(
          { kind: "bot", text: res.answer?.text ?? "" },
          { kind: "run", run: res.run },
        );
      } else if (select && res.decision) {
        // 🔴 말로 한 승인도 **모달로 누른 승인과 같은 자리에서 끝난다** (`approve`).
        //    여기서 갈리면 어느 길로 승인했느냐에 따라 실매입을 적을 칸이 있고 없다 —
        //    9/11 시연은 말로 승인하고 실매입을 적는 것이 전부다.
        //
        //    ★ `res.decision` 이 없으면 승인이 안 된 것이라 아래 분기로 그냥 흐른다.
        if (res.answer) push({ kind: "bot", text: res.answer.text });
        push({ kind: "approved", decision: res.decision, item });
      } else if (res.answer && salesAnswered(turn.intent, (res as { status?: unknown }).status)) {
        push({ kind: "sales", asOf, detail: { text: res.answer.text, note: res.note } });
      } else if (res.answer) {
        push({
          kind: "bot",
          text: res.answer.text,
          note: res.note,
          ...mlParts(turn.intent, res.answer),
        });
      } else {
        push({
          kind: "bot",
          text: clarificationText(res, "실행했지만 답이 비었습니다."),
        });
      }
    } catch (error) {
      fail(error);
    } finally {
      setBusy(false);
    }
  }

  /** ③ 안 선택 — 발화문에 없는 둘(대상 실행·승인자)을 화면이 싣는다. */
  async function approve() {
    if (!picked || !session) return;
    setModalBusy(true);
    setModalError(null);
    try {
      const res = await execute({
        intent: {
          action: "SELECT_SCENARIO",
          agents: [],
          item: null,
          scenario_label: String(picked.scenario.label ?? ""),
          condition: null,
          confidence: "HIGH",
        },
        targetRequestId: picked.requestId,
        targetHistoryRunId: picked.historyRunId ?? undefined,
        decidedBy: session.name,
      });
      const scenario = picked.scenario;
      setPicked(null);
      if (!isProcurement(res) && res.answer)
        push({ kind: "bot", text: res.answer.text });
      // 화면은 방금 무엇을 승인했는지 안다 — 서버에 다시 묻지 않는다.
      if (!isProcurement(res) && res.decision)
        push({ kind: "approved", scenario, decision: res.decision });
    } catch (error) {
      // 한국어로만 된 서버 문장(「이미 승인됐다」 같은 말)은 그대로, 코드가 섞이면 사람 말로
      setModalError(
        userErrorText(
          error instanceof ApiError ? error.status : null,
          error instanceof Error ? error.message : "",
          "승인을 기록하지 못했습니다. 잠시 뒤 다시 시도해 주세요.",
        ),
      );
    } finally {
      setModalBusy(false);
    }
  }

  /** 지름길 — 부서 이름을 누르면 **같은 API 를 발화문 없이** 부른다. */
  function shortcut(key: string) {
    const canned = SHORTCUT[key];
    if (canned) {
      setTab("master");
      void send(canned);
    }
  }

  const isHistory = tab === "runs";

  return (
    /**
     * ★ `h-full` 이다. 예전엔 `h-screen` 이었는데, 이제 서랍 안이라 **서랍이
     *   정해 준 높이**를 채워야 한다. 화면 높이를 다시 잡으면 서랍 밖으로 넘친다.
     *
     * 아래 스크롤 영역의 `min-h-0` 은 그대로 둔다 — flex 아이템의 기본
     * `min-height: auto` 는 내용 높이라, 없으면 `flex-1` 이 내용보다 작아지지
     * 못해 `overflow-y-auto` 가 안 걸린다.
     */
    <div className="flex h-full min-h-0 flex-col">
      <header className="relative flex flex-wrap items-center gap-2 border-b border-line px-4 py-2.5">
        {(["master", "runs"] as const).map((k) => (
          <button
            key={k}
            type="button"
            onClick={() => setTab(k)}
            aria-pressed={tab === k}
            className={`rounded-md px-2.5 py-1 text-[15.5px] font-medium transition ${
              tab === k ? "bg-ink text-paper" : "text-muted hover:bg-sunk"
            }`}
          >
            {{ master: "묻기", runs: "실행 이력" }[k]}
          </button>
        ))}
        <span className="ml-1 hidden gap-1 sm:flex">
          {Object.keys(SHORTCUT).map((k) => (
            <button
              key={k}
              type="button"
              onClick={() => shortcut(k)}
              disabled={locked}
              className="rounded-md border border-line px-2 py-1 text-[15px] text-muted
                transition hover:bg-sunk disabled:opacity-40"
            >
              {
                { purchase: "오늘 매입", inventory: "재고", finance: "자금", sales: "판매", ml: "가격 전망" }[
                  k
                ]
              }
            </button>
          ))}
        </span>
        <span className="ml-auto text-[15px] text-faint">기준일 {formatKoreanDate(asOf)}</span>
        <button type="button" onClick={() => setResetOpen(true)} className="rounded-md border border-line px-2 py-1 text-[15px] text-muted transition hover:bg-sunk focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">대화 초기화</button>
        {resetOpen && <div role="dialog" aria-modal="true" aria-label="대화 초기화 확인" className="absolute right-4 top-12 z-20 rounded-lg border border-line bg-surface p-3 text-[16px] shadow-lg"><p className="m-0">현재 대화내용을 모두 초기화할까요?<br />이 작업은 되돌릴 수 없습니다.</p><div className="mt-3 flex justify-end gap-2"><button type="button" onClick={() => setResetOpen(false)} className="rounded border border-line px-2 py-1 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">취소</button><button type="button" onClick={() => { setTurns([]); setReportPeriod("idle"); setReportDates({ from: "", to: "" }); setDraft(""); localStorage.removeItem(CHAT_STORAGE_KEY); setResetOpen(false); }} className="rounded bg-accent px-2 py-1 text-white focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">초기화</button></div></div>}
      </header>

        {isHistory ? (
          <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
            <RunHistoryPanel known={runIds} />
          </div>
        ) : (
          <>
            {/* 🔴 하루를 넘기는 자리. **판 머리에 둔다** — 「하루는 이 순서로 돕니다」
                안내와 같은 자리에서 보이고, 대화가 쌓여도 안 밀린다 (그 안내는
                대화가 비었을 때만 있는 `Empty` 안에 있다).
                ★ 허용 목록에 없는 축이면 `DayAdvance` 가 스스로 아무것도 안 그린다. */}
            <DayAdvance asOf={asOf} simRunId={simRun} />
            {/* 안내 버튼이 구르는 판 위에 떠야 해서 `relative` 한 겹을 덧댄다.
                높이 규칙(`min-h-0 flex-1`)은 덧댄 겹과 안쪽 판이 그대로 이어받는다. */}
            <div className="relative flex min-h-0 flex-1 flex-col">
            <div
              ref={scroller}
              onScroll={onScroll}
              className="min-h-0 flex-1 space-y-3.5 overflow-y-auto px-4 py-4"
            >
              {turns.length === 0 && <Empty onPick={send} />}
              {reportPeriod !== "idle" && (
                <ReportControls
                  mode={reportPeriod}
                  onPreset={(preset) => void send(`${preset} 재무 보고서 만들어줘`)}
                  onChooseCustom={() => setReportPeriod("custom")}
                  dates={reportDates}
                  onDatesChange={setReportDates}
                  onCustomSubmit={(dateFrom, dateTo) => void send("재무 보고서 만들어줘", { dateFrom, dateTo })}
                />
              )}

              {turns.map((turn, i) => (
                <TurnView
                  key={i}
                  turn={turn}
                  index={i}
                  onConfirm={confirm}
                  busy={busy}
                >
                  {turn.kind === "sales" && (
                    <>
                      <SalesConversation asOf={turn.asOf} canApprove={can.approve} />
                    </>
                  )}
                  {turn.kind === "run" && (
                    <>
                    <ProcurementResult
                      run={turn.run}
                      onPick={(scenario) => {
                        setModalError(null);
                        setPicked({
                          scenario,
                          requestId: turn.run.request_id,
                          historyRunId: turn.run.history_run_id,
                        });
                      }}
                      onRerun={() => {
                        rememberRun(turn.run);
                        setDraft("예산 2천만원으로 낮춰서 다시 해줘");
                        composer.current?.focus();
                      }}
                    />
                    {/* 들고 나갈 수 있는 문서 — 안이 있든 없든 낸다.
                        **안이 없는 실행도 기록으로 남길 값이 있다** (왜 없는지가 담긴다). */}
                    <div className="mt-3">
                      <ReportDownload requestId={turn.run.request_id} />
                    </div>
                    </>
                  )}
                </TurnView>
              ))}

              {busy && (
                <p className="m-0 text-[17px] text-faint">
                  데이터를 확인하고 있습니다.
                </p>
              )}
              <div ref={tail} />
            </div>

            {/* 위에서 읽는 동안 새 글이 오면 여기서만 알린다 — 판은 안 움직인다. */}
            {unread && (
              <button
                type="button"
                onClick={() => {
                  setUnread(false);
                  scrollToTail();
                }}
                className="absolute bottom-3 left-1/2 -translate-x-1/2 rounded-full border border-line
                  bg-surface px-3 py-1 text-[16px] text-muted shadow-[0_6px_18px_-8px_rgba(21,26,22,.5)]
                  transition hover:border-accent hover:text-accent-ink"
              >
                새 메시지 ↓
              </button>
            )}
            </div>

            <div className="border-t border-line px-4 py-3">
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  void send(draft);
                }}
                className="flex items-center gap-2 rounded-xl border-[1.5px] border-accent bg-surface px-3.5 py-2.5"
              >
                <input
                  ref={composer}
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  placeholder="자금 현황, 판매안, 거래처, 보고서를 자연어로 물어보세요"
                  className="min-w-0 flex-1 bg-transparent text-[18.5px] outline-none placeholder:text-faint"
                />
                {/* 잠긴 동안 남은 초를 버튼이 적는다 — 왜 안 눌리는지 보여야 사람이 기다린다. */}
                <button
                  type="submit"
                  disabled={locked || !draft.trim()}
                  className="rounded-lg bg-accent px-4 py-1.5 text-[17.5px] font-semibold text-white disabled:opacity-45"
                >
                  {cooldown > 0 ? `${cooldown}초 뒤 다시` : "보내기"}
                </button>
              </form>
              <p className="m-0 mt-2 text-[15.5px] text-faint">
                장부를 바꾸는 요청은{" "}
                <b className="text-muted">확인을 한 번 더 받습니다</b>. 조회와 보고서는 바로
                돕니다.
              </p>
            </div>
          </>
        )}

      {picked && (
        <DecisionModal
          scenario={picked.scenario}
          targetRequestId={picked.requestId}
          decidedBy={session.name}
          busy={modalBusy}
          error={modalError}
          onConfirm={approve}
          onCancel={() => setPicked(null)}
        />
      )}
    </div>
  );
}

function TurnView({
  turn,
  index,
  onConfirm,
  busy,
  children,
}: {
  turn: Turn;
  index: number;
  onConfirm: (t: Extract<Turn, { kind: "confirm" }>, index: number) => void;
  busy: boolean;
  children?: React.ReactNode;
}) {
  if (turn.kind === "me")
    return (
      <div className="flex justify-end">
        <p className="m-0 max-w-[74%] rounded-xl rounded-br-sm bg-accent px-3.5 py-2 text-[18px] text-white">
          {turn.text}
        </p>
      </div>
    );

  if (turn.kind === "bot")
    return (
      <div className="max-w-[85%] rounded-xl rounded-bl-sm border border-line-soft bg-sunk px-3.5 py-2.5">
        {!turn.hideText && (
          <div className="whitespace-pre-wrap text-[18px] leading-relaxed">
            {turn.text}
          </div>
        )}
        {turn.markdown && (
          <div className={turn.hideText ? "text-[18px]" : "mt-2 text-[18px]"}>
            <Markdownish text={turn.markdown} />
          </div>
        )}
        {turn.trace && <LlmTrace trace={turn.trace} />}
      </div>
    );

  if (turn.kind === "domain") {
    return (
      <div className="max-w-[94%] rounded-xl border border-line bg-surface p-4">
        <div className="whitespace-pre-wrap text-[18px] leading-relaxed">{turn.result.text}</div>
        {turn.result.report_kind && (
          <div className="mt-3">
            <DomainReportPreview kind={turn.result.report_kind} facts={turn.result.data} />
          </div>
        )}
        {!turn.result.report_kind && <DomainReadResult result={turn.result} />}
        {turn.trace && <LlmTrace trace={turn.trace} />}
      </div>
    );
  }

  if (turn.kind === "approved") {
    //  말로 한 승인은 안 전체를 들고 있지 않다. **없는 숫자를 0 으로 그리지 않고**
    //  무엇을 승인했는지만 적는다 — 안의 값은 매입 화면에 그대로 서 있다.
    const label = turn.decision.scenario_label;
    const what = [(turn.item ?? "").trim(), label ? `'${label}' 안` : "고른 안"]
      .filter(Boolean)
      .join(" ");
    return (
      <div className="flex flex-col gap-3">
        {turn.scenario ? (
          <ApprovedPlan scenario={turn.scenario} decision={turn.decision} />
        ) : (
          <p className="m-0 rounded-xl border border-line bg-surface px-3.5 py-2.5 text-[18px]">
            {turn.decision.decided_by} 님이 {what}을 승인했습니다.
          </p>
        )}
        {/* 사람 승인 뒤 실제로 산 값을 적는 자리. 자동 승인 · 매입 승인이 아니면 카드가 스스로 숨는다. */}
        <PurchaseRecordCard requestId={turn.decision.request_id} />
      </div>
    );
  }

  if (turn.kind === "error")
    return (
      <Panel tone="attn" title="실행하지 못했습니다" items={[turn.text]} />
    );

  if (turn.kind === "confirm")
    return (
      <div className="max-w-[85%] rounded-xl rounded-bl-sm border border-line-soft bg-sunk px-3.5 py-2.5">
        <p className="m-0 text-[18px]">{turn.text}</p>
        <LlmTrace trace={turn.trace} />
        {turn.done ? (
          <p className="m-0 mt-2 text-[16.5px] text-faint">
            확인함 — 아래 결과를 보세요
          </p>
        ) : (
          <button
            type="button"
            onClick={() => onConfirm(turn, index)}
            disabled={busy}
            className="mt-2.5 rounded-lg bg-accent px-4 py-1.5 text-[17px] font-semibold text-white disabled:opacity-45"
          >
            네, 진행합니다
          </button>
        )}
      </div>
    );

  // kind === "run" · "sales" — 결과 카드는 부르는 쪽이 children 으로 넣는다
  return (
    <div className="rounded-xl border border-line bg-surface p-4">
      {children}
    </div>
  );
}

//: 하루가 도는 차례. **누르는 순서 그대로** 적는다 (2026-09-16).
//
//  ★ 시연에서 사람이 제일 자주 묻는 것이 «이제 뭘 눌러야 하나» 다. 화면이 그 답을
//    들고 있으면 진행하는 사람이 순서를 외우지 않아도 된다.
//  🔴 **여기에 없는 단계를 지어내지 않는다.** 네 자리 전부 사람이 실제로 누르는 것이다 —
//     승인과 실매입 기록은 매입 화면, 판매 승인은 판매 화면에서 한다.
const DAY_STEPS = [
  "하루 열기",
  "매입안 승인",
  "실제 매입가 기록",
  "판매안 승인",
  "하루 닫기",
];

function Empty({ onPick }: { onPick: (text: string) => void }) {
  //: 눌러서 바로 답이 나오는 말만 둔다. **순서가 뜻이다** — 잔액 같은 «점» 에서
  //  «흐름» 을 거쳐 «보고서» 로 간다 (재무 요청 2026-09-16).
  //
  //  🔴 되묻는 말은 넣지 않는다. 「여신 한도 알려줘」는 거래처를 되물어 한 번
  //     클릭으로 안 끝나고, 「오늘 확정된 판매」는 기준일이 어긋나면 빈손이다.
  const samples = [
    "현재 자금 상황 알려줘",
    "받을 돈 보여줘",
    "오늘 판매안 보여줘",
    "거래처 목록 보여줘",
    "이번 달 현금 흐름 보여줘",
    "이번 주 재무 보고서 만들어줘",
  ];
  return (
    <div className="rounded-xl border border-dashed border-line p-6">
      <p className="m-0 text-[18px] font-semibold">무엇을 도와드릴까요?</p>
      <p className="m-0 mt-1 text-[17px] text-muted">
        마스터가 알아듣고 필요한 부서를 부릅니다. 무엇을 확인했고 무엇을 못
        봤는지 함께 답합니다.
      </p>

      <div className="mt-4 rounded-lg border border-line bg-sunk p-3">
        <p className="m-0 text-[16px] font-semibold">하루는 이 순서로 돕니다</p>
        <ol className="m-0 mt-2 flex flex-wrap items-center gap-x-1 gap-y-1 p-0 text-[16px] text-muted">
          {DAY_STEPS.map((step, i) => (
            <li key={step} className="flex items-center gap-1 list-none">
              <span className="rounded bg-surface px-1.5 py-0.5 tabular-nums">
                {i + 1}
              </span>
              <span>{step}</span>
              {i < DAY_STEPS.length - 1 && (
                <span aria-hidden="true" className="px-1">
                  ›
                </span>
              )}
            </li>
          ))}
        </ol>
        <p className="m-0 mt-2 text-[16px] text-muted">
          질문과 질문 사이에 몇 초를 두십시오. 말을 알아듣는 기능이 잠시 멈추면
          말을 바꾸지 마시고 같은 말을 다시 눌러 주세요.
        </p>
      </div>

      <div className="mt-3 flex flex-wrap gap-2">
        {samples.map((s) => (
          <button
            key={s}
            type="button"
            onClick={() => onPick(s)}
            className="rounded-full border border-line bg-sunk px-3 py-1 text-[16px] text-muted hover:border-accent hover:text-accent-ink"
          >
            {s}
          </button>
        ))}
      </div>
    </div>
  );
}

function ReportControls({
  mode,
  onPreset,
  onChooseCustom,
  dates,
  onDatesChange,
  onCustomSubmit,
}: {
  mode: Exclude<ReportPeriodState, "idle">;
  onPreset: (preset: string) => void;
  onChooseCustom: () => void;
  dates: { from: string; to: string };
  onDatesChange: (dates: { from: string; to: string }) => void;
  onCustomSubmit: (dateFrom: string, dateTo: string) => void;
}) {
  const [error, setError] = useState("");

  function submit() {
    if (!dates.from || !dates.to) return setError("시작일과 종료일을 모두 선택해 주세요.");
    if (dates.from > dates.to) return setError("종료일은 시작일 이후여야 합니다.");
    setError("");
    onCustomSubmit(dates.from, dates.to);
  }

  if (mode === "custom") {
    return (
      <section className="mt-5 rounded-lg border border-line bg-sunk p-3" aria-label="재무 보고서 기간 직접 선택">
        <p className="m-0 text-[18px] font-semibold">보고 기간 직접 선택</p>
        <div className="mt-3 grid gap-2 sm:grid-cols-2">
          <label className="text-[16px] font-medium text-muted">시작일<input type="date" value={dates.from} onChange={(e) => onDatesChange({ ...dates, from: e.target.value })} className="mt-1 block w-full rounded border border-line bg-surface p-2 text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent" /></label>
          <label className="text-[16px] font-medium text-muted">종료일<input type="date" value={dates.to} onChange={(e) => onDatesChange({ ...dates, to: e.target.value })} className="mt-1 block w-full rounded border border-line bg-surface p-2 text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent" /></label>
        </div>
        {error && <p className="mb-0 mt-2 text-[16px] text-red-700" role="alert">{error}</p>}
        <button type="button" onClick={submit} className="mt-3 rounded bg-accent px-3 py-2 text-[16px] font-semibold text-white focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">이 기간으로 보고서 생성</button>
      </section>
    );
  }

  return (
    <section className="mt-5 rounded-lg border border-line bg-sunk p-3" aria-label="재무 보고서 기간 선택">
      <p className="m-0 text-[18px] font-semibold">어느 기간의 재무 보고서를 생성할까요?</p>
      <div className="mt-3 flex flex-wrap gap-2">
        {["최근 7일", "최근 30일", "최근 3개월", "최근 1년"].map((label) => (
          <button key={label} type="button" onClick={() => onPreset(label)} className="rounded border border-line bg-surface px-2 py-1 text-[16px] text-ink transition hover:border-accent focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">{label}</button>
        ))}
        <button type="button" onClick={onChooseCustom} className="rounded border border-line bg-surface px-2 py-1 text-[16px] text-ink transition hover:border-accent focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">직접 선택</button>
      </div>
    </section>
  );
}
