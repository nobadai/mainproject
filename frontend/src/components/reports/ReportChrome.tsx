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
  const availableStart = facts.available_start_date;
  const availableEnd = facts.available_end_date;
  const requestedAndAvailableDiffer = Boolean(availableStart && availableEnd && (availableStart !== facts.start_date || availableEnd !== facts.end_date));
  //  🔴 **모를 때 「PREVIEW」라고 적지 않는다.** 이 칸의 값은 `sim_runs.run_type` 이고
  //     그 값 공간에 `PREVIEW` 는 없다 (`WALK` · `BURN_IN`). 게다가 이 저장소에서
  //     「PREVIEW」는 이미 **「부서가 실제 값에 안 붙은 예시값」** 이라는 다른 뜻으로
  //     쓰인다 (`console/DomainShell.tsx`) — 진짜 원장 숫자에 그 딱지를 붙이게 된다.
  //     세 보고서가 같은 머리말을 쓰므로 재무·판매도 같은 거짓 설명을 하고 있었다
  //     (`sim_run_id` 가 `sim_runs` 에 없으면 `data_type` 이 `null` 이 된다).
  const mode = facts.data_mode ? String(facts.data_mode) : "실행 모드 미확인";
  return (
    <article data-report-page className="report-page bg-white text-[#16241f] shadow-xl print:shadow-none">
      <div className="h-2 bg-[#0e2419]" />
      <header className="border-b border-[#dce7e1] px-10 py-6">
        <p className="m-0 text-[10px] font-bold tracking-[0.25em] text-[#5a9c77]">HAETDEUL OPERATIONS REPORT</p>
        <div className="mt-2 flex items-end justify-between gap-5">
          <div><h2 className="m-0 text-2xl font-extrabold">{title}</h2><p className="m-0 mt-1 text-sm text-[#668076]">{subtitle}</p></div>
          <dl className="m-0 grid grid-cols-2 gap-x-5 gap-y-1 text-right text-[11px] text-[#587067]">
            <dt>보고 기간</dt><dd>{String(period ?? "—")}</dd><dt>기준일 (as_of)</dt><dd>{String(facts.as_of ?? "—")}</dd><dt>기준 실행</dt><dd>{String(facts.sim_run_id ?? "—")}</dd><dt>데이터 모드</dt><dd>{mode}</dd>
          </dl>
        </div>
      </header>
      {requestedAndAvailableDiffer && <div className="mx-10 mt-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-900">요청 기간: {String(period)} · 사용 가능 데이터: {String(availableStart)} ~ {String(availableEnd)}. 데이터가 없는 기간은 보고서에 표시되지 않습니다.</div>}
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
