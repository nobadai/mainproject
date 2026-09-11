"use client";

/**
 * 실행 축을 고르는 자리.
 *
 * ★ **목록은 백엔드가 준다** (`/api/console/runs`). 화면은 고르기만 하고, 어느 실행이
 *   있는지는 저장된 행이 답한다.
 *
 * 🔴 **여기서 기본 실행을 정하지 않는다.** 아무것도 안 골랐으면 빈 값이고, 그 상태를
 *    그대로 보여 준다 — 아무 실행이나 골라 숫자를 띄우는 것보다 낫다.
 *
 * 🔴 **`NEXT_PUBLIC_SIM_RUN_ID` 를 정본으로 믿지 않는다.** 목록에 실제로 있는 실행일
 *    때만 초기 선택으로 쓴다. 없는 실행을 그대로 부르면 화면은 «오류» 를 띄우는데,
 *    사실은 **설정이 낡은 것**이다.
 */

import { useEffect, useMemo, useSyncExternalStore } from "react";

import { Pill } from "@/components/console/Blocks";
import { useConsoleData } from "@/components/console/ConsoleData";
import { consoleRuns, type ConsoleRun, type ConsoleRunsResponse } from "@/lib/console_api";
import { serverSimRun, setSimRun, simRunSnapshot, subscribeSimRun } from "@/lib/run_context";

export function useSimRun(): string {
  return useSyncExternalStore(subscribeSimRun, simRunSnapshot, serverSimRun);
}

function describe(run: ConsoleRun): string {
  const parts = [run.sim_run_id];
  if (run.run_type) parts.push(run.run_type);
  if (run.status) parts.push(run.status);
  return parts.join(" · ");
}

export function RunPicker({ asOf }: { asOf: string }) {
  const simRun = useSimRun();
  const state = useConsoleData<ConsoleRunsResponse>("runs", () => consoleRuns.list(), true);
  //  ★ 빈 배열을 매 렌더마다 새로 만들면 아래 효과가 끝없이 다시 돈다.
  const rows = useMemo(() => state.data?.rows ?? [], [state.data]);
  const selected = rows.find((row) => row.sim_run_id === simRun) ?? null;

  //  ★ 고른 실행이 목록에 없으면 **고르지 않은 상태로 되돌린다.** 지워진 실행이나
  //    낡은 환경 설정이 그대로 남아 조회를 계속 실패시키는 것을 막는다.
  useEffect(() => {
    if (!simRun || rows.length === 0) return;
    if (!rows.some((row) => row.sim_run_id === simRun)) setSimRun("");
  }, [simRun, rows]);

  return (
    <div
      className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-xl border bg-panel px-4 py-3 text-[11.5px]"
      style={{ borderColor: "var(--color-hair)" }}
    >
      <Pill text={simRun ? "LIVE" : "NO RUN"} tone={simRun ? "good" : "sim"} />
      <span className="flex items-center gap-2">
        <b>sim_run_id</b>
        <select
          value={simRun}
          onChange={(event) => setSimRun(event.target.value)}
          disabled={state.loading || rows.length === 0}
          className="w-[320px] rounded-md border px-2 py-1 font-mono text-[11px]"
          style={{ borderColor: "var(--color-hair)" }}
        >
          <option value="">
            {state.loading
              ? "실행 목록을 읽는 중"
              : rows.length === 0
                ? "저장된 실행이 없습니다"
                : "실행을 선택하세요"}
          </option>
          {rows.map((row) => (
            <option key={row.sim_run_id} value={row.sim_run_id}>
              {describe(row)}
            </option>
          ))}
        </select>
      </span>
      <span>
        <b>as_of</b> <span className="font-mono">{asOf}</span>
      </span>
      <span>
        <b>실행 기준일</b>{" "}
        <span className="font-mono">{selected?.as_of ?? "—"}</span>
      </span>
      <span>
        <b>조달</b> <span className="font-mono">{selected?.financing_mode ?? "—"}</span>
      </span>
      {/* 🔴 `sim_runs` 에 정책 버전 칸이 없다. 빈 칸으로 두고 지어내지 않는다. */}
      <span>
        <b>policy</b> <span className="text-ink2">실행 행에 저장되지 않음</span>
      </span>
      {state.error && <span className="text-ink2">실행 목록을 읽지 못했습니다 — {state.error}</span>}
    </div>
  );
}
