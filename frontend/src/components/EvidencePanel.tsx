"use client";

import { useState } from "react";

import { AGENT_LABEL } from "@/components/LlmTrace";
import { evidenceGroups } from "@/lib/procurementLabels";
import type { ProcurementRunResponse } from "@/lib/types";

/**
 * 이 안의 숫자가 어디서 왔는가.
 *
 * ★ 사용자에게 필요한 것만 사람 말로 보인다 (2026-09-15 결정).
 *   이 실행의 품목 · 사전에 있는 항목만 남기고, 같은 값은 한 번만 적는다.
 *   고르고 옮기는 규칙은 `lib/procurementLabels.ts` 한 곳에 있다.
 *
 * ★ 기본은 접어 둔다. 결론을 먼저 보고, "왜?" 라고 물을 때 연다.
 */
export function EvidencePanel({
  evidences,
  scenarios,
  item,
}: {
  evidences: ProcurementRunResponse["evidences"];
  scenarios: ProcurementRunResponse["scenarios"];
  /** 이 실행의 품목. 다른 품목의 값은 보이지 않는다. */
  item: string | null;
}) {
  const [open, setOpen] = useState(false);
  const groups = evidenceGroups(evidences ?? [], scenarios ?? [], item);
  const count = groups.reduce((n, g) => n + g.rows.length, 0);

  if (count === 0) return null;

  return (
    <section className="mt-4 rounded-lg border border-line">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left"
      >
        <span className="text-[17px] font-semibold">
          이 안의 숫자는 어디서 왔나요
          <span className="ml-2 font-normal text-muted">{count}건</span>
        </span>
        <span className="text-[16px] text-muted">{open ? "접기" : "펼치기"}</span>
      </button>

      {open && (
        <div className="overflow-x-auto border-t border-line">
          <table className="w-full min-w-[420px] border-collapse text-[16.5px]">
            <thead>
              <tr className="text-left text-muted">
                <th className="px-3 py-1.5 font-medium">부서</th>
                <th className="px-3 py-1.5 font-medium">항목</th>
                <th className="px-3 py-1.5 text-right font-medium">값</th>
              </tr>
            </thead>
            {groups.map((g, gi) => (
              <tbody key={`${g.agent}-${g.scenario ?? ""}`}>
                {g.rows.map((row, i) => (
                  <tr
                    key={`${row.label}-${i}`}
                    className={i === 0 ? "border-t border-line" : ""}
                  >
                    <td className="whitespace-nowrap px-3 py-1.5 align-top font-medium">
                      {i === 0 && (gi === 0 || groups[gi - 1].agent !== g.agent)
                        ? (AGENT_LABEL[g.agent] ?? "")
                        : ""}
                    </td>
                    <td className="px-3 py-1.5">
                      {row.scenario ? `${row.scenario} · ${row.label}` : row.label}
                    </td>
                    <td className="whitespace-nowrap px-3 py-1.5 text-right tabular-nums">
                      {row.value}
                    </td>
                  </tr>
                ))}
              </tbody>
            ))}
          </table>
        </div>
      )}
    </section>
  );
}
