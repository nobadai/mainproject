"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, useSyncExternalStore } from "react";

import { Panel } from "@/components/Badges";
import { DecisionModal } from "@/components/DecisionModal";
import { ProcurementResult } from "@/components/ProcurementResult";
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
  | { kind: "bot"; text: string }
  | { kind: "confirm"; text: string; intent: Intent; requestId: string }
  | { kind: "run"; run: ProcurementRunResponse }
  | { kind: "error"; text: string };

const SHORTCUT: Record<string, string> = {
  purchase: "오늘 배추 얼마나 사야 해?",
  inventory: "창고에 얼마나 남았어?",
  finance: "지금 자금 상황 알려줘",
};

export default function ConsolePage() {
  const router = useRouter();
  // localStorage 는 React 바깥이라 구독해서 읽는다 (`session.ts` 주석 참조)
  const session = useSyncExternalStore(subscribeSession, sessionSnapshot, serverSnapshot);
  const [tab, setTab] = useState("master");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);

  // 승인 모달 — 어느 실행의 어느 안인지 함께 들고 있어야 한다
  const [picked, setPicked] = useState<{ scenario: Scenario; requestId: string } | null>(null);
  const [modalBusy, setModalBusy] = useState(false);
  const [modalError, setModalError] = useState<string | null>(null);

  const tail = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // 세션이 없으면 로그인으로. **효과가 하는 일은 외부(라우터) 갱신뿐이다.**
    if (session === null) router.replace("/");
  }, [session, router]);

  useEffect(() => {
    tail.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns]);

  if (!session) return null;
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
        });
      } else if (res.answer) {
        push({ kind: "bot", text: res.answer.text });
      } else {
        push({ kind: "bot", text: res.clarification ?? res.note ?? "답을 받지 못했습니다." });
      }
    } catch (error) {
      fail(error);
    } finally {
      setBusy(false);
    }
  }

  /** ② 확인한 의도를 실행한다. `intent` 를 **그대로** 돌려보낸다. */
  async function confirm(turn: Extract<Turn, { kind: "confirm" }>) {
    if (busy) return;
    push({ kind: "me", text: "네" });
    setBusy(true);
    try {
      const res = await execute({ intent: turn.intent, requestId: turn.requestId });
      if (isProcurement(res)) push({ kind: "run", run: res });
      else if (res.answer) push({ kind: "bot", text: res.answer.text });
      else push({ kind: "bot", text: res.clarification ?? "실행했지만 답이 비었습니다." });
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
        decidedBy: session.name,
      });
      setPicked(null);
      if (!isProcurement(res) && res.answer) push({ kind: "bot", text: res.answer.text });
    } catch (error) {
      // 🔴 서버 문장을 그대로 보인다 — "이미 승인됐다 (회차 1)" 같은 말이 답이다
      setModalError(
        error instanceof ApiError ? `[${error.status}] ${error.message}` : String(error),
      );
    } finally {
      setModalBusy(false);
    }
  }

  function selectTab(key: string) {
    setTab(key);
    const canned = SHORTCUT[key];
    if (canned) void send(canned);
  }

  return (
    <div className="flex min-h-screen">
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
          <h1 className="m-0 text-base font-semibold">마스터에게 묻기</h1>
          <span className="font-mono text-[11.5px] text-faint">기준일 {AS_OF}</span>
        </header>

        <div className="flex-1 space-y-3.5 overflow-y-auto px-6 py-5">
          {turns.length === 0 && <Empty onPick={send} />}

          {turns.map((turn, i) => (
            <TurnView key={i} turn={turn} onConfirm={confirm} busy={busy}>
              {turn.kind === "run" && (
                <ProcurementResult
                  run={turn.run}
                  canApprove={can.approve}
                  onPick={(scenario) => {
                    setModalError(null);
                    setPicked({ scenario, requestId: turn.run.request_id });
                  }}
                />
              )}
            </TurnView>
          ))}

          {busy && <p className="m-0 text-[13px] text-faint">마스터가 부서를 부르는 중…</p>}
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
            매입 실행은 <b className="text-muted">확인을 한 번 더 받습니다</b> — 잘못 알아들으면
            호출 예산 12회와 매입 LLM 을 태웁니다. 조회는 바로 돕니다.
          </p>
        </div>
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
  onConfirm,
  busy,
  children,
}: {
  turn: Turn;
  onConfirm: (t: Extract<Turn, { kind: "confirm" }>) => void;
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
      <div className="max-w-[85%] whitespace-pre-wrap rounded-xl rounded-bl-sm border border-line-soft bg-sunk px-3.5 py-2.5 text-sm leading-relaxed">
        {turn.text}
      </div>
    );

  if (turn.kind === "error")
    return <Panel tone="attn" title="실행하지 못했습니다" items={[turn.text]} />;

  if (turn.kind === "confirm")
    return (
      <div className="max-w-[85%] rounded-xl rounded-bl-sm border border-line-soft bg-sunk px-3.5 py-2.5">
        <p className="m-0 text-sm">{turn.text}</p>
        <button
          type="button"
          onClick={() => onConfirm(turn)}
          disabled={busy}
          className="mt-2.5 rounded-lg bg-accent px-4 py-1.5 text-[13px] font-semibold text-white disabled:opacity-45"
        >
          네, 진행합니다
        </button>
      </div>
    );

  // kind === "run"
  return (
    <div className="rounded-xl border border-line bg-surface p-4">{children}</div>
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
        마스터가 알아듣고 필요한 부서를 부릅니다. 무엇을 확인했고 무엇을 못 봤는지 함께
        답합니다.
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
