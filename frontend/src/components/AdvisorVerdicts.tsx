"use client";

import { useState } from "react";

import { AGENT_LABEL } from "@/components/LlmTrace";
import {
  financeSummary,
  logisticsSummary,
  STATUS_LABEL,
  type FinanceSummary,
  type LogisticsSummary,
} from "@/lib/procurementLabels";
import type { ProcurementRunResponse } from "@/lib/types";

/**
 * 재무·물류가 이 안들을 보고 낸 판정.
 *
 * ★ 사용자에게 필요한 것만 사람 말로 보인다 (2026-09-15 결정).
 *   부서가 보낸 값을 키째 펴지 않고, 판정 · 이유 문장 · 판단에 쓸 숫자만 요약한다.
 *   요약 규칙은 `lib/procurementLabels.ts` 한 곳에 있다.
 *
 * ★ 「조건부」 는 풀어 적는다. 안에 문제가 없는데 확인하지 못한 항목 때문에
 *   조건부인 날은 그 사실을 한 줄로 말한다.
 *
 * ★ 조정안은 여기 없다. `AdjustmentPanel` 이 갖는다.
 */

const STATUS_TONE: Record<string, string> = {
  ok: "border-line text-muted",
  conditional: "border-warn/35 text-warn",
  reject: "border-warn/45 text-warn",
};

export function AdvisorVerdicts({ verdicts }: { verdicts: ProcurementRunResponse["verdicts"] }) {
  const rows = Object.entries(verdicts ?? {});
  if (rows.length === 0) return null;

  return (
    <div className="rounded-lg border border-line bg-sunk p-3">
      <p className="mb-2 text-[11px] font-semibold uppercase tracking-[0.05em] text-muted">
        부서 판정
      </p>
      <div className="flex flex-col gap-2">
        {rows.map(([agent, v]) => (
          <AdvisorRow key={agent} agent={agent} verdict={v} />
        ))}
      </div>
    </div>
  );
}

function AdvisorRow({
  agent,
  verdict,
}: {
  agent: string;
  verdict: ProcurementRunResponse["verdicts"][string];
}) {
  const [open, setOpen] = useState(false);
  const label = STATUS_LABEL[verdict.business_status];
  const finance = agent === "finance" ? financeSummary(verdict.payload) : null;
  const logistics =
    agent === "inventory" ? logisticsSummary(verdict.payload, verdict.business_status) : null;
  const hasDetail = finance != null || logistics != null;
  const name = AGENT_LABEL[agent] ?? "부서";

  return (
    <div className="rounded-lg border border-line-soft bg-surface px-3 py-2">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="text-[13px] font-semibold text-ink">{name}</span>
        {label ? (
          <span
            className={`rounded-md border px-1.5 py-0.5 text-[11.5px] ${
              STATUS_TONE[verdict.business_status] ?? "border-line text-muted"
            }`}
          >
            {label}
          </span>
        ) : (
          <span className="rounded-md border border-warn/35 px-1.5 py-0.5 text-[11.5px] text-warn">
            판정 없음
          </span>
        )}
        {verdict.runtime_status !== "READY" && (
          <span className="text-[11.5px] text-warn">판정을 받지 못했습니다</span>
        )}
        {verdict.needs_followup && <span className="text-[11.5px] text-warn">후속 확인 필요</span>}
        {hasDetail && (
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
            className="ml-auto rounded-md border border-line px-2 py-0.5 text-[11.5px] text-muted hover:border-accent hover:text-accent-ink"
          >
            {open ? "접기" : "자세히"}
          </button>
        )}
      </div>

      {verdict.reasoning && (
        <p className="m-0 mt-1 text-[12.5px] leading-relaxed text-muted">{verdict.reasoning}</p>
      )}
      {logistics?.conditionalNote && (
        <p className="m-0 mt-1 text-[12.5px] leading-relaxed text-warn">
          {logistics.conditionalNote}
        </p>
      )}

      {open && finance && <FinanceDetail summary={finance} />}
      {open && logistics && <LogisticsDetail summary={logistics} />}
    </div>
  );
}

function FinanceDetail({ summary }: { summary: FinanceSummary }) {
  return (
    <div className="mt-2 flex flex-col gap-2 border-t border-line-soft pt-2 text-[12.5px]">
      {summary.sharedCap && (
        <p className="m-0">
          <span className="text-muted">매입에 쓸 수 있는 한도</span>
          <span className="ml-2 tabular-nums">{summary.sharedCap}</span>
        </p>
      )}
      {summary.scenarios.map((s, i) => (
        <div key={`${s.name}-${i}`} className="rounded-md border border-line-soft px-2.5 py-1.5">
          <p className="m-0">
            <b className="font-semibold text-ink">{s.name}</b>
            <span className="ml-1.5 text-muted">{s.status}</span>
          </p>
          {s.reason && <p className="m-0 mt-0.5 text-muted">{s.reason}</p>}
          <dl className="m-0 mt-1 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
            {s.cashMin && (
              <>
                <dt className="text-muted">이 안 실행 뒤 최저 현금</dt>
                <dd className="m-0 tabular-nums">{s.cashMin}</dd>
              </>
            )}
            {s.stressCashMin && (
              <>
                <dt className="text-muted">회수가 늦어질 때 최저 현금</dt>
                <dd className="m-0 tabular-nums">{s.stressCashMin}</dd>
              </>
            )}
            {s.cap && (
              <>
                <dt className="text-muted">매입에 쓸 수 있는 한도</dt>
                <dd className="m-0 tabular-nums">{s.cap}</dd>
              </>
            )}
            {s.payments.length > 0 && (
              <>
                <dt className="text-muted">지급 일정</dt>
                <dd className="m-0 tabular-nums">
                  {s.payments.map((p) => `${p.date} ${p.amount}`).join(" · ")}
                </dd>
              </>
            )}
          </dl>
          {s.adjustmentNote && <p className="m-0 mt-1 text-muted">{s.adjustmentNote}</p>}
        </div>
      ))}
    </div>
  );
}

function LogisticsDetail({ summary }: { summary: LogisticsSummary }) {
  return (
    <dl className="m-0 mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 border-t border-line-soft pt-2 text-[12.5px]">
      {summary.scenarios.length > 0 && (
        <>
          <dt className="text-muted">안별 판정</dt>
          <dd className="m-0">{summary.scenarios.map((s) => `${s.name} ${s.status}`).join(" · ")}</dd>
        </>
      )}
      {summary.arrivals.length > 0 && (
        <>
          <dt className="text-muted">도착 예정일</dt>
          <dd className="m-0 tabular-nums">{summary.arrivals.join(" · ")}</dd>
        </>
      )}
      {summary.freeByDate.length > 0 && (
        <>
          <dt className="text-muted">도착일 창고 여유</dt>
          <dd className="m-0 tabular-nums">
            {summary.freeByDate.map((d) => `${d.date} ${d.free}`).join(" · ")}
          </dd>
        </>
      )}
      {summary.checks.length > 0 && (
        <>
          <dt className="text-muted">점검 항목</dt>
          <dd className="m-0">
            <ul className="m-0 list-none p-0">
              {summary.checks.map((c) => (
                <li key={c.name}>
                  {c.name}
                  <span className={`ml-1.5 ${c.attention ? "text-warn" : "text-muted"}`}>
                    {c.status}
                  </span>
                </li>
              ))}
            </ul>
          </dd>
        </>
      )}
    </dl>
  );
}
