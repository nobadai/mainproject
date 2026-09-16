import type { ReactNode } from "react";

export function ReportChrome({
  title,
  subtitle,
  facts,
  page,
  children,
}: {
  title: string;
  subtitle: string;
  facts: Record<string, unknown>;
  page: number;
  children: ReactNode;
}) {
  const period = facts.start_date === facts.end_date ? facts.end_date : `${facts.start_date} ~ ${facts.end_date}`;
  return (
    <article data-report-page className="report-page bg-white text-[#16241f] shadow-xl print:shadow-none">
      <div className="h-2 bg-[#0e2419]" />
      <header className="border-b border-[#dce7e1] px-10 py-6">
        <p className="m-0 text-[10px] font-bold tracking-[0.25em] text-[#5a9c77]">HAETDEUL OPERATIONS REPORT</p>
        <div className="mt-2 flex items-end justify-between gap-5">
          <div><h2 className="m-0 text-2xl font-extrabold">{title}</h2><p className="m-0 mt-1 text-sm text-[#668076]">{subtitle}</p></div>
          <dl className="m-0 grid grid-cols-2 gap-x-5 gap-y-1 text-right text-[11px] text-[#587067]">
            <dt>보고기간</dt><dd>{String(period ?? "—")}</dd><dt>기준일</dt><dd>{String(facts.as_of ?? "—")}</dd><dt>기준 실행</dt><dd>{String(facts.sim_run_id ?? "—")}</dd>
          </dl>
        </div>
      </header>
      <main className="px-10 py-7">{children}</main>
      <footer className="mt-auto flex justify-between border-t border-[#dce7e1] px-10 py-3 text-[10px] text-[#71867d]">
        <span>기존 Domain read model 기준 · 업무 계산 없음</span><span>{page} / 4</span>
      </footer>
    </article>
  );
}

export function Metric({ label, value, detail }: { label: string; value: string; detail?: string }) {
  return <div className="rounded-lg border border-[#dbe7e0] bg-[#f4f8f6] p-4"><p className="m-0 text-[11px] font-semibold text-[#5f776d]">{label}</p><p className="m-0 mt-2 text-xl font-bold">{value}</p>{detail && <p className="m-0 mt-1 text-[11px] text-[#70857b]">{detail}</p>}</div>;
}

export function Section({ title, children }: { title: string; children: ReactNode }) {
  return <section className="mt-5"><h3 className="mb-2 text-sm font-bold text-[#234638]">{title}</h3>{children}</section>;
}

export function Table({ headers, children }: { headers: string[]; children: ReactNode }) {
  return <div className="overflow-hidden rounded-lg border border-[#dbe7e0]"><table className="w-full border-collapse text-left text-[11px]"><thead className="bg-[#edf5f0]"><tr>{headers.map((header) => <th key={header} className="px-2 py-2 font-semibold">{header}</th>)}</tr></thead><tbody>{children}</tbody></table></div>;
}
