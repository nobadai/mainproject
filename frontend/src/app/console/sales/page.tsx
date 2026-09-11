"use client";

import { useState, useSyncExternalStore } from "react";
import { CardBlock, ErrorBox, Loading, Panel, StatRow } from "@/components/console/Blocks";
import { DomainHeader, NotProvisioned, RunContextBar } from "@/components/console/DomainShell";
import { useTab } from "@/components/console/useTab";
import { asOfSnapshot, serverAsOf, subscribeAsOf } from "@/lib/demo_as_of";
import { sales, type SalesTab } from "@/lib/screen";

type Tab = "overview" | "agent" | "partners" | "orders" | "collections" | "analysis" | "reports" | "runs";
const TABS: { key: Tab; label: string }[] = [{ key: "overview", label: "판매 현황" }, { key: "agent", label: "Agent 판단" }, { key: "partners", label: "거래처" }, { key: "orders", label: "주문 · 판매" }, { key: "collections", label: "수금" }, { key: "analysis", label: "거래처 분석" }, { key: "reports", label: "보고서" }, { key: "runs", label: "실행 이력" }];

export default function SalesPage() {
  const asOf = useSyncExternalStore(subscribeAsOf, asOfSnapshot, serverAsOf);
  const { data, error } = useTab<SalesTab>(asOf, () => sales(asOf)); const [tab, setTab] = useState<Tab>("overview");
  if (error) return <ErrorBox message={error} />; if (!data) return <Loading what="판매" />;
  return <div className="mx-auto flex w-full max-w-[1400px] flex-col gap-4 sm:gap-5"><DomainHeader title="판매" tabs={TABS} active={tab} onChange={setTab} /><RunContextBar asOf={asOf} source={data.source} /><Body data={data} tab={tab} /></div>;
}
function Body({ data, tab }: { data: SalesTab; tab: Tab }) {
  const [action, ...details] = data.cards;
  if (tab === "overview") return <><p className="m-0 text-[12px] text-ink2">{data.read_only.text}</p><StatRow items={data.stats} />{action && <CardBlock card={action} />}{details.map((card) => <CardBlock key={card.key} card={card} />)}</>;
  if (tab === "orders") return <><Panel title="판매 Lifecycle" subtitle="각 단계는 backend가 제공하는 저장 상태만 표시합니다."><div className="thin-scroll flex gap-2 overflow-x-auto">{["Candidate", "Finance", "Logistics", "Master", "Sale", "Outbound", "Receivable", "Collection"].map((step) => <span key={step} className="shrink-0 rounded-lg border px-3 py-2 text-[12px]">{step}<b className="ml-2 font-mono text-[10px] text-ink2">API 미제공</b></span>)}</div></Panel>{details.find((card) => card.key === "recent") && <CardBlock card={details.find((card) => card.key === "recent")!} />}</>;
  if (tab === "collections") { const card = details.find((item) => item.key === "ar"); return card ? <CardBlock card={card} /> : <NotProvisioned title="수금" detail="Finance 매출채권 조회 결과가 없습니다." />; }
  if (tab === "agent") return <><Panel title="Sales Decision Center" subtitle="실제 Sales/Master API 응답만 표시합니다."><p className="m-0 text-[12px] text-ink2">거래처·품목·요청 수량·희망 가격·납품일·결제조건을 받는 typed Sales API가 아직 없습니다. 원가·여신·물류·마진·SL 코드는 프론트에서 계산하지 않습니다.</p><button disabled className="mt-3 rounded-lg border px-3 py-2 text-[12px] opacity-50">후보 생성 API 미연결</button></Panel><Panel title="Master end code"><p className="m-0 text-[12px] leading-6 text-ink2"><b>SL1_PRESENTED</b> 판매 가능한 안이 있습니다<br /><b>SL3_ALL_REJECTED</b> 후보가 있었으나 검증에서 모두 탈락했습니다<br /><b>SL6_VALIDATION_UNRESOLVED</b> 필요 검증이 완료되지 않았습니다</p><p className="mb-0 text-[11px] text-ink2">코드는 실제 API 응답이 연결될 때만 표시됩니다.</p></Panel></>;
  return <NotProvisioned title={TABS.find((item) => item.key === tab)?.label ?? ""} detail={tab === "partners" ? "Partner 목록·상세 read API와 신규 등록 schema/API/validation이 필요합니다." : "이 영역의 authoritative API가 아직 없습니다. frontend-only 저장은 하지 않습니다."} />;
}
