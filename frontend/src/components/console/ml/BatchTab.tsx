"use client";

/**
 * 배치 상황 — 매일 아침 도는 것이 잘 돌았나.
 *
 * ★ **실패했을 때만 할 말이 있습니다.** 잘 돌면 조사 에이전트는 아무 말도
 *   안 합니다. 조용할 때 조용해야 진짜 신호가 눈에 띕니다.
 *
 * ★ 사흘 동안 배치가 실패했는데 아무도 몰랐던 일이 있었습니다. 실패 알림은
 *   떴는데 그 파일을 아무도 안 열어봤습니다. 그래서 화면에 올립니다.
 *
 * ★ **오늘 것을 맨 위에 둡니다.** 이 탭을 여는 사람이 알고 싶은 것은
 *   「오늘 아침 것이 잘 돌았나」 하나입니다. 지난 목록은 그 다음입니다 —
 *   15줄짜리 표를 먼저 지나가야 오늘을 볼 수 있으면 안 봅니다.
 */

import { useCallback, useEffect, useState } from "react";

import {
  batchAgent,
  batchRecent,
  MlError,
  type AgentReport,
  type BatchRun,
} from "@/lib/mlConsole";

import { DateRange } from "./DateRange";
import { ReportBody } from "./Report";
import { RerunButton } from "./RerunButton";

/**
 * DB 가 시각을 **UTC 로** 담습니다 (`+00:00`). 그대로 보이면 아침 9시 배치가
 * **00:00 으로 보입니다** — 바로 위에 「매일 아침 09:00」 이라 적어 놓고
 * 표에는 자정이 뜨니, 읽는 사람이 둘 중 뭘 믿어야 할지 모릅니다.
 * 보는 사람 시간대로 옮겨 적습니다.
 */
function when(iso: string | null): string {
  if (!iso) return "— 진행 중";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
    `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
  );
}

//  ★ **열쇠(`ok` · `fail` …)는 서버가 보내는 값 그대로 두고, 글자만 우리말로
//    답니다.** 열쇠를 우리말로 바꾸면 어느 것도 안 걸려서 성공도 실패도
//    똑같은 회색이 됩니다.
const STATE: Record<string, { label: string; fg: string; bg: string }> = {
  ok: { label: "성공", fg: "var(--color-t-good)", bg: "var(--color-t-good-bg)" },
  success: { label: "성공", fg: "var(--color-t-good)", bg: "var(--color-t-good-bg)" },
  fail: { label: "실패", fg: "var(--color-t-bad)", bg: "var(--color-t-bad-bg)" },
  failed: { label: "실패", fg: "var(--color-t-bad)", bg: "var(--color-t-bad-bg)" },
  running: { label: "진행 중", fg: "var(--color-t-info)", bg: "var(--color-t-info-bg)" },
};

export function BatchTab() {
  const [runs, setRuns] = useState<BatchRun[] | null>(null);
  const [report, setReport] = useState<AgentReport | null>(null);
  const [err, setErr] = useState<string | null>(null);
  //  ★ 고른 기간. `DateRange` 가 첫 렌더에서 기본값(기준일 −4일 ~ 기준일)을
  //    알려주므로, 목록을 받아오는 것은 **전부 여기서** 시작합니다.
  const [range, setRange] = useState<{ from: string; to: string } | null>(null);

  //  값이 같으면 **같은 객체를 그대로 둡니다** — 새 객체를 만들면 아래
  //  `useEffect` 가 다시 돌아 같은 것을 또 받아옵니다.
  const onRange = useCallback((from: string, to: string) => {
    setRange((r) => (r && r.from === from && r.to === to ? r : { from, to }));
  }, []);

  const fail = useCallback((e: unknown) => {
    setErr(e instanceof MlError ? `[${e.status || "연결 안 됨"}] ${e.message}` : String(e));
  }, []);

  //  ★ **기간이 바뀔 때마다 다시 받아옵니다.** 달력을 건드리는 것이 곧 조회입니다.
  useEffect(() => {
    if (!range) return;
    let alive = true;
    batchRecent(range.from, range.to)
      //  ★ 배열이 그대로 옵니다. `r.runs` 로 읽으면 `undefined` 가 되어
      //    **오류 없이 「읽는 중…」 에서 멈춥니다.** 실제로 그랬습니다.
      .then((r) => {
        if (!alive) return;
        setRuns(r);
        //  받아왔으면 지난 오류 줄을 지웁니다 — 남겨 두면 «지금도 안 된다» 로 읽힙니다.
        setErr(null);
      })
      .catch((e: unknown) => alive && fail(e));
    return () => {
      alive = false;
    };
  }, [range, fail]);

  //  ★ 다시 돌린 뒤에도 씁니다. 안 그러면 방금 돌린 결과가 안 보이고
  //    실패한 옛 기록이 그대로 남아 «또 실패했나» 로 읽힙니다.
  const load = useCallback(() => {
    if (range)
      batchRecent(range.from, range.to)
        .then((r) => setRuns(r))
        .catch(fail);
    //  조사는 실패했을 때만 내용이 있다. 없다고 오류가 아니다.
    batchAgent()
      .then((r) => setReport(r))
      .catch(() => {
        /* 조사가 안 돌아도 실행 목록은 보여야 한다 */
      });
  }, [range, fail]);

  //  조사 보고서는 기간과 상관없이 **한 번만** 받습니다 (오늘 것 하나입니다).
  useEffect(() => {
    batchAgent()
      .then((r) => setReport(r))
      .catch(() => {
        /* 조사가 안 돌아도 실행 목록은 보여야 한다 */
      });
  }, []);

  //  ★ **화면을 통째로 오류·「불러오는 중」 으로 덮지 않습니다** (2026-09-16).
  //    덮으면 **달력이 같이 사라져** 기간을 다시 고를 수가 없습니다. 목록을
  //    받아오는 일 자체가 달력에서 시작하므로, 달력이 없으면 영영 못 받습니다.
  //    오류도 「읽는 중」 도 목록 자리 안에서만 말합니다.
  const failed = (runs ?? []).filter((r) => (r.status ?? "").toLowerCase().startsWith("fail")).length;

  //  ★ 「오늘 것이 실패했나」 — 목록 전체가 아니라 **가장 최근 실행 하나**를
  //    봅니다. 지난주에 한 번 실패한 것 때문에 버튼이 계속 떠 있으면 안 됩니다.
  //    그리고 오늘 것이어야 합니다 — 어제 실패는 오늘 다시 돌릴 일이 아닙니다
  //    (오늘 아침 배치가 이미 그 뒤에 돌았습니다).
  const today = new Date();
  const isToday = (iso: string | null) => {
    if (!iso) return false;
    const d = new Date(iso);
    return (
      d.getFullYear() === today.getFullYear() &&
      d.getMonth() === today.getMonth() &&
      d.getDate() === today.getDate()
    );
  };
  const last = runs?.[0] ?? null;
  const todayFailed =
    !!last && isToday(last.started_at) && (last.status ?? "").toLowerCase().startsWith("fail");

  return (
    <div className="flex flex-col gap-4">
      <section
        className="flex flex-col gap-3.5 rounded-xl border bg-panel p-4"
        style={{ borderColor: "var(--color-hair)" }}
      >
        <header className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <h2 className="m-0 text-[13.5px] font-semibold">금일 자동 작업 결과</h2>
          {/*  ★ **실패했을 때만 버튼을 보입니다.** 잘 돌았는데 버튼이 있으면
                 누르고 싶어집니다 — 자동 작업은 학습표를 비우고 다시 채우는
                 것이라 이유 없이 돌릴 일이 아닙니다. */}
          {todayFailed && (
            <div className="ml-auto">
              <RerunButton what="batch" label="다시 돌리기" onDone={load} />
            </div>
          )}
        </header>
        {todayFailed && (
          <p
            className="m-0 rounded-lg px-3.5 py-2.5 text-[12px] leading-relaxed"
            style={{ background: "var(--color-t-warn-bg)", color: "var(--color-t-warn)" }}
          >
            ★ <b>오늘 아침 작업이 실패했습니다.</b> 매입 파트 전달표에 오늘 것이 안 갔을 수
            있습니다. 원인을 고친 뒤 <b>다시 돌리기</b>를 누르세요 — 아침에 도는 것과{" "}
            <b>똑같은 것</b>을 돌립니다. 같은 날짜를 다시 쓰는 것이라 있던 날이 사라지지
            않습니다.
          </p>
        )}
        {report ? (
          <ReportBody report={report} />
        ) : (
          <p className="m-0 text-[12px]" style={{ color: "var(--color-mut2)" }}>
            불러오는 중… (실패한 작업이 없으면 내용이 비어 있습니다)
          </p>
        )}
      </section>
      <section
        className="flex flex-col gap-3.5 rounded-xl border bg-panel p-4"
        style={{ borderColor: "var(--color-hair)" }}
      >
        <header className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <h2 className="m-0 text-[13.5px] font-semibold">최근 자동 작업 기록</h2>
          {failed > 0 && (
            <span
              className="rounded px-2 py-0.5 text-[11px] font-semibold"
              style={{ background: "var(--color-t-bad-bg)", color: "var(--color-t-bad)" }}
            >
              {failed}건 실패
            </span>
          )}
          {/*  달력을 건드리면 그 자리에서 다시 받아옵니다 — 조회 버튼이 없습니다. */}
          <div className="ml-auto">
            <DateRange onChange={onRange} />
          </div>
        </header>

        {err && (
          <p
            className="m-0 rounded-lg px-3.5 py-2.5 text-[12px]"
            style={{ background: "var(--color-t-bad-bg)", color: "var(--color-t-bad)" }}
          >
            {err}
          </p>
        )}

        {!runs ? (
          <p className="m-0 py-6 text-center text-[12.5px]" style={{ color: "var(--color-mut2)" }}>
            작업 기록을 불러오는 중…
          </p>
        ) : runs.length === 0 ? (
          //  ★ **빈 표를 그리지 않습니다.** 머리글만 있는 표는 «배치가 안 돌았다»
          //    로 읽힙니다 — 실제로는 «그 기간을 안 골랐다» 입니다.
          <p
            className="m-0 rounded-lg border border-dashed px-4 py-6 text-center text-[12px]"
            style={{ borderColor: "var(--color-hair)", color: "var(--color-mut2)" }}
          >
            이 기간에 배치 기록이 없습니다
          </p>
        ) : (
          <div className="thin-scroll -mx-1 overflow-x-auto px-1">
            <table className="w-full border-collapse text-[12px]">
              <thead>
                <tr>
                  {/*  ★ «실행»(run_id) 열은 뺐습니다 (2026-09-16). 사람이 볼 값이
                         아니라 DB 안에서 줄을 잇는 번호입니다 — 좁은 화면에서
                         자리만 차지했습니다. 서버는 그대로 보냅니다. */}
                  {["시작 시각", "종료 시각", "상태", "실행 서버"].map((h) => (
                    <th
                      key={h}
                      scope="col"
                      className="whitespace-nowrap border-b px-2.5 py-2 text-left font-medium"
                      style={{ borderColor: "var(--color-hair)", color: "var(--color-mut)" }}
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {/*  ★ 자르지 않습니다 — 고른 기간이 곧 보이는 범위입니다.
                       20줄에서 자르면 닷새를 골라도 나흘만 보일 수 있습니다. */}
                {runs.map((r) => {
                  const st = STATE[(r.status ?? "").toLowerCase()] ?? {
                    label: r.status ?? "—",
                    fg: "var(--color-mut)",
                    bg: "var(--color-sunk)",
                  };
                  return (
                    <tr key={r.run_id}>
                      <td
                        className="tabular whitespace-nowrap border-b px-2.5 py-2 font-mono"
                        style={{ borderColor: "var(--color-hair-soft)" }}
                      >
                        {when(r.started_at)}
                      </td>
                      <td
                        className="tabular whitespace-nowrap border-b px-2.5 py-2 font-mono"
                        style={{
                          borderColor: "var(--color-hair-soft)",
                          color: r.finished_at ? undefined : "var(--color-mut2)",
                        }}
                      >
                        {when(r.finished_at)}
                      </td>
                      {/*  ★ 배지가 **두 줄로 찢어지던** 것을 막습니다 (2026-09-16).
                             칸에 `whitespace-nowrap`, 배지에 `inline-flex` 를 줍니다 —
                             `inline` 이면 줄바꿈 자리에서 배경색이 반으로 갈립니다. */}
                      <td
                        className="whitespace-nowrap border-b px-2.5 py-2"
                        style={{ borderColor: "var(--color-hair-soft)" }}
                      >
                        <span
                          className="inline-flex items-center whitespace-nowrap rounded px-2 py-0.5 text-[11px] font-semibold"
                          style={{ background: st.bg, color: st.fg }}
                        >
                          {st.label}
                        </span>
                      </td>
                      <td
                        className="whitespace-nowrap border-b px-2.5 py-2 text-[11.5px]"
                        style={{ borderColor: "var(--color-hair-soft)", color: "var(--color-mut)" }}
                      >
                        {r.host ?? "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

    </div>
  );
}
