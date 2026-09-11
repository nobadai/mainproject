"use client";

/**
 * 재무 운영 콘솔.
 *
 * 🔴 **legacy `/api/screen/finance` 를 더 이상 부르지 않는다.** 그 계약은 문장·색·표를
 *    미리 만들어 주는 옛 화면용이고, 실행 축(`sim_run_id`)이 없다. 운영 콘솔은
 *    `/api/console/finance/…` 만 쓴다 — 옛 API 자체는 남겨 둔다.
 *
 * 🔴 **화면이 숫자를 만들지 않는다.** 합계·연체·마진은 백엔드가 낸 값을 적기만 한다.
 */

import { useState, useSyncExternalStore } from "react";

import { Panel } from "@/components/console/Blocks";
import {
  Blocked,
  EmptyRows,
  Failed,
  Metric,
  Metrics,
  NoRunSelected,
  Skeleton,
  Table,
  Unsupported,
  useConsoleData,
} from "@/components/console/ConsoleData";
import { DomainHeader } from "@/components/console/DomainShell";
import { RunPicker, useSimRun } from "@/components/console/RunPicker";
import {
  AGING_LABELS,
  financeConsole,
  money,
  type FinanceCashflowResponse,
  type FinanceRun,
  type FinanceSummaryResponse,
  type PayablesResponse,
  type ExpensesResponse,
  type FinanceRunsResponse,
  type ReceivablesResponse,
} from "@/lib/console_api";
import { asOfSnapshot, serverAsOf, subscribeAsOf } from "@/lib/demo_as_of";

type Tab = "overview" | "cash" | "receivables" | "payables" | "expenses" | "loans" | "runs";
const TABS: { key: Tab; label: string }[] = [
  { key: "overview", label: "재무 현황" },
  { key: "cash", label: "자금 흐름" },
  { key: "receivables", label: "채권" },
  { key: "payables", label: "채무" },
  { key: "expenses", label: "비용" },
  { key: "loans", label: "차입" },
  { key: "runs", label: "실행 이력" },
];

export default function FinancePage() {
  const asOf = useSyncExternalStore(subscribeAsOf, asOfSnapshot, serverAsOf);
  const simRun = useSimRun();
  const [tab, setTab] = useState<Tab>("overview");
  return (
    <div className="mx-auto flex w-full max-w-[1400px] flex-col gap-4 sm:gap-5">
      <DomainHeader title="재무" tabs={TABS} active={tab} onChange={setTab} />
      <RunPicker asOf={asOf} />
      {!simRun ? <NoRunSelected /> : <Body simRun={simRun} asOf={asOf} tab={tab} />}
    </div>
  );
}

function Body({ simRun, asOf, tab }: { simRun: string; asOf: string; tab: Tab }) {
  if (tab === "overview") return <Overview simRun={simRun} asOf={asOf} />;
  if (tab === "cash") return <Cashflow simRun={simRun} asOf={asOf} />;
  if (tab === "receivables") return <Receivables simRun={simRun} asOf={asOf} />;
  if (tab === "payables") return <Payables simRun={simRun} asOf={asOf} />;
  if (tab === "expenses") return <Expenses simRun={simRun} asOf={asOf} />;
  if (tab === "loans") return <Loans simRun={simRun} asOf={asOf} />;
  return <Runs simRun={simRun} />;
}

/* ── 재무 현황 ─────────────────────────────────────────────────────────── */

function Overview({ simRun, asOf }: { simRun: string; asOf: string }) {
  const summary = useConsoleData<FinanceSummaryResponse>(
    `summary:${simRun}:${asOf}`,
    () => financeConsole.summary(simRun, asOf),
    true,
  );
  const receivables = useConsoleData<ReceivablesResponse>(
    `ar:${simRun}:${asOf}`,
    () => financeConsole.receivables(simRun, asOf),
    true,
  );
  const payables = useConsoleData<PayablesResponse>(
    `ap:${simRun}:${asOf}`,
    () => financeConsole.payables(simRun, asOf),
    true,
  );
  const latest = useConsoleData<FinanceRun | null>(
    `latest:${simRun}`,
    () => financeConsole.latestRun(simRun),
    true,
  );
  const state = summary.data?.states[0];
  return (
    <>
      <Panel title="재무 상태" subtitle="저장된 재무 상태 행 그대로 — 화면이 다시 세지 않습니다">
        {summary.loading ? (
          <Skeleton what="재무 상태" />
        ) : summary.error ? (
          <Failed what="재무 상태" message={summary.error} />
        ) : !state ? (
          <EmptyRows what="재무 상태" />
        ) : (
          <Metrics>
            <Metric label="보유 현금" value={money(state.current_cash_krw)} hint={state.financing_mode} />
            <Metric label="최소 운전자금" value={money(state.minimum_operating_cash_krw)} />
            {/* 🔴 차입 원장이 없어 authoritative 한 것은 이 잔액 하나다 */}
            <Metric label="현재 차입잔액" value={money(state.current_debt_krw)} hint={state.state_date} />
            <Metric label="재고 장부가" value={money(state.inventory_book_value_krw)} />
          </Metrics>
        )}
      </Panel>
      <Panel title="채권 · 채무" subtitle="두 장부가 낸 합계를 그대로 적습니다">
        {receivables.loading || payables.loading ? (
          <Skeleton what="재무 현황" />
        ) : receivables.error ? (
          <Failed what="매출채권" message={receivables.error} />
        ) : payables.error ? (
          <Failed what="매입채무" message={payables.error} />
        ) : (
          <Metrics>
            <Metric
              label="매출채권 잔액"
              value={money(receivables.data?.summary.total_outstanding_krw)}
            />
            <Metric
              label="연체 채권 (1일 이상)"
              value={money(receivables.data?.summary.days_1_7_krw)}
              hint="1–7일 구간"
            />
            <Metric
              label="매입채무 잔액"
              value={money(payables.data?.summary.total_outstanding_krw)}
            />
            <Metric label="연체 채무" value={money(payables.data?.summary.overdue_krw)} />
          </Metrics>
        )}
      </Panel>
      <AgentCard state={latest} />
    </>
  );
}

/**
 * Finance Agent Card — **세 계층을 한 badge 로 합치지 않는다.**
 *
 * 🔴 Runtime 은 *"돌 수 있었나"*, Verdict 는 *"업무 판정이 무엇인가"*, LLM 은
 *    *"설명을 누가 썼나"* 다. 합치면 «판정 없음» 과 «못 돌았음» 이 같은 칸이 된다.
 */
function AgentCard({ state }: { state: { data: FinanceRun | null; error: string | null; loading: boolean } }) {
  return (
    <Panel title="Finance Agent" subtitle="저장된 최신 실행을 읽습니다 — 화면이 Agent를 다시 돌리지 않습니다">
      {state.loading ? (
        <Skeleton what="최신 실행" />
      ) : state.error ? (
        <Failed what="최신 실행" message={state.error} />
      ) : !state.data ? (
        <EmptyRows what="이 실행의 Finance 실행 이력" />
      ) : (
        <>
          <div className="grid gap-2 sm:grid-cols-3">
            <Metric label="Runtime" value={state.data.runtime_status} />
            <Metric label="Verdict" value={state.data.verdict ?? "판정 없음"} />
            <Metric label="LLM Status" value={state.data.llm_status} />
          </div>
          <div className="mt-3 border-t pt-3" style={{ borderColor: "var(--color-hair)" }}>
            <b className="text-[12px]">Deterministic Result</b>
            <pre className="thin-scroll mb-0 mt-1 overflow-x-auto font-mono text-[11px] text-ink2">
              {state.data.deterministic_result
                ? JSON.stringify(state.data.deterministic_result, null, 2)
                : "이 실행에는 저장된 결정론 결과가 없습니다."}
            </pre>
          </div>
          <div className="mt-3 border-t pt-3" style={{ borderColor: "var(--color-hair)" }}>
            <b className="text-[12px]">Evidence</b>
            <p className="mb-0 mt-1 font-mono text-[11px] text-ink2">
              {state.data.evidence?.length ? state.data.evidence.join(", ") : "근거 참조 없음"}
            </p>
          </div>
          <div className="mt-3 border-t pt-3" style={{ borderColor: "var(--color-hair)" }}>
            <b className="text-[12px]">AI Interpretation</b>
            <p className="mb-0 mt-1 text-[12px] text-ink2">
              {state.data.interpretation ?? "실행별 LLM 설명은 저장되지 않습니다 — 지어내지 않습니다."}
            </p>
          </div>
        </>
      )}
    </Panel>
  );
}

/* ── 자금 흐름 ─────────────────────────────────────────────────────────── */

function Cashflow({ simRun, asOf }: { simRun: string; asOf: string }) {
  const state = useConsoleData<FinanceCashflowResponse>(
    `cash:${simRun}:${asOf}`,
    () => financeConsole.cashflow(simRun, asOf),
    true,
  );
  if (state.loading) return <Skeleton what="자금 흐름" />;
  if (state.error) return <Failed what="자금 흐름" message={state.error} />;
  const rows = state.data!.cashflow;
  return (
    <Panel title="자금 흐름" subtitle="일마감이 저장한 값입니다 — 화면에서 기간별로 다시 계산하지 않습니다">
      {rows.length === 0 ? (
        <EmptyRows what="일마감" />
      ) : (
        <Table
          rows={rows}
          columns={[
            { key: "date", label: "마감일", mono: true, render: (row) => row.close_date },
            { key: "out", label: "매입 지출", align: "right", render: (row) => money(row.purchase_cash_out_krw) },
            { key: "in", label: "수금", align: "right", render: (row) => money(row.collection_cash_in_krw) },
            { key: "net", label: "순현금", align: "right", render: (row) => money(row.base_net_cash_krw) },
            { key: "bal", label: "기말 현금", align: "right", render: (row) => money(row.loan_cash_balance_krw) },
            {
              key: "min",
              //  ⚠️ 최소 운전자금은 없을 수 있다. 0 으로 적으면 «한도 0» 으로 읽힌다.
              label: "최소 운전자금",
              align: "right",
              render: (row) => money(row.minimum_operating_cash_krw),
            },
          ]}
        />
      )}
    </Panel>
  );
}

/* ── 채권 ─────────────────────────────────────────────────────────────── */

function Receivables({ simRun, asOf }: { simRun: string; asOf: string }) {
  const state = useConsoleData<ReceivablesResponse>(
    `ar:${simRun}:${asOf}`,
    () => financeConsole.receivables(simRun, asOf),
    true,
  );
  if (state.loading) return <Skeleton what="매출채권" />;
  if (state.error) return <Failed what="매출채권" message={state.error} />;
  const data = state.data!;
  return (
    <Panel title="AR Aging" subtitle="Finance 장부 정본 · 연체 구간은 백엔드 규칙입니다">
      <Metrics>
        <Metric label="정상" value={money(data.summary.current_krw)} />
        <Metric label="1–7일" value={money(data.summary.days_1_7_krw)} />
        <Metric label="8–30일" value={money(data.summary.days_8_30_krw)} />
        <Metric label="30일 초과" value={money(data.summary.days_30_plus_krw)} />
      </Metrics>
      <div className="mt-3">
        {data.rows.length === 0 ? (
          <EmptyRows what="매출채권" />
        ) : (
          <Table
            rows={data.rows}
            columns={[
              { key: "id", label: "채권", mono: true, render: (row) => row.receivable_id },
              { key: "partner", label: "거래처", render: (row) => row.partner_name ?? row.partner_id ?? "미지정" },
              { key: "due", label: "만기", mono: true, render: (row) => row.due_date },
              { key: "bucket", label: "구간", render: (row) => AGING_LABELS[row.aging_bucket] },
              {
                key: "overdue",
                label: "연체일",
                align: "right",
                render: (row) => (row.days_overdue === null ? "—" : `${row.days_overdue}일`),
              },
              {
                key: "amount",
                label: "잔액",
                align: "right",
                render: (row) => money(row.outstanding_amount_krw),
              },
            ]}
          />
        )}
      </div>
    </Panel>
  );
}

/* ── 채무 ─────────────────────────────────────────────────────────────── */

function Payables({ simRun, asOf }: { simRun: string; asOf: string }) {
  const state = useConsoleData<PayablesResponse>(
    `ap:${simRun}:${asOf}`,
    () => financeConsole.payables(simRun, asOf),
    true,
  );
  if (state.loading) return <Skeleton what="매입채무" />;
  if (state.error) return <Failed what="매입채무" message={state.error} />;
  const data = state.data!;
  return (
    <Panel title="매입채무" subtitle="기준일로 잰 만기 — 오늘 시계가 아니라 실행의 as_of 입니다">
      <Metrics>
        <Metric label="총 잔액" value={money(data.summary.total_outstanding_krw)} />
        <Metric label="오늘 만기" value={money(data.summary.due_today_krw)} />
        <Metric label="7일 내 만기" value={money(data.summary.due_next_7d_krw)} />
        <Metric label="연체" value={money(data.summary.overdue_krw)} />
      </Metrics>
      <div className="mt-3">
        {data.rows.length === 0 ? (
          <EmptyRows what="매입채무" />
        ) : (
          <Table
            rows={data.rows}
            columns={[
              { key: "id", label: "채무", mono: true, render: (row) => row.payable_id },
              { key: "src", label: "매입", mono: true, render: (row) => row.purchase_id ?? "출처 없음" },
              { key: "due", label: "만기", mono: true, render: (row) => row.due_date },
              {
                key: "days",
                label: "만기까지",
                align: "right",
                render: (row) =>
                  row.days_until_due < 0 ? `${-row.days_until_due}일 초과` : `${row.days_until_due}일`,
              },
              {
                key: "amount",
                label: "잔액",
                align: "right",
                render: (row) => money(row.outstanding_amount_krw),
              },
              { key: "status", label: "상태", render: (row) => row.status },
            ]}
          />
        )}
      </div>
    </Panel>
  );
}

/* ── 비용 ─────────────────────────────────────────────────────────────── */

function Expenses({ simRun, asOf }: { simRun: string; asOf: string }) {
  const state = useConsoleData<ExpensesResponse>(
    `exp:${simRun}:${asOf}`,
    () => financeConsole.expenses(simRun, asOf),
    true,
  );
  if (state.loading) return <Skeleton what="비용" />;
  if (state.error) return <Failed what="비용" message={state.error} />;
  const data = state.data!;
  return (
    <>
      <Panel title="비용" subtitle="분류는 장부가 저장한 이름 그대로입니다 — 화면이 재분류하지 않습니다">
        <Metrics>
          <Metric label="누적 비용" value={money(data.summary.total_expenses_krw)} />
        </Metrics>
        <div className="mt-3">
          {data.summary.category_totals.length === 0 ? (
            <EmptyRows what="비용 분류" />
          ) : (
            <Table
              rows={data.summary.category_totals}
              columns={[
                { key: "label", label: "분류", render: (row) => row.display_category },
                { key: "raw", label: "저장값", mono: true, render: (row) => row.raw_category },
                { key: "count", label: "건수", align: "right", render: (row) => `${row.expense_count}건` },
                { key: "sum", label: "합계", align: "right", render: (row) => money(row.total_amount_krw) },
              ]}
            />
          )}
        </div>
      </Panel>
      <Panel title="비용 내역">
        {data.rows.length === 0 ? (
          <EmptyRows what="비용" />
        ) : (
          <Table
            rows={data.rows}
            columns={[
              { key: "date", label: "일자", mono: true, render: (row) => row.expense_date },
              { key: "cat", label: "분류", render: (row) => row.display_category },
              { key: "amount", label: "금액", align: "right", render: (row) => money(row.amount_krw) },
              { key: "ref", label: "근거", mono: true, render: (row) => row.source_ref ?? "근거 없음" },
            ]}
          />
        )}
      </Panel>
    </>
  );
}

/* ── 차입 ─────────────────────────────────────────────────────────────── */

/**
 * 🔴 **차입 상세 원장이 없다.** authoritative 값은 `finance_states.current_debt_krw`
 *    수준이고, `loan_id` · 이자율 · 실행일 · 만기 · 상환 일정은 **저장소에 없다.**
 *    화면이 그것을 만들면 존재하지 않는 대출이 보고서에 실린다.
 */
function Loans({ simRun, asOf }: { simRun: string; asOf: string }) {
  return (
    <>
      <Panel title="현재 차입잔액" subtitle="재무 상태가 들고 있는 값입니다">
        <p className="m-0 text-[12px] text-ink2">
          차입잔액은 재무 현황(`/console/finance/summary`)의 재무 상태 행이 정본입니다. 이 탭은 그
          값을 다시 만들지 않습니다 — 실행 {simRun} · 기준일 {asOf}.
        </p>
      </Panel>
      <Unsupported
        what="차입 상세"
        why="loan_id · 이자율 · 실행일 · 만기 · 상환 일정을 담은 대출 원장이 아직 없습니다. 없는 값을 화면이 만들지 않습니다."
      />
    </>
  );
}

/* ── 실행 이력 ─────────────────────────────────────────────────────────── */

function Runs({ simRun }: { simRun: string }) {
  const state = useConsoleData<FinanceRunsResponse>(
    `runs:${simRun}`,
    () => financeConsole.runs(simRun),
    true,
  );
  if (state.loading) return <Skeleton what="실행 이력" />;
  if (state.error) return <Failed what="실행 이력" message={state.error} />;
  const data = state.data!;
  return (
    <Panel title="Finance 실행 이력" subtitle="이 실행에 속한 저장 기록만 — 다른 실행으로 넘어가지 않습니다">
      {data.rows.length === 0 ? (
        <>
          <EmptyRows what="Finance 실행" />
          <Blocked
            what="실행 축 연결"
            why="Finance 실행 행에는 sim_run_id 칸이 없어, 마스터가 저장한 request_id ↔ 실행 연결로 찾습니다. 마스터를 거치지 않은 실행은 어느 실행에도 속하지 않습니다."
          />
        </>
      ) : (
        <Table
          rows={data.rows}
          columns={[
            { key: "as_of", label: "기준일", mono: true, render: (row) => row.as_of },
            { key: "mode", label: "모드", render: (row) => row.mode },
            { key: "runtime", label: "Runtime", render: (row) => row.runtime_status },
            { key: "verdict", label: "Verdict", render: (row) => row.verdict ?? "판정 없음" },
            { key: "llm", label: "LLM", render: (row) => row.llm_status },
            { key: "req", label: "요청", mono: true, render: (row) => row.request_id },
          ]}
        />
      )}
    </Panel>
  );
}
