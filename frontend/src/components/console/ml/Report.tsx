"use client";

/**
 * Agent report — verdict badge, one-line finding, the numbers behind it.
 *
 * ★ **The screen never re-decides the verdict.** OK / Caution / Problem is
 *   settled by the agent and the screen only paints it. Once a screen starts
 *   applying its own thresholds, something the agent called a Problem can end
 *   up green here.
 *
 * ★ **The numbers ride along.** If all you see is the word "Problem", the only
 *   thing a person can do is believe it.
 *
 * Ported from `AgentPanel` on our ML console (`localhost:3100`).
 */

import type { AgentReport, Finding } from "@/lib/mlConsole";

import { en, VERDICT } from "./labels";

//  키는 백엔드가 보내는 한글 그대로 둡니다 — 영어로 바꾸는 것은 보이는 글자뿐.
const TONE: Record<string, { fg: string; bg: string }> = {
  OK: { fg: "var(--color-t-good)", bg: "var(--color-t-good-bg)" },
  Caution: { fg: "var(--color-t-warn)", bg: "var(--color-t-warn-bg)" },
  Problem: { fg: "var(--color-t-bad)", bg: "var(--color-t-bad-bg)" },
};

export function Verdict({ level }: { level: string }) {
  const t = TONE[level] ?? { fg: "var(--color-mut)", bg: "var(--color-sunk)" };
  return (
    <span
      className="inline-flex shrink-0 items-center gap-1.5 rounded px-2 py-0.5 text-[11px] font-semibold"
      style={{ color: t.fg, background: t.bg }}
    >
      <i aria-hidden className="inline-block size-1.5 rounded-full" style={{ background: t.fg }} />
      {en(VERDICT, level)}
    </span>
  );
}

export function FindingCard({ f, compact = false }: { f: Finding; compact?: boolean }) {
  const nums = f.numbers ?? [];
  return (
    <li
      className={`list-none rounded-lg border bg-panel ${compact ? "p-3" : "p-3.5"}`}
      style={{ borderColor: "var(--color-hair)" }}
    >
      <div className="flex items-start gap-2.5">
        {f.level && <Verdict level={f.level} />}
        <p className="m-0 flex-1 text-[13px] font-semibold leading-snug">{f.title}</p>
      </div>
      {f.detail && (
        <p
          className="m-0 mt-2 whitespace-pre-line text-[12px] leading-relaxed"
          style={{ color: "var(--color-mut)" }}
        >
          {f.detail}
        </p>
      )}
      {nums.length > 0 && (
        <dl
          className="mt-2.5 grid grid-cols-[minmax(0,1fr)_auto] gap-x-4 gap-y-1 border-t pt-2.5"
          style={{ borderColor: "var(--color-hair-soft)" }}
        >
          {nums.map(([k, v], i) => (
            <div key={i} className="contents">
              <dt className="m-0 truncate text-[11.5px]" style={{ color: "var(--color-mut)" }}>
                {k}
              </dt>
              <dd className="tabular m-0 text-right font-mono text-[11.5px]">{v}</dd>
            </div>
          ))}
        </dl>
      )}
      {f.advice && (
        <p
          className="m-0 mt-2.5 rounded px-2.5 py-1.5 text-[11.5px]"
          style={{ background: "var(--color-sunk)", color: "var(--color-t-good)" }}
        >
          → {f.advice}
        </p>
      )}
    </li>
  );
}

export function ReportBody({ report, subtitle }: { report: AgentReport; subtitle?: string }) {
  return (
    <section className="flex flex-col gap-2.5">
      <header className="flex flex-wrap items-center gap-2">
        <Verdict level={report.verdict} />
        <h3 className="m-0 text-[13.5px] font-semibold">{report.name}</h3>
        {subtitle && (
          <span className="text-[11.5px]" style={{ color: "var(--color-mut)" }}>
            {subtitle}
          </span>
        )}
        {report.at && (
          <span className="ml-auto font-mono text-[11px]" style={{ color: "var(--color-mut2)" }}>
            {report.at}
          </span>
        )}
      </header>
      {report.findings.length === 0 ? (
        <p className="m-0 text-[12px]" style={{ color: "var(--color-mut2)" }}>
          보고할 내용이 없습니다 — 데이터와 시스템에 아무 문제가 없다는 뜻입니다.
        </p>
      ) : (
        <ul className="m-0 flex list-none flex-col gap-2 p-0">
          {report.findings.map((f, i) => (
            <FindingCard key={i} f={f} />
          ))}
        </ul>
      )}
    </section>
  );
}
