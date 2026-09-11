"use client";

import { useState, useSyncExternalStore } from "react";
import { CardBlock, DataTable, ErrorBox, Loading, Panel, StatRow, TabButtons } from "@/components/console/Blocks";
import { DomainHeader, NotProvisioned, RunContextBar } from "@/components/console/DomainShell";
import { FinanceCashChart } from "@/app/console/finance/FinanceCashChart";
import { useTab } from "@/components/console/useTab";
import { asOfSnapshot, serverAsOf, subscribeAsOf } from "@/lib/demo_as_of";
import { finance, type FinanceTab } from "@/lib/screen";

type Tab = "overview" | "cash" | "ledger" | "cost" | "loans" | "statements" | "reports" | "runs";
const TABS: { key: Tab; label: string }[] = [{ key: "overview", label: "재무 현황" }, { key: "cash", label: "자금 흐름" }, { key: "ledger", label: "채권 · 채무" }, { key: "cost", label: "비용" }, { key: "loans", label: "차입" }, { key: "statements", label: "경영용 재무현황" }, { key: "reports", label: "보고서" }, { key: "runs", label: "실행 이력" }];

export default function FinancePage() {
  const asOf = useSyncExternalStore(subscribeAsOf, asOfSnapshot, serverAsOf);
  const { data, error } = useTab<FinanceTab>(asOf, () => finance(asOf));
  const [tab, setTab] = useState<Tab>("overview");
  if (error) return <ErrorBox message={error} />;
  if (!data) return <Loading what="재무" />;
  return <div className="mx-auto flex w-full max-w-[1400px] flex-col gap-4 sm:gap-5"><DomainHeader title="재무" tabs={TABS} active={tab} onChange={setTab} /><RunContextBar asOf={data.requested_as_of} source={data.source} />{!data.has_data ? <NotProvisioned title="재무" detail="선택한 기준일에 저장된 재무 상태가 없습니다." /> : <Body data={data} tab={tab} />}</div>;
}

function Body({ data, tab }: { data: FinanceTab; tab: Tab }) {
  if (tab === "overview") return <><p className="m-0 text-[12px] text-ink2">{data.read_only.text}</p><StatRow items={data.stats} />{data.action_card && <CardBlock card={data.action_card} />}<Panel title="Finance Agent" subtitle="사실 → 결정론적 판정 → AI 설명 순서"><div className="grid gap-2 sm:grid-cols-3">{["Runtime", "Verdict", "LLM Status"].map((label) => <div key={label} className="rounded-lg border p-3"><span className="text-[11px] text-ink2">{label}</span><b className="mt-1 block font-mono text-[12px]">API 미제공</b></div>)}</div><Section title="Deterministic Result" text="현재 Finance 조회 API는 집계·현금흐름만 제공합니다." /><Section title="Evidence" text={data.source.note ?? "재무 장부"} /><Section title="AI Interpretation" text="실행별 LLM 설명 API가 아직 화면 계약에 없습니다." /></Panel></>;
  if (tab === "cash") return <><Panel title="자금 흐름" subtitle="백엔드가 만든 projection과 minimum cash line"><TabButtons items={[{ key: "today", label: "오늘" }, { key: "week", label: "7일" }, { key: "month", label: "30일" }]} value="month" onChange={() => undefined} /><p className="m-0 text-[11.5px] text-ink2">현재 API는 30일 projection만 제공합니다. 프론트에서 기간별 현금을 재계산하지 않습니다.</p>{data.cash_chart && <FinanceCashChart chart={data.cash_chart} availableStates={data.states.map((s) => s.key)} />}</Panel><Panel title="예정 현금 흐름"><div className="grid gap-3 sm:grid-cols-2">{data.flows.map((flow) => <div key={flow.label} className="rounded-lg border p-3"><span className="text-[11.5px] text-ink2">{flow.label}</span><b className="mt-1 block tabular-nums">{flow.value}</b></div>)}</div></Panel></>;
  if (tab === "ledger") return <><Panel title="AR Aging" subtitle="Finance 장부 정본"><StatRow items={data.balances} />{data.closings ? <DataTable table={data.closings} /> : <NotProvisioned title="매출채권 상세" detail="채권 단위 Aging API가 아직 없습니다." />}</Panel><NotProvisioned title="매입채무" detail="Payables 상세 DTO/API가 아직 화면에 제공되지 않습니다." /></>;
  if (tab === "statements") return <Panel title="경영용 재무현황" subtitle="정식 기업회계 재무제표가 아닌 운영 장부 집계입니다."><TabButtons items={[{ key: "pnl", label: "손익" }, { key: "position", label: "재무상태" }, { key: "cashflow", label: "현금흐름" }]} value="cashflow" onChange={() => undefined} />{data.closings && <DataTable table={data.closings} />}</Panel>;
  return <NotProvisioned title={TABS.find((item) => item.key === tab)?.label ?? ""} detail="이 영역의 authoritative read API가 아직 없습니다. 임의 숫자나 보고서를 생성하지 않습니다." />;
}
function Section({ title, text }: { title: string; text: string }) { return <div className="border-t pt-3"><b className="text-[12px]">{title}</b><p className="mb-0 mt-1 text-[12px] text-ink2">{text}</p></div>; }
