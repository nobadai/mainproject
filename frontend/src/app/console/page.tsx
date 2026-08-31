"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, useSyncExternalStore } from "react";

import { Panel } from "@/components/Badges";
import { DecisionModal } from "@/components/DecisionModal";
import { ProcurementResult } from "@/components/ProcurementResult";
import { RunHistoryPanel } from "@/components/RunHistory";
import { BurnInPanel } from "@/components/BurnInPanel";
import { LlmTrace } from "@/components/LlmTrace";
import { Sidebar } from "@/components/Sidebar";
import { ApiError, AS_OF, ask, execute } from "@/lib/api";
import {
  CAN,
  clearSession,
  serverSnapshot,
  sessionSnapshot,
  subscribeSession,
} from "@/lib/session";
import {
  isProcurement,
  type AskResponse,
  type Intent,
  type ProcurementRunResponse,
  type Scenario,
} from "@/lib/types";

/**
 * 콘솔 — 말로 묻고 말로 답받는 자리.
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
  | { kind: "bot"; text: string; trace?: LlmTraceData }
  // 🔴 `done` 이 필요한 이유 — 누른 뒤에도 버튼이 살아 있으면 **같은 실행을 두 번**
  //    돌릴 수 있다. 실측에서 첫 매입 확인을 다시 눌러 같은 업무 키로 재실행됐고,
  //    그게 바로 `DECISION-COLLISION` 이 잡는 상황이다.
  | {
      kind: "confirm";
      text: string;
      intent: Intent;
      requestId: string;
      trace: LlmTraceData;
      done?: boolean;
    }
  | { kind: "run"; run: ProcurementRunResponse }
  | { kind: "error"; text: string };

/**
 * 화면이 쥐고 있을 ① 분류 흔적. **응답에 이미 있던 것만 추린다** — 여기서 값을
 * 만들면 화면이 서버와 다른 이야기를 하게 된다.
 */
type LlmTraceData = Pick<
  AskResponse,
  "intent" | "llm_status" | "llm_provider" | "llm_model" | "llm_attempts" | "llm_fallback_used"
>;

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

const SHORTCUT: Record<string, string> = {
  purchase: "오늘 배추 얼마나 사야 해?",
  inventory: "창고에 얼마나 남았어?",
  finance: "지금 자금 상황 알려줘",
};

export default function ConsolePage() {
  const router = useRouter();
  // localStorage 는 React 바깥이라 구독해서 읽는다 (`session.ts` 주석 참조)
  const session = useSyncExternalStore(
    subscribeSession,
    sessionSnapshot,
    serverSnapshot,
  );

  // 🔴 **"아직 모른다" 와 "없다" 를 가른다.**
  //
  // 서버 렌더에는 저장소가 없어 첫 렌더의 세션이 늘 `null` 이다. 그걸 "없다" 로 읽고
  // 바로 로그인으로 보내면 **로그인한 사용자가 새로고침마다 튕긴다** (실측으로 잡음).
  // 이 값은 클라이언트에서만 `true` 라, 하이드레이션이 끝났는지를 알려 준다.
  const hydrated = useSyncExternalStore(
    subscribeSession,
    () => true,
    () => false,
  );
  const [tab, setTab] = useState("master");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);

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

  useEffect(() => {
    // 세션이 없으면 로그인으로. **효과가 하는 일은 외부(라우터) 갱신뿐이다.**
    // 하이드레이션 전에는 판단하지 않는다 — 아직 저장소를 못 읽은 상태다.
    if (hydrated && session === null) router.replace("/");
  }, [hydrated, session, router]);

  useEffect(() => {
    tail.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns]);

  if (!hydrated || !session) return null;
  const can = CAN[session.role];

  function push(...items: Turn[]) {
    setTurns((prev) => [...prev, ...items]);
  }

  function fail(error: unknown) {
    const message =
      error instanceof ApiError
        ? `[${error.status || "연결 실패"}] ${error.message}`
        : String(error);
    push({ kind: "error", text: message });
  }

  /** ① 발화문 분류. **확인이 필요하면 아무것도 실행하지 않는다.** */
  async function send(text: string) {
    const utterance = text.trim();
    if (!utterance || busy) return;
    setDraft("");
    push({ kind: "me", text: utterance });
    setBusy(true);
    try {
      const res: AskResponse = await ask(utterance);
      if (res.confirm_required) {
        push({
          kind: "confirm",
          text: res.clarification ?? "진행할까요?",
          intent: res.intent,
          requestId: res.request_id,
          trace: traceOf(res),
        });
      } else if (res.answer) {
        push({ kind: "bot", text: res.answer.text, trace: traceOf(res) });
      } else {
        push({
          kind: "bot",
          text: res.clarification ?? res.note ?? "답을 받지 못했습니다.",
          trace: traceOf(res),
        });
      }
    } catch (error) {
      fail(error);
    } finally {
      setBusy(false);
    }
  }

  /** ② 확인한 의도를 실행한다. `intent` 를 **그대로** 돌려보낸다. */
  async function confirm(
    turn: Extract<Turn, { kind: "confirm" }>,
    index: number,
  ) {
    if (busy || !session || turn.done) return;
    const rerun = turn.intent.action === "RERUN_WITH_CONDITION";

    // 🔴 다시 돌릴 대상이 없으면 **추측하지 않고 멈춘다.** 서버도 같은 이유로 422 를 낸다.
    if (rerun && !last) {
      push({
        kind: "error",
        text: "다시 만들 대상이 없습니다 — 먼저 매입안을 한 번 만들어야 조건을 붙일 수 있습니다.",
      });
      return;
    }

    // 한 번 누른 확인은 닫는다 — 두 번 눌러 같은 실행이 두 번 도는 것을 막는다
    setTurns((prev) =>
      prev.map((t, i) => (i === index ? { ...t, done: true } : t)),
    );
    push({ kind: "me", text: "네" });
    setBusy(true);
    try {
      const res = await execute({
        intent: turn.intent,
        requestId: turn.requestId,
        // 재요청에만 싣는다 — 조회·매입 실행에는 대상 실행이 없다
        targetRequestId: rerun ? (last?.requestId ?? undefined) : undefined,
        targetHistoryRunId: rerun ? (last?.historyRunId ?? undefined) : undefined,
        decidedBy: rerun ? session.name : undefined,
      });

      if (isProcurement(res)) {
        rememberRun(res);
        push({ kind: "run", run: res });
      } else if (res.run) {
        // 재요청 — 결정 기록과 **새로 나온 안**이 함께 온다
        rememberRun(res.run);
        push(
          { kind: "bot", text: res.answer?.text ?? "" },
          { kind: "run", run: res.run },
        );
      } else if (res.answer) {
        push({ kind: "bot", text: res.answer.text });
      } else {
        push({
          kind: "bot",
          text: res.clarification ?? "실행했지만 답이 비었습니다.",
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
      setPicked(null);
      if (!isProcurement(res) && res.answer)
        push({ kind: "bot", text: res.answer.text });
    } catch (error) {
      // 🔴 서버 문장을 그대로 보인다 — "이미 승인됐다 (회차 1)" 같은 말이 답이다
      setModalError(
        error instanceof ApiError
          ? `[${error.status}] ${error.message}`
          : String(error),
      );
    } finally {
      setModalBusy(false);
    }
  }

  function selectTab(key: string) {
    setTab(key);
    const canned = SHORTCUT[key];
    if (canned) {
      // 부서 탭은 **같은 API 를 발화문 없이 부르는 지름길**이다 — 돌아오는 것은 같다
      setTab("master");
      void send(canned);
    }
  }

  const isHistory = tab === "runs";
  const isBurnIn = tab === "burnin";

  return (
    /**
     * 🔴 `h-screen` 이지 `min-h-screen` 이 아니다. `min-h-screen` 은 **최소**만
     * 정해서, 대화가 쌓이면 부모가 같이 늘어나고 스크롤이 대화 영역이 아니라
     * **페이지 전체**에 걸린다. 그러면 하단 입력창과 왼쪽 사이드바가 화면 밖으로
     * 밀려난다 — 매 턴 `tail.scrollIntoView()` 가 돌아 **보낸 직후에는 보이므로**
     * 탭을 왕복하거나 위로 스크롤했을 때만 드러난다.
     *
     * 아래 스크롤 영역의 `min-h-0` 도 같이 있어야 한다. flex 아이템의 기본
     * `min-height: auto` 는 내용 높이라 **`flex-1` 이 내용보다 작아지지 못하고**,
     * `overflow-y-auto` 를 걸어도 스크롤이 안 걸린다.
     */
    <div className="flex h-screen">
      <Sidebar
        session={session}
        active={tab}
        onSelect={selectTab}
        onSignOut={() => {
          clearSession();
          router.replace("/");
        }}
      />

      <main className="flex min-w-0 flex-1 flex-col bg-surface">
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-6 py-3.5">
          <h1 className="m-0 text-base font-semibold">
            {isHistory ? "실행 이력" : isBurnIn ? "판단 전 30일" : "마스터에게 묻기"}
          </h1>
          <span className="font-mono text-[11.5px] text-faint">
            기준일 {AS_OF}
          </span>
        </header>

        {isBurnIn ? (
          <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
            <BurnInPanel />
          </div>
        ) : isHistory ? (
          <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
            <RunHistoryPanel known={runIds} />
          </div>
        ) : (
          <>
            <div className="min-h-0 flex-1 space-y-3.5 overflow-y-auto px-6 py-5">
              {turns.length === 0 && <Empty onPick={send} />}

              {turns.map((turn, i) => (
                <TurnView
                  key={i}
                  turn={turn}
                  index={i}
                  onConfirm={confirm}
                  busy={busy}
                >
                  {turn.kind === "run" && (
                    <ProcurementResult
                      run={turn.run}
                      canApprove={can.approve}
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
                  )}
                </TurnView>
              ))}

              {busy && (
                <p className="m-0 text-[13px] text-faint">
                  마스터가 부서를 부르는 중…
                </p>
              )}
              <div ref={tail} />
            </div>

            <div className="border-t border-line px-6 py-4">
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
                  placeholder="무엇을 도와드릴까요"
                  className="min-w-0 flex-1 bg-transparent text-[14.5px] outline-none placeholder:text-faint"
                />
                <button
                  type="submit"
                  disabled={busy || !draft.trim()}
                  className="rounded-lg bg-accent px-4 py-1.5 text-[13.5px] font-semibold text-white disabled:opacity-45"
                >
                  보내기
                </button>
              </form>
              <p className="m-0 mt-2 text-[11.5px] text-faint">
                매입 실행은{" "}
                <b className="text-muted">확인을 한 번 더 받습니다</b> — 잘못
                알아들으면 호출 예산 12회와 매입 LLM 을 태웁니다. 조회는 바로
                돕니다.
              </p>
            </div>
          </>
        )}
      </main>

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
        <p className="m-0 max-w-[74%] rounded-xl rounded-br-sm bg-accent px-3.5 py-2 text-sm text-white">
          {turn.text}
        </p>
      </div>
    );

  if (turn.kind === "bot")
    return (
      <div className="max-w-[85%] rounded-xl rounded-bl-sm border border-line-soft bg-sunk px-3.5 py-2.5">
        <div className="whitespace-pre-wrap text-sm leading-relaxed">
          {turn.text}
        </div>
        {turn.trace && <LlmTrace trace={turn.trace} />}
      </div>
    );

  if (turn.kind === "error")
    return (
      <Panel tone="attn" title="실행하지 못했습니다" items={[turn.text]} />
    );

  if (turn.kind === "confirm")
    return (
      <div className="max-w-[85%] rounded-xl rounded-bl-sm border border-line-soft bg-sunk px-3.5 py-2.5">
        <p className="m-0 text-sm">{turn.text}</p>
        <LlmTrace trace={turn.trace} />
        {turn.done ? (
          <p className="m-0 mt-2 text-[12.5px] text-faint">
            확인함 — 아래 결과를 보세요
          </p>
        ) : (
          <button
            type="button"
            onClick={() => onConfirm(turn, index)}
            disabled={busy}
            className="mt-2.5 rounded-lg bg-accent px-4 py-1.5 text-[13px] font-semibold text-white disabled:opacity-45"
          >
            네, 진행합니다
          </button>
        )}
      </div>
    );

  // kind === "run"
  return (
    <div className="rounded-xl border border-line bg-surface p-4">
      {children}
    </div>
  );
}

function Empty({ onPick }: { onPick: (text: string) => void }) {
  const samples = [
    "오늘 배추 얼마나 사야 해?",
    "창고에 얼마나 남았어?",
    "지금 자금 상황 알려줘",
    "예산 2천만원으로 낮춰서 다시 해줘",
  ];
  return (
    <div className="rounded-xl border border-dashed border-line p-6">
      <p className="m-0 text-sm font-semibold">말로 물어보세요</p>
      <p className="m-0 mt-1 text-[13px] text-muted">
        마스터가 알아듣고 필요한 부서를 부릅니다. 무엇을 확인했고 무엇을 못
        봤는지 함께 답합니다.
      </p>
      <div className="mt-3 flex flex-wrap gap-2">
        {samples.map((s) => (
          <button
            key={s}
            type="button"
            onClick={() => onPick(s)}
            className="rounded-full border border-line bg-sunk px-3 py-1 text-xs text-muted hover:border-accent hover:text-accent-ink"
          >
            {s}
          </button>
        ))}
      </div>
    </div>
  );
}
