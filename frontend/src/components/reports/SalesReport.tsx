"use client";

import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { Metric, ReportChrome, Section, Table } from "./ReportChrome";
import { date, kg, record, rows, text, won, type ReportFacts } from "./reportFormat";

export function SalesReport({ facts }: { facts: ReportFacts }) {
  const proposals = record(facts.proposals);
  const proposalRows = rows(proposals.rows);
  const trend = record(facts.trend);
  const trendRows = rows(trend.rows);
  const confirmed = rows(facts.confirmed_sales);
  return <div className="space-y-5">
    <ReportChrome title="판매 운영 보고서" subtitle="판매 후보와 실적" facts={facts} page={1}><div className="grid grid-cols-5 gap-3"><Metric label="제안 가능" value={text(proposals.presentable_count, "0")} detail="PRESENTABLE" /><Metric label="검토 필요" value={text(proposals.review_required_count, "0")} detail="REVIEW_REQUIRED" /><Metric label="미해결" value={text(proposals.unresolved_count, "0")} detail="UNRESOLVED" /><Metric label="제외" value={text(proposals.rejected_count, "0")} detail="REJECTED" /><Metric label="확정 판매" value={text(facts.confirmed_count, "0")} detail="CONFIRMED / DELIVERED" /></div><Section title="기간 Sales Trend"><div className="h-64 rounded-lg border border-[#dbe7e0] p-3"><ResponsiveContainer width="100%" height="100%"><LineChart data={trendRows}><XAxis dataKey="sale_date" tick={{ fontSize: 14 }} /><YAxis tick={{ fontSize: 14 }} /><Tooltip /><Line type="monotone" dataKey="sales_amount_krw" stroke="#1d6b48" dot={false} /></LineChart></ResponsiveContainer></div></Section></ReportChrome>
    <ReportChrome title="판매 운영 보고서" subtitle="금일 판매안" facts={facts} page={2}><Section title="판매 후보"><Table headers={["전략", "품목", "거래처", "수량", "단가", "매출", "재무", "표시 상태", "승인 차단"]}>{proposalRows.map((row) => <tr key={text(row.scenario_id)} className="border-t border-[#e6eee9]"><td>{text(row.scenario_type)}</td><td>{text(row.item)}</td><td>{text(row.partner_id)}</td><td>{kg(row.quantity_kg)}</td><td>{won(row.unit_price_krw)}</td><td>{won(row.reported_sales_amount_krw)}</td><td>{text(row.finance_verdict)}</td><td>{text(row.presentation_state)}</td><td>{text(row.approval_blocked)}</td></tr>)}</Table></Section></ReportChrome>
    <ReportChrome title="판매 운영 보고서" subtitle="확정 판매" facts={facts} page={3}><Section title="실제 sale_status가 CONFIRMED 또는 DELIVERED인 판매"><Table headers={["품목", "거래처", "수량", "매출", "상태", "납품일"]}>{confirmed.map((row) => <tr key={text(row.scenario_id)} className="border-t border-[#e6eee9]"><td>{text(row.item)}</td><td>{text(row.partner_id)}</td><td>{kg(row.quantity_kg)}</td><td>{won(row.reported_sales_amount_krw)}</td><td>{text(row.sale_status)}</td><td>{date(row.delivery_date)}</td></tr>)}</Table></Section></ReportChrome>
    <ReportChrome title="판매 운영 보고서" subtitle="전략 · 검증" facts={facts} page={4}><Section title="전략과 검증"><Table headers={["안", "Runtime", "Finance", "Presentation", "LLM", "미판정 사유"]}>{proposalRows.map((row) => { const strategy = record(row.strategy); return <tr key={text(row.scenario_id)} className="border-t border-[#e6eee9]"><td>{text(row.scenario_type, text(row.scenario_id))}</td><td>{text(row.status)}</td><td>{text(row.finance_verdict)}</td><td>{text(row.presentation_state)}</td><td>{text(strategy.llm_status)}</td><td>{Array.isArray(row.unresolved_reason_codes) ? row.unresolved_reason_codes.join(", ") || "—" : "—"}</td></tr>; })}</Table></Section><Section title="기간 추이"><Table headers={["일자", "판매 건수", "판매량", "매출", "공헌이익"]}>{trendRows.map((row) => <tr key={text(row.sale_date)} className="border-t border-[#e6eee9]"><td>{date(row.sale_date)}</td><td>{text(row.sales_count, "0")}</td><td>{kg(row.quantity_kg)}</td><td>{won(row.sales_amount_krw)}</td><td>{won(row.contribution_profit_krw)}</td></tr>)}</Table></Section></ReportChrome>
  </div>;
}
