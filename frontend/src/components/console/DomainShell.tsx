"use client";

import { Pill, TabButtons } from "@/components/console/Blocks";

export function RunContextBar({
  asOf,
  source,
}: {
  asOf: string;
  source: { filled: boolean; owner: string; note: string | null };
}) {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-xl border bg-panel px-4 py-3 text-[11.5px]" style={{ borderColor: "var(--color-hair)" }}>
      <Pill text={source.filled ? "LIVE" : "PREVIEW"} tone={source.filled ? "good" : "sim"} />
      <span><b>as_of</b> <span className="font-mono">{asOf}</span></span>
      <span><b>sim_run_id</b> <span className="font-mono">API 미제공</span></span>
      <span><b>policy</b> API 미제공</span>
      <span><b>data</b> {source.note ?? `${source.owner} 조회 결과`}</span>
    </div>
  );
}

export function DomainHeader<T extends string>({
  title,
  tabs,
  active,
  onChange,
}: {
  title: string;
  tabs: { key: T; label: string }[];
  active: T;
  onChange: (tab: T) => void;
}) {
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-end justify-between gap-3">
        <div><p className="m-0 text-[11px] font-semibold tracking-[0.14em] text-ink2">OPERATIONS</p><h2 className="mb-0 mt-1 text-[22px] font-semibold">{title}</h2></div>
      </div>
      <TabButtons items={tabs} value={active} onChange={onChange} />
    </div>
  );
}

export function NotProvisioned({ title, detail }: { title: string; detail: string }) {
  return <section className="rounded-xl border border-dashed bg-panel px-5 py-10 text-center" style={{ borderColor: "var(--color-hair)" }}><p className="m-0 text-[15px] font-semibold">{title} 데이터 미구축</p><p className="mb-0 mt-2 text-[12px] text-ink2">{detail}</p></section>;
}
