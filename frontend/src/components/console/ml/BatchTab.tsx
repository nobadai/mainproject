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

import { useEffect, useState } from "react";

import {
  batchAgent,
  batchRecent,
  MlError,
  type AgentReport,
  type BatchRun,
} from "@/lib/mlConsole";

import { ReportBody } from "./Report";

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

  useEffect(() => {
    let alive = true;
    const fail = (e: unknown) =>
      alive && setErr(e instanceof MlError ? `[${e.status || "연결 안 됨"}] ${e.message}` : String(e));
    batchRecent()
      //  ★ 배열이 그대로 옵니다. `r.runs` 로 읽으면 `undefined` 가 되어
      //    **오류 없이 「읽는 중…」 에서 멈춥니다.** 실제로 그랬습니다.
      .then((r) => alive && setRuns(r))
      .catch(fail);
    //  조사는 실패했을 때만 내용이 있다. 없다고 오류가 아니다.
    batchAgent()
      .then((r) => alive && setReport(r))
      .catch(() => {
        /* 조사가 안 돌아도 실행 목록은 보여야 한다 */
      });
    return () => {
      alive = false;
    };
  }, []);

  if (err)
    return (
      <p
        className="m-0 rounded-lg px-4 py-3.5 text-[12.5px]"
        style={{ background: "var(--color-t-bad-bg)", color: "var(--color-t-bad)" }}
      >
        {err}
      </p>
    );
  if (!runs)
    return (
      <p className="m-0 py-10 text-center text-[12.5px]" style={{ color: "var(--color-mut2)" }}>
        작업 기록을 불러오는 중…
      </p>
    );

  const failed = runs.filter((r) => (r.status ?? "").toLowerCase().startsWith("fail")).length;

  return (
    <div className="flex flex-col gap-4">
      <section
        className="flex flex-col gap-3.5 rounded-xl border bg-panel p-4"
        style={{ borderColor: "var(--color-hair)" }}
      >
        <header className="flex flex-wrap items-center gap-x-3">
          <h2 className="m-0 text-[13.5px] font-semibold">오늘 자동 작업 상태</h2>
          <span className="text-[11.5px]" style={{ color: "var(--color-mut)" }}>
            오늘 아침 작업이 잘 끝났는지 보여줍니다 · 실패했을 때만 자세한 내용이 나옵니다
          </span>
        </header>
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
          <span className="text-[11.5px]" style={{ color: "var(--color-mut)" }}>
            매일 아침 09:00 · 데이터 수집 → 표 다시 만들기 → 가격 예측 → 저장 → 예측 채점
          </span>
          {failed > 0 && (
            <span
              className="ml-auto rounded px-2 py-0.5 text-[11px] font-semibold"
              style={{ background: "var(--color-t-bad-bg)", color: "var(--color-t-bad)" }}
            >
              {failed}건 실패
            </span>
          )}
        </header>

        {runs.length === 0 ? (
          <p
            className="m-0 rounded-lg border border-dashed px-4 py-6 text-center text-[12px]"
            style={{ borderColor: "var(--color-hair)", color: "var(--color-mut2)" }}
          >
            작업 기록이 없습니다 — 아직 실행되지 않았거나 로그를 읽지 못했습니다
          </p>
        ) : (
          <div className="thin-scroll -mx-1 overflow-x-auto px-1">
            <table className="w-full border-collapse text-[12px]">
              <thead>
                <tr>
                  {["시작 시각", "종료 시각", "상태", "실행", "실행 서버"].map((h) => (
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
                {runs.slice(0, 20).map((r) => {
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
                      <td className="border-b px-2.5 py-2" style={{ borderColor: "var(--color-hair-soft)" }}>
                        <span
                          className="rounded px-2 py-0.5 text-[11px] font-semibold"
                          style={{ background: st.bg, color: st.fg }}
                        >
                          {st.label}
                        </span>
                      </td>
                      <td
                        className="whitespace-nowrap border-b px-2.5 py-2 font-mono text-[11px]"
                        style={{ borderColor: "var(--color-hair-soft)", color: "var(--color-mut2)" }}
                      >
                        {r.run_id}
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
