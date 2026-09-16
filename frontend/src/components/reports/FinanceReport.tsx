"use client";

import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { Metric, ReportChrome, Section, Table } from "./ReportChrome";
import { date, record, rows, text, won, type ReportFacts } from "./reportFormat";

export function FinanceReport({ facts }: { facts: ReportFacts }) {
  const summary = record(facts.summary);
  const receivables = record(facts.receivables);
  const payables = record(facts.payables);
  const expenses = record(facts.expenses);
  const credit = record(facts.credit);
  const states = rows(summary.states);
  const closings = rows(facts.closings);
  const expenseRows = rows(expenses.rows);
  const creditRows = rows(credit.partners);
  const receivableSummary = record(receivables.summary);
  const payableSummary = record(payables.summary);
  const expenseSummary = record(expenses.summary);
  return <div className="space-y-5">
    <ReportChrome title="재무 운영 보고서" subtitle="재무 상태와 현금 흐름" facts={facts} page={1}>
      <div className="grid grid-cols-4 gap-3">
        {states.map((state) => <Metric key={text(state.financing_mode)} label="현재 현금" value={won(state.current_cash_krw)} detail={`${text(state.financing_mode)} · 기준일 현재`} />)}
        <Metric label="받을 돈" value={won(receivableSummary.total_outstanding_krw)} detail="아직 받을 금액" />
        <Metric label="지급할 돈" value={won(payableSummary.total_outstanding_krw)} detail="지급 예정 금액" />
        <Metric label="미지급 비용" value={won(expenseSummary.accrued_krw)} detail={`${text(expenseSummary.accrued_count, "0")}건 · 아직 지급 전`} />
      </div>
      <Section title="기간 Cash Trend"><div className="h-64 rounded-lg border border-[#dbe7e0] p-3"><ResponsiveContainer width="100%" height="100%"><LineChart data={closings}><XAxis dataKey="close_date" tick={{ fontSize: 10 }} /><YAxis tick={{ fontSize: 10 }} /><Tooltip /><Line type="monotone" dataKey="base_net_cash_krw" stroke="#1d6b48" dot={false} /></LineChart></ResponsiveContainer></div></Section>
      <Section title="주요 상태"><div className="grid grid-cols-2 gap-3">{states.map((state) => <div key={text(state.financing_mode)} className="rounded-lg border border-[#dbe7e0] p-3 text-sm"><b>{text(state.financing_mode)}</b><br />운영 여유 {won(state.operating_cash_buffer_krw)} · 차입 {won(state.current_debt_krw)}</div>)}</div></Section>
    </ReportChrome>
    <ReportChrome title="재무 운영 보고서" subtitle="현금 흐름" facts={facts} page={2}><Section title="일별 현금 흐름"><Table headers={["일자", "수금", "매입", "물류", "급여·이자", "운영비 유출", "순현금"]}>{closings.map((row) => <tr key={text(row.close_date)} className="border-t border-[#e6eee9]"><td className="px-2 py-2">{date(row.close_date)}</td><td>{won(row.collection_cash_in_krw)}</td><td>{won(row.purchase_cash_out_krw)}</td><td>{won(row.logistics_cash_out_krw)}</td><td>{won(row.payroll_interest_cash_out_krw)}</td><td>{won(row.operating_expense_cash_out_krw)}</td><td>{won(row.base_net_cash_krw)}</td></tr>)}</Table></Section></ReportChrome>
    <ReportChrome title="재무 운영 보고서" subtitle="채권 · 채무 · 여신" facts={facts} page={3}><div className="grid grid-cols-2 gap-4"><Section title="미수금 Aging"><Table headers={["CURRENT", "1~7일", "8~30일", "30일+"]}><tr><td>{won(receivableSummary.current_krw)}</td><td>{won(receivableSummary.days_1_7_krw)}</td><td>{won(receivableSummary.days_8_30_krw)}</td><td>{won(receivableSummary.days_30_plus_krw)}</td></tr></Table></Section><Section title="미지급금"><Table headers={["지급할 돈", "오늘 지급", "7일 내", "연체"]}><tr><td>{won(payableSummary.total_outstanding_krw)}</td><td>{won(payableSummary.due_today_krw)}</td><td>{won(payableSummary.due_next_7d_krw)}</td><td>{won(payableSummary.overdue_krw)}</td></tr></Table></Section></div><Section title="거래처 여신"><Table headers={["거래처", "한도", "현재 미수", "가용 여신", "결제 예정"]}>{creditRows.map((row) => <tr key={text(row.partner_id)} className="border-t border-[#e6eee9]"><td>{text(row.partner_name, text(row.partner_id))}</td><td>{won(row.credit_limit_krw)}</td><td>{won(row.current_ar_krw)}</td><td>{won(row.available_credit_krw)}</td><td>{date(row.expected_credit_recovery_date)}</td></tr>)}</Table></Section></ReportChrome>
    <ReportChrome title="재무 운영 보고서" subtitle="Expense" facts={facts} page={4}><div className="grid grid-cols-3 gap-3"><Metric label="ACCRUED" value={won(expenseSummary.accrued_krw)} /><Metric label="PAID" value={won(expenseSummary.paid_krw)} /><Metric label="CANCELLED" value={won(expenseSummary.cancelled_krw)} /></div><Section title="최근 Expense"><Table headers={["Expense", "분류", "금액", "발생일", "지급 예정", "지급일", "상태", "근거"]}>{expenseRows.map((row) => <tr key={text(row.expense_id)} className="border-t border-[#e6eee9]"><td>{text(row.expense_id)}</td><td>{text(row.display_category, text(row.raw_category))}</td><td>{won(row.amount_krw)}</td><td>{date(row.expense_date)}</td><td>{date(row.due_date)}</td><td>{row.status === "PAID" && !row.paid_date_known ? "지급일 미상" : date(row.paid_date)}</td><td>{text(row.status)}</td><td>{text(row.source_ref)}</td></tr>)}</Table></Section></ReportChrome>
  </div>;
}
