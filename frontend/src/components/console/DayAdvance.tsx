"use client";

/**
 * 하루를 넘기는 자리 — 「하루 마치기」와 「다음 날 열기」.
 *
 * ★ **왜 둘로 가르나.** 마치기와 열기 사이에 사람이 **결과를 볼 시간**이 필요하다.
 *   하나로 묶으면 오늘 무엇이 됐는지 못 보고 내일로 넘어간다.
 *
 * 🔴 **순서가 계약이다.** 하루가 도는 차례는 `app/master/scheduler.py` 가 못 박았다 —
 *    개장 → 전이 재시도 → 입고 → 채권 → 수금 → [판단] → 출고 → 마감.
 *    여기서 순서를 새로 정하지 않고 그 문서 그대로 부른다.
 *
 * 🔴 **유지보수(`run_auto_maintenance`)는 안 부른다.** 기본이 꺼짐이고 걷기도 플래그로만
 *    켠다 — 화면이 켜면 걷기와 다른 하루가 된다.
 *
 * 🔴 **되돌리기를 만들지 않는다.** 닫기가 실패하면 그날이 막힌 채로 선다. 되돌리려면
 *    재무·물류가 같이 움직여야 하고, 그것은 이 자리의 몫이 아니다.
 */

import { useState } from "react";

import { ApiError, dayStep, type DayStepPath } from "@/lib/api";
import { setDemoAsOf } from "@/lib/demo_as_of";
import { formatKoreanDate } from "@/lib/procurementLabels";

/**
 * 🔴 **여기 적힌 축에서만 그린다.** 목록에 없는 축에서는 **아예 안 그린다**
 *    (숨김이 아니라 없음). **막는 쪽이 기본이다.**
 *
 * 🔴 **왜 거부 목록이 아니라 허용 목록인가.** `SIM-CHAIN-FINAL-0918` 은 **제출물**이고
 *    260일로 닫혀 있어야 한다. 누가 실수로 눌러 261일째가 생기면 **제출 숫자와 화면이
 *    갈린다.** 거부 목록은 새 축이 생길 때마다 적는 것을 잊으면 열린 채로 새고,
 *    허용 목록은 적는 것을 잊어도 막힌 채로 선다. 그래서 **허용을 적고 나머지를 막는다.**
 *
 * ★ **왜 이 둘인가.**
 *   - `SIM-MENTOR-0918` — 멘토 체험용. 멘토가 직접 하루를 넘겨 본다.
 *   - `SIM-SHOOT-0918` — 시연 영상 촬영용. 재촬영을 제출물 밖에서 돌린다.
 *
 * ⚠️ **축 이름을 코드에 박는 것이 마음에 걸린다.** 그래도 박아 둔다 — 오늘·내일짜리고,
 *   **박아 두는 편이 실수로 켜지는 것보다 안전하다.** 환경변수나 설정을 새로 만들면
 *   그 설정이 잘못 켜진 날 제출물 위에서 하루가 돈다.
 *
 * ★ **발표 뒤 지울 자리** — 이 목록과 아래 가드를 같이 지운다. #833 이 주석으로 남긴
 *   되돌릴 자리(`lib/run_context.ts` · `backend/app/api/shown_run.py` ·
 *   `frontend/Dockerfile`) 와 같은 때 손본다.
 */
const DAY_ADVANCE_RUNS: readonly string[] = ["SIM-MENTOR-0918", "SIM-SHOOT-0918"];

/**
 * **멈추는 어휘.** 이 넷이 나오면 거기서 서고 뒤 단계를 부르지 않는다.
 *
 * 🔴 **성공 어휘를 외우지 않는다.** 단계마다 다르고(`OPENED` · `ALREADY_OPENED` ·
 *    `RECEIVED` · `ISSUED` · `COLLECTED` · `RAN` · `CLOSED` · `NOTHING_DUE`) 주인은
 *    각 부서 모듈이다. 새 성공 어휘가 생겨도 화면이 막지 않게 **멈추는 쪽만** 적는다.
 *
 * ★ 이름의 주인은 백엔드다 (`day_open.DayOpenOut` · `inbound.InboundOut` ·
 *   `receivable.ReceivableOut` · `collection.CollectionOut` · `outbound_flow.OutboundOut` ·
 *   `pending_transition.RetryOut` · `closing.ClosingOut`). 여기는 그 값을 견주기만 한다.
 */
const STOP_STATUSES = ["FAILED", "BLOCKED", "NOT_OPENED", "REJECTED_GAP"];

type StepMark = "idle" | "running" | "ok" | "stop";

interface Step {
  label: string;
  path: DayStepPath;
  mark: StepMark;
  /** 서버가 준 사유. **여기서 짓지 않는다.** */
  detail: string;
}

/** 🟢 ①「하루 마치기」 — 출고 → 마감. 오늘(화면 기준일)에 대해 돈다. */
const FINISH: readonly { label: string; path: DayStepPath }[] = [
  { label: "출고", path: "ship" },
  { label: "하루 닫기", path: "close" },
];

/** 🟢 ②「다음 날 열기」 — 개장 → 전이 재시도 → 입고 → 채권 → 수금. */
const OPEN_NEXT: readonly { label: string; path: DayStepPath }[] = [
  { label: "하루 열기", path: "open" },
  { label: "전이 재시도", path: "retry-transitions" },
  { label: "도착분 받기", path: "receive" },
  { label: "채권 세우기", path: "issue-receivables" },
  { label: "수금", path: "collect" },
];

/**
 * 다음 날짜. **달력 하루를 더한다.**
 *
 * 🔴 **주말·휴장 판정을 화면에서 새로 짓지 않는다.** 달력 규칙의 주인은 백엔드이고,
 *    개장·입고·수금은 **달력일** 사건이다 (창고는 토요일에도 받는다). 그 날이 안 되는
 *    날이면 서버가 거절하고, 화면은 **그 사유를 그대로** 보이고 멈춘다.
 */
function nextCalendarDay(ymd: string): string {
  const [y, m, d] = ymd.split("-").map(Number);
  if (!y || !m || !d) return ymd;
  return new Date(Date.UTC(y, m - 1, d + 1)).toISOString().slice(0, 10);
}

/**
 * 서버가 준 사유를 한 줄로 **그대로** 옮긴다.
 *
 * ★ `PurchaseRecordCard.serverReasonText` 와 같은 규율이다 — 일반 문장으로 덮으면
 *   **무엇이 안 됐는지**가 사라지고, 사람이 다음 수를 못 고른다.
 */
function serverReasonText(error: unknown): string {
  let message = error instanceof Error ? error.message : "";
  try {
    const parsed = JSON.parse(message) as unknown;
    if (Array.isArray(parsed)) {
      message = parsed
        .map((item) => String((item as { msg?: unknown })?.msg ?? ""))
        .map((msg) => msg.replace(/^Value error,\s*/, ""))
        .filter(Boolean)
        .join(" · ");
    }
  } catch {
    /* 문장 그대로 */
  }
  if (error instanceof ApiError && message.trim() !== "") return `${error.status} ${message}`;
  return message.trim() === "" ? "서버가 사유를 주지 않았습니다." : message;
}

/** 단계 한 줄에 적을 문장. **서버가 준 것만 적는다.** */
function stepDetail(status: string, reason: string, nextAction: string): string {
  return [status, reason, nextAction && `다음 할 일: ${nextAction}`].filter(Boolean).join(" — ");
}

export function DayAdvance({ asOf, simRunId }: { asOf: string; simRunId: string }) {
  const [steps, setSteps] = useState<Step[]>([]);
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);

  //  🔴 **허용한 축에서만 있다.** 목록에 없으면 그리지 않는다 (위 상수 주석 참조).
  if (!DAY_ADVANCE_RUNS.includes(simRunId)) return null;

  const next = nextCalendarDay(asOf);

  /**
   * 단계를 **순서대로** 부른다. 🔴 **멈추면 거기서 선다** — 뒤 단계를 부르지 않는다.
   *
   * @returns 끝까지 갔으면 `true`.
   */
  async function run(
    plan: readonly { label: string; path: DayStepPath }[],
    day: string,
    heading: string,
  ): Promise<boolean> {
    const rows: Step[] = plan.map((one) => ({ ...one, mark: "idle", detail: "" }));
    setTitle(heading);
    setSteps(rows);
    setBusy(true);
    try {
      for (let i = 0; i < rows.length; i += 1) {
        setSteps(rows.map((row, at) => (at === i ? { ...row, mark: "running" } : row)));
        try {
          const out = await dayStep(rows[i].path, day, simRunId);
          const detail = stepDetail(out.status, out.reason ?? "", out.next_action ?? "");
          const stopped = STOP_STATUSES.includes(out.status);
          rows[i] = { ...rows[i], mark: stopped ? "stop" : "ok", detail };
          setSteps([...rows]);
          //  🔴 멈춘 단계 뒤는 **안 부른다.** 뒤 단계는 「안 함」으로 남는다.
          if (stopped) return false;
        } catch (error) {
          rows[i] = { ...rows[i], mark: "stop", detail: serverReasonText(error) };
          setSteps([...rows]);
          return false;
        }
      }
      return true;
    } finally {
      setBusy(false);
    }
  }

  async function finishDay() {
    await run(FINISH, asOf, `${formatKoreanDate(asOf)} 마치기`);
  }

  async function openNextDay() {
    const done = await run(OPEN_NEXT, next, `${formatKoreanDate(next)} 열기`);
    //  🔴 **하루가 열렸으면 화면 기준일도 옮긴다.** 안 옮기면 다음 조작이 어제로 간다.
    //     ★ 기준일의 주인은 `lib/demo_as_of.ts` 다 — 그 모듈이 주는 방식을 그대로 쓴다.
    if (done) setDemoAsOf(next);
  }

  return (
    <section
      aria-label="하루 넘기기"
      className="border-b border-line bg-sunk px-4 py-2.5"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[16px] font-semibold">하루 넘기기</span>
        <button
          type="button"
          onClick={() => void finishDay()}
          disabled={busy}
          className="rounded-lg border border-line bg-surface px-3 py-1.5 text-[16.5px] font-semibold
            text-ink transition hover:border-accent disabled:opacity-45
            focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
        >
          하루 마치기 · {formatKoreanDate(asOf)}
        </button>
        <button
          type="button"
          onClick={() => void openNextDay()}
          disabled={busy}
          className="rounded-lg bg-accent px-3 py-1.5 text-[16.5px] font-semibold text-white
            transition disabled:opacity-45
            focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
        >
          다음 날 열기 · {formatKoreanDate(next)}
        </button>
        <span className="text-[15.5px] text-faint">
          마치기를 먼저 하고, 오늘 결과를 본 뒤 다음 날을 여십시오.
        </span>
      </div>

      {steps.length > 0 && (
        <div className="mt-2.5 rounded-lg border border-line bg-surface p-3">
          <p className="m-0 text-[16px] font-semibold">{title}</p>
          <ol className="m-0 mt-2 flex flex-col gap-1 p-0 text-[16px]">
            {steps.map((step, i) => (
              <li key={step.path} className="flex list-none flex-wrap items-baseline gap-2">
                <span className="tabular-nums text-faint">{i + 1}</span>
                <span className="min-w-[7rem] font-medium">{step.label}</span>
                <StepMarkView mark={step.mark} />
                {step.detail && (
                  //  🔴 **서버가 준 사유를 그대로 적는다.** 덮으면 무엇이 안 됐는지 사라진다.
                  <span
                    className={
                      step.mark === "stop" ? "text-[15.5px] text-warn" : "text-[15.5px] text-muted"
                    }
                  >
                    {step.detail}
                  </span>
                )}
              </li>
            ))}
          </ol>
          {steps.some((step) => step.mark === "stop") && (
            <p className="m-0 mt-2 text-[15.5px] text-muted">
              멈춘 단계 뒤는 부르지 않았습니다. 사유를 보고 그 자리를 푼 뒤 같은 버튼을 다시
              누르십시오.
            </p>
          )}
        </div>
      )}
    </section>
  );
}

/** 🟢 됨 · 🔴 멈춤 · ⚪ 안 함. **색과 글자 둘 다** 로 적는다 — 색만으로는 못 읽는 사람이 있다. */
function StepMarkView({ mark }: { mark: StepMark }) {
  if (mark === "ok") return <span className="text-[15.5px] font-semibold text-t-good">됨</span>;
  if (mark === "stop") return <span className="text-[15.5px] font-semibold text-warn">멈춤</span>;
  if (mark === "running") return <span className="text-[15.5px] text-muted">도는 중</span>;
  return <span className="text-[15.5px] text-faint">안 함</span>;
}
