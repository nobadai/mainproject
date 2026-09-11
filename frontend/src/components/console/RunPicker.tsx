"use client";

/**
 * 실행 축을 고르는 자리.
 *
 * ⚠️ **목록이 아니라 입력이다.** `sim_runs` 를 나열하는 read 계약이 저장소에 없다
 *   (마스터 소유 표 · 2026-09-11 실측). 목록 API 가 생기면 이 입력이 선택 상자로
 *   바뀌고, 그때도 **값의 주인은 `lib/run_context.ts`** 하나다.
 *
 * 🔴 여기서 기본 실행을 넣어 주지 않는다. 빈 값은 *"아직 안 골랐다"* 이고, 그 상태를
 *    그대로 화면에 보여 주는 것이 아무 실행이나 보여 주는 것보다 낫다.
 */

import { useState, useSyncExternalStore } from "react";

import { Pill } from "@/components/console/Blocks";
import { serverSimRun, setSimRun, simRunSnapshot, subscribeSimRun } from "@/lib/run_context";

export function useSimRun(): string {
  return useSyncExternalStore(subscribeSimRun, simRunSnapshot, serverSimRun);
}

export function RunPicker({ asOf }: { asOf: string }) {
  const simRun = useSimRun();
  const [draft, setDraft] = useState(simRun);
  return (
    <div
      className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-xl border bg-panel px-4 py-3 text-[11.5px]"
      style={{ borderColor: "var(--color-hair)" }}
    >
      <Pill text={simRun ? "LIVE" : "NO RUN"} tone={simRun ? "good" : "sim"} />
      <span>
        <b>as_of</b> <span className="font-mono">{asOf}</span>
      </span>
      <span className="flex items-center gap-2">
        <b>sim_run_id</b>
        <input
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onBlur={() => setSimRun(draft)}
          onKeyDown={(event) => {
            if (event.key === "Enter") setSimRun(draft);
          }}
          placeholder="예: SIM-WALK-2026-V4"
          spellCheck={false}
          className="w-[260px] rounded-md border px-2 py-1 font-mono text-[11px]"
          style={{ borderColor: "var(--color-hair)" }}
        />
      </span>
      <span className="text-ink2">
        {simRun ? "이 실행의 저장된 사실만 조회합니다." : "실행을 지정하기 전에는 조회하지 않습니다."}
      </span>
    </div>
  );
}
