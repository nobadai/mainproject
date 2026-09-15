"use client";

/**
 * 재무 운영 콘솔.
 *
 * 🔴 **legacy `/api/screen/finance` 를 더 이상 부르지 않는다.** 그 계약은 문장·색·표를
 *    미리 만들어 주는 옛 화면용이고, 실행 축(`sim_run_id`)이 없다. 운영 콘솔은
 *    `/api/console/finance/…` 만 쓴다 — 옛 API 자체는 남겨 둔다.
 *
 * 🔴 **화면이 숫자를 만들지 않는다.** 합계·연체·마진은 백엔드가 낸 값을 적기만 한다.
 *
 * ★ **사용자가 먼저 알아야 하는 것을 먼저 놓는다** (2026-09-14). 돈이 얼마나 있고,
 *   앞으로 부족한지, 받을 돈과 줄 돈이 얼마인지가 첫 화면이다. Runtime · Verdict ·
 *   LLM 같은 내부 상태는 **기술 상세 안**으로 내렸다 — 지우지 않고 옮겼다.
 */

import { useState, useSyncExternalStore } from "react";

import { Panel } from "@/components/console/Blocks";
import {
  Blocked,
  EmptyRows,
  Failed,
  Metric,
  Metrics,
  Skeleton,
  Table,
  useConsoleData,
} from "@/components/console/ConsoleData";
import { DomainHeader } from "@/components/console/DomainShell";
import { RunPicker, useSimRun } from "@/components/console/RunPicker";
import {
  AGING_LABELS,
  financeConsole,
  type ClosingItem,
  type FinanceCashflowResponse,
  type FinanceRun,
  type FinanceSummaryResponse,
  type PayablesResponse,
  type ExpensesResponse,
  type FinanceRunsResponse,
  type ReceivablesResponse,
} from "@/lib/console_api";
import { asOfSnapshot, serverAsOf, subscribeAsOf } from "@/lib/demo_as_of";

import { AgingBars } from "./AgingBars";
import { CreditPanel } from "./CreditPanel";
import { CreditLimitForm } from "./CreditLimitForm";
import { FinanceCashChart } from "./FinanceCashChart";
import { FinanceFlowChart } from "./FinanceFlowChart";
import { DataBasis, NoRunChosen, TechDetails } from "./TechDetails";
import {
  DATA_SOURCE_NOTE,
  financingModeText,
  moneyWon,
  partnerText,
  payableStatusText,
  receivableStatusText,
  runtimeText,
  toNumber,
  verdictText,
} from "./user_text";

type Tab = "overview" | "cash" | "receivables" | "payables" | "expenses" | "loans" | "runs";
const TABS: { key: Tab; label: string }[] = [
  { key: "overview", label: "재무 현황" },
  { key: "cash", label: "자금 흐름" },
  { key: "receivables", label: "받을 돈" },
  { key: "payables", label: "줄 돈" },
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
      <DataBasis asOf={asOf} note={DATA_SOURCE_NOTE} />
      {/* 🔴 실행 축은 내부 식별자다. 고르는 자리는 남기되 기본 화면에서 내린다 —
          공용 `RunPicker` 는 고치지 않고 **위치만** 옮겼다. 아무것도 안 골랐으면
          열어 둔다. 닫아 두면 사용자가 고를 자리를 못 찾는다. */}
      <TechDetails summary={simRun ? "실행 선택 · 기술 상세" : "실행을 선택해 주세요"} open={!simRun}>
        <RunPicker asOf={asOf} />
      </TechDetails>
      {!simRun ? <NoRunChosen /> : <Body simRun={simRun} asOf={asOf} tab={tab} />}
    </div>
  );
}

/**
 * 기준일까지의 **가장 최근** 일마감.
 *
 * 🔴 **`at(-1)` 은 가장 오래된 행이다.** 백엔드 `load_recent_closings` 가
 *    `ORDER BY close_date DESC` 로 주기 때문에, 배열 끝을 «마지막 마감» 으로 읽으면
 *    2026-01-26 화면이 2026-01-15 잔액을 적는다 (실측).
 *
 * ★ 순서 계약을 믿고 `[0]` 을 쓰는 대신 **날짜로 고른다.** 정렬이 바뀌는 날에도
 *   이 화면은 틀리지 않는다. 미래 마감은 애초에 고르지 않는다.
 */
function latestClosing(rows: ClosingItem[] | undefined, asOf: string): ClosingItem | null {
  if (!rows || rows.length === 0) return null;
  let best: ClosingItem | null = null;
  for (const row of rows) {
    if (row.close_date > asOf) continue;
    if (best === null || row.close_date > best.close_date) best = row;
  }
  return best;
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
  const [creditRefresh, setCreditRefresh] = useState(0);
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
  //  🔴 배열 끝이 아니라 **날짜로** 고른다 — 백엔드가 최신부터 주기 때문이다.
  const closing = latestClosing(summary.data?.recent_closings, asOf);

  return (
    <>
      <Panel
        title="지금 돈이 얼마나 있나"
        subtitle="저장된 재무 상태와 일마감을 그대로 적습니다"
      >
        {summary.loading ? (
          <Skeleton what="재무 상태" />
        ) : summary.error ? (
          <Failed what="재무 상태" message={summary.error} />
        ) : !state ? (
          <EmptyRows what="재무 상태" />
        ) : (
          <>
            {/* 🔴 **두 기준일을 한 줄에 섞지 않는다.** 재무 상태와 일마감은 서로 다른
                날짜를 가질 수 있고, 사용자는 같은 시점 숫자로 읽는다. 묶음을 나누고
                각 묶음이 어느 날짜의 값인지 제목에 적는다. */}
            <BasisGroup title="재무 상태" basis={state.state_date}>
              <Metric label="현재 현금" value={moneyWon(state.current_cash_krw)} />
              <Metric label="최소 운영현금" value={moneyWon(state.minimum_operating_cash_krw)} />
            </BasisGroup>
            {closing ? (
              <BasisGroup title="일마감" basis={closing.close_date}>
                <Metric label="대출 제외 현금" value={moneyWon(closing.base_cash_balance_krw)} />
                <Metric label="대출 포함 현금" value={moneyWon(closing.loan_cash_balance_krw)} />
              </BasisGroup>
            ) : (
              <p className="mb-0 text-[12px] text-ink2">
                기준일까지 마감된 날이 없어 대출 제외·포함 현금을 적을 수 없습니다.
              </p>
            )}
            {closing && closing.close_date !== state.state_date && (
              <p className="mb-0 text-[11.5px] text-ink2">
                재무 상태는 {state.state_date}, 마지막 일마감은 {closing.close_date} 입니다 - 두
                숫자는 서로 다른 날의 값입니다.
              </p>
            )}
            <CashBufferNote
              cash={state.current_cash_krw}
              minimum={state.minimum_operating_cash_krw}
              buffer={state.operating_cash_buffer_krw}
            />
          </>
        )}
      </Panel>

      <Panel title="받을 돈 · 줄 돈" subtitle="두 장부가 낸 합계를 그대로 적습니다">
        {receivables.loading || payables.loading ? (
          <Skeleton what="채권·채무" />
        ) : receivables.error ? (
          <Failed what="받을 돈" message={receivables.error} />
        ) : payables.error ? (
          <Failed what="줄 돈" message={payables.error} />
        ) : !receivables.data || !payables.data ? (
          <EmptyRows what="채권·채무" />
        ) : (
          <>
            <Metrics>
              <Metric
                label="받을 돈"
                value={moneyWon(receivables.data?.summary.total_outstanding_krw)}
              />
              <Metric
                label="그중 연체"
                value={moneyWon(overdueReceivable(receivables.data))}
                hint="만기가 지난 금액"
              />
              <Metric label="줄 돈" value={moneyWon(payables.data?.summary.total_outstanding_krw)} />
              <Metric label="그중 연체" value={moneyWon(payables.data?.summary.overdue_krw)} />
            </Metrics>
            <div className="mt-4 grid gap-5 sm:grid-cols-2">
              <div>
                <b className="text-[12px]">받을 돈 — 경과 구간</b>
                <div className="mt-2">
                  <AgingBars
                    empty="아직 받을 돈이 없습니다."
                    slices={[
                      { label: "정상", value: receivables.data.summary.current_krw, color: "var(--color-t-good)" },
                      { label: "1–7일", value: receivables.data.summary.days_1_7_krw, color: "var(--color-t-info)" },
                      { label: "8–30일", value: receivables.data.summary.days_8_30_krw, color: "var(--color-t-warn)" },
                      { label: "30일 초과", value: receivables.data.summary.days_30_plus_krw, color: "var(--color-t-bad)" },
                    ]}
                  />
                </div>
              </div>
              <div>
                <b className="text-[12px]">줄 돈 — 만기 구간</b>
                <div className="mt-2">
                  <AgingBars
                    empty="아직 줄 돈이 없습니다."
                    slices={[
                      { label: "오늘 만기", value: payables.data.summary.due_today_krw, color: "var(--color-t-warn)" },
                      { label: "7일 내 만기", value: payables.data.summary.due_next_7d_krw, color: "var(--color-t-info)" },
                      { label: "연체", value: payables.data.summary.overdue_krw, color: "var(--color-t-bad)" },
                    ]}
                  />
                </div>
              </div>
            </div>
          </>
        )}
      </Panel>

      {/* ★ 판매 전에 묻는 질문 — «얼마까지 더 팔 수 있나» 에 답하는 자리다. */}
      <CreditPanel simRun={simRun} asOf={asOf} refreshKey={creditRefresh} />
      <CreditLimitForm asOf={asOf} onSaved={() => setCreditRefresh((value) => value + 1)} />

      <AgentCard state={latest} />
    </>
  );
}

/**
 * 연체된 받을 돈. **한 구간이 아니라 만기가 지난 구간 전부다.**
 *
 * ⚠️ 전에는 `1–7일` 한 칸만 «그중 연체» 로 적어, 8일 넘게 밀린 돈이 연체에서 빠졌다.
 *   합치는 것은 표시용이고, 구간 금액 자체는 백엔드가 나눈 값 그대로다.
 */
function overdueReceivable(data: ReceivablesResponse | null): number | null {
  if (!data) return null;
  const parts = [
    data.summary.days_1_7_krw,
    data.summary.days_8_30_krw,
    data.summary.days_30_plus_krw,
  ].map(toNumber);
  //  🔴 한 구간이라도 값이 없으면 합을 만들지 않는다 — 없는 것을 0 으로 읽지 않는다.
  if (parts.some((value) => value === null)) return null;
  return parts.reduce<number>((sum, value) => sum + (value ?? 0), 0);
}

/**
 * 같은 기준일을 공유하는 숫자 묶음. **날짜를 묶음 제목에 적는다.**
 *
 * ⚠️ 지표마다 작은 글씨로 날짜를 붙이면 사용자는 그것을 «부가 설명» 으로 읽고 넘긴다.
 *   기준일이 다른 숫자를 한 줄에 섞지 않는 것이 목적이라, 묶음 자체를 나눈다.
 */
function BasisGroup({
  title,
  basis,
  children,
}: {
  title: string;
  basis: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <p className="m-0 mb-2 text-[11.5px] text-ink2">
        {title} 기준 <b className="text-ink tabular-nums">{basis}</b>
      </p>
      <Metrics>{children}</Metrics>
    </div>
  );
}

/**
 * 현금이 최소 운영현금 위인지 아래인지 한 문장으로 말한다.
 *
 * 🔴 **여유를 화면이 빼서 만들지 않는다.** `operating_cash_buffer_krw` 가 백엔드
 *    정본이고, 여기서는 그 값의 **부호만** 읽어 문장을 고른다.
 */
function CashBufferNote({
  cash,
  minimum,
  buffer,
}: {
  cash: string | number | null;
  minimum: string | number | null;
  buffer: string | number | null;
}) {
  const value = toNumber(buffer);
  if (value === null) return null;
  const short = value < 0;
  return (
    <p
      className="mb-0 mt-3 rounded-lg px-3 py-2 text-[12px]"
      style={{
        background: short ? "var(--color-t-bad-bg)" : "var(--color-t-good-bg)",
        color: short ? "var(--color-t-bad)" : "var(--color-t-good)",
      }}
    >
      {short ? (
        <>
          현금 {moneyWon(cash)}이 최소 운영현금 {moneyWon(minimum)}보다{" "}
          <b>{moneyWon(Math.abs(value))} 모자랍니다.</b> 자금이 부족한 상태입니다.
        </>
      ) : (
        <>
          최소 운영현금 {moneyWon(minimum)} 위로 <b>{moneyWon(value)}</b>의 여유가
          있습니다.
        </>
      )}
    </p>
  );
}

/**
 * Finance Agent 카드 — **사용자 문장이 먼저, 원본 값은 접기 안.**
 *
 * 🔴 Runtime 은 *"돌 수 있었나"*, Verdict 는 *"업무 판정이 무엇인가"* 다. 합치면
 *    «판정 없음» 과 «못 돌았음» 이 같은 칸이 된다 — 그래서 **문장도 두 줄**이다.
 */
function AgentCard({
  state,
}: {
  state: { data: FinanceRun | null; error: string | null; loading: boolean };
}) {
  return (
    <Panel title="재무 판단" subtitle="저장된 최신 판단을 읽습니다 — 화면이 다시 판단하지 않습니다">
      {state.loading ? (
        <Skeleton what="최신 판단" />
      ) : state.error ? (
        <Failed what="최신 판단" message={state.error} />
      ) : !state.data ? (
        <EmptyRows what="이 실행의 재무 판단 기록" />
      ) : (
        <>
          <div className="grid gap-2 sm:grid-cols-2">
            <Metric label="판단 결과" value={verdictText(state.data.verdict)} />
            <Metric label="조회 상태" value={runtimeText(state.data.runtime_status)} />
          </div>
          {/* 🔴 **화면 위의 데이터 기준일과 다른 축이다.** 이 카드는 실행 전체에서 가장
              최근 판단을 읽으므로, 날짜만 작게 붙여 두면 사용자가 같은 기준일로 읽는다. */}
          <p className="mb-0 mt-2 text-[11.5px] text-ink2">
            이 실행에서 가장 최근에 내려진 판단이며, 판단 기준일은{" "}
            <b className="text-ink tabular-nums">{state.data.as_of}</b> 입니다 - 화면 위의 데이터
            기준일과 다를 수 있습니다.
          </p>
          {/* ⚠️ 실행별 LLM 설명은 저장되지 않는다. 없으면 없다고 적고 지어내지 않는다. */}
          {state.data.interpretation && (
            <p className="mb-0 mt-3 text-[12px] leading-relaxed text-ink2">
              {state.data.interpretation}
            </p>
          )}
          <div className="mt-3">
            <TechDetails>
              <div className="grid gap-2 sm:grid-cols-3">
                <Metric label="runtime_status" value={state.data.runtime_status} />
                <Metric label="verdict" value={state.data.verdict ?? "null"} />
                <Metric label="llm_status" value={state.data.llm_status} />
              </div>
              <p className="mb-0 mt-3 font-mono text-[11px] text-ink2">
                {state.data.request_id} · {state.data.mode} · {state.data.sim_run_id}
              </p>
              <div className="mt-3 border-t pt-3" style={{ borderColor: "var(--color-hair)" }}>
                <b className="text-[12px]">근거 참조</b>
                <p className="mb-0 mt-1 break-all font-mono text-[11px] text-ink2">
                  {state.data.evidence?.length ? state.data.evidence.join(", ") : "근거 참조 없음"}
                </p>
              </div>
              <div className="mt-3 border-t pt-3" style={{ borderColor: "var(--color-hair)" }}>
                <b className="text-[12px]">저장된 결정론 결과</b>
                <DeterministicRows result={state.data.deterministic_result} />
              </div>
            </TechDetails>
          </div>
        </>
      )}
    </Panel>
  );
}

/**
 * 결정론 결과를 **key/value 표**로 편다.
 *
 * 🔴 **`JSON.stringify` 로 통째로 뱉지 않는다.** 실제로 어떤 키가 오는지는 실행마다
 *    다르고 계약도 없다(`Record<string, unknown>`). 그래서 **키를 지어내 골라내지도
 *    않는다** — 온 것을 이름과 값으로 편평하게 적고, 객체는 접어 둔다.
 */
function DeterministicRows({ result }: { result: Record<string, unknown> | null }) {
  if (!result) {
    return <p className="mb-0 mt-1 text-[12px] text-ink2">이 실행에는 저장된 결정론 결과가 없습니다.</p>;
  }
  const entries = Object.entries(result);
  if (entries.length === 0) {
    return <p className="mb-0 mt-1 text-[12px] text-ink2">저장된 칸이 없습니다.</p>;
  }
  return (
    <dl className="m-0 mt-2 grid grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)] gap-x-4 gap-y-1.5 text-[11.5px]">
      {entries.map(([key, value]) => (
        <div key={key} className="contents">
          <dt className="truncate font-mono text-ink2">{key}</dt>
          <dd className="m-0 break-all font-mono">{scalar(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

function scalar(value: unknown): string {
  if (value === null || value === undefined) return "없음";
  if (typeof value === "object") return Array.isArray(value) ? `${value.length}개 항목` : "하위 구조";
  return String(value);
}

/* ── 자금 흐름 ─────────────────────────────────────────────────────────── */

/** 볼 수 있는 기간. **백엔드 상한(400일) 안에서만 고른다.** */
const RANGES = [
  { key: 30, label: "30일" },
  { key: 90, label: "90일" },
  { key: 400, label: "전체" },
] as const;

function Cashflow({ simRun, asOf }: { simRun: string; asOf: string }) {
  const [days, setDays] = useState<number>(90);
  const state = useConsoleData<FinanceCashflowResponse>(
    `cash:${simRun}:${asOf}:${days}`,
    () => financeConsole.cashflow(simRun, asOf, days),
    true,
  );
  const rows = state.data?.cashflow ?? [];
  return (
    <>
      <Panel
        title="현금이 어떻게 움직였나"
        subtitle="일마감이 저장한 값입니다 — 화면에서 기간별로 다시 계산하지 않습니다"
      >
        <div className="mb-3 flex flex-wrap items-center gap-2">
          {RANGES.map((range) => (
            <button
              key={range.key}
              type="button"
              onClick={() => setDays(range.key)}
              aria-pressed={days === range.key}
              className="rounded-full border px-3 py-1 text-[11.5px]"
              style={{
                borderColor: days === range.key ? "var(--color-t-info)" : "var(--color-hair)",
                opacity: days === range.key ? 1 : 0.6,
              }}
            >
              {range.label}
            </button>
          ))}
          {rows.length > 0 && (
            <span className="text-[11.5px] text-ink2">
              {rows[0].close_date} ~ {rows[rows.length - 1].close_date} · {rows.length}일
            </span>
          )}
        </div>
        {state.loading ? (
          <Skeleton what="자금 흐름" />
        ) : state.error ? (
          <Failed what="자금 흐름" message={state.error} />
        ) : rows.length === 0 ? (
          <EmptyRows what="일마감" />
        ) : (
          <FinanceCashChart rows={rows} />
        )}
      </Panel>

      <Panel title="돈이 어디서 들어오고 나갔나" subtitle="같은 일마감 행의 유입·유출 칸입니다">
        {/* 🔴 **위 패널과 같은 조회다.** 실패했는데 여기서 «0건» 을 띄우면 같은 사고가
            한 화면에서 오류와 빈 데이터로 갈려 보인다 — 순서는 loading, error,
            empty, success 로 고정한다. */}
        {state.loading ? (
          <Skeleton what="유입·유출" />
        ) : state.error ? (
          <Failed what="유입·유출" message={state.error} />
        ) : rows.length === 0 ? (
          <EmptyRows what="일마감" />
        ) : (
          <FinanceFlowChart rows={rows} />
        )}
      </Panel>

      <TechDetails summary="일별 상세 표">
        {state.loading ? (
          <Skeleton what="일별 상세" />
        ) : state.error ? (
          <Failed what="일별 상세" message={state.error} />
        ) : rows.length === 0 ? (
          <EmptyRows what="일마감" />
        ) : (
          <Table
            rows={rows}
            columns={[
              { key: "date", label: "마감일", mono: true, render: (row) => row.close_date },
              { key: "out", label: "매입대금", align: "right", render: (row) => moneyWon(row.purchase_cash_out_krw) },
              { key: "log", label: "물류비", align: "right", render: (row) => moneyWon(row.logistics_cash_out_krw) },
              //  🔴 그래프가 다루는 칸은 표에도 있어야 한다. 빠지면 그래프의 한 줄을
              //     상세에서 되짚을 수 없다.
              { key: "pay", label: "급여·이자", align: "right", render: (row) => moneyWon(row.payroll_interest_cash_out_krw) },
              { key: "in", label: "수금", align: "right", render: (row) => moneyWon(row.collection_cash_in_krw) },
              { key: "net", label: "순현금", align: "right", render: (row) => moneyWon(row.base_net_cash_krw) },
              { key: "base", label: "대출 제외 잔액", align: "right", render: (row) => moneyWon(row.base_cash_balance_krw) },
              { key: "loan", label: "대출 포함 잔액", align: "right", render: (row) => moneyWon(row.loan_cash_balance_krw) },
              {
                key: "min",
                //  ⚠️ 최소 운전자금은 없을 수 있다. 0 으로 적으면 «한도 0» 으로 읽힌다.
                label: "최소 운영현금",
                align: "right",
                render: (row) => moneyWon(row.minimum_operating_cash_krw),
              },
            ]}
          />
        )}
      </TechDetails>
    </>
  );
}

/* ── 받을 돈 ──────────────────────────────────────────────────────────── */

function Receivables({ simRun, asOf }: { simRun: string; asOf: string }) {
  const state = useConsoleData<ReceivablesResponse>(
    `ar:${simRun}:${asOf}`,
    () => financeConsole.receivables(simRun, asOf),
    true,
  );
  if (state.loading) return <Skeleton what="받을 돈" />;
  if (state.error) return <Failed what="받을 돈" message={state.error} />;
  if (!state.data) return <EmptyRows what="받을 돈" />;
  const data = state.data;
  //  🔴 **지금 받을 돈과 이미 받은 돈을 가른다.** 둘을 한 표에 두면 «받을 돈 0원» 이라고
  //     적은 화면 아래에 수금 완료 행이 잔뜩 남아 모순처럼 보인다. 지우는 것이 아니라
  //     아래 이력으로 옮긴다.
  const open = data.rows.filter((row) => (toNumber(row.outstanding_amount_krw) ?? 0) > 0);
  const done = data.rows.filter((row) => (toNumber(row.outstanding_amount_krw) ?? 0) <= 0);
  return (
    <>
      <CreditPanel simRun={simRun} asOf={asOf} />
      <Panel title="받을 돈" subtitle="연체 구간은 백엔드 규칙입니다 — 화면이 다시 나누지 않습니다">
        <Metrics>
          <Metric label="정상" value={moneyWon(data.summary.current_krw)} />
          <Metric label="1–7일" value={moneyWon(data.summary.days_1_7_krw)} />
          <Metric label="8–30일" value={moneyWon(data.summary.days_8_30_krw)} />
          <Metric label="30일 초과" value={moneyWon(data.summary.days_30_plus_krw)} />
        </Metrics>
        <div className="mt-4">
          <AgingBars
            empty="아직 받을 돈이 없습니다."
            slices={[
              { label: "정상", value: data.summary.current_krw, color: "var(--color-t-good)" },
              { label: "1–7일", value: data.summary.days_1_7_krw, color: "var(--color-t-info)" },
              { label: "8–30일", value: data.summary.days_8_30_krw, color: "var(--color-t-warn)" },
              { label: "30일 초과", value: data.summary.days_30_plus_krw, color: "var(--color-t-bad)" },
            ]}
          />
        </div>
        <div className="mt-4">
          {open.length === 0 ? (
            <EmptyRows what="아직 받지 못한 돈" />
          ) : (
            <Table
              rows={open}
              columns={[
                {
                  key: "partner",
                  label: "거래처",
                  render: (row) => partnerText(row.partner_name, row.partner_id),
                },
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
                  label: "받을 금액",
                  align: "right",
                  render: (row) => moneyWon(row.outstanding_amount_krw),
                },
                { key: "status", label: "상태", render: (row) => receivableStatusText(row.status) },
              ]}
            />
          )}
        </div>
      </Panel>
      {done.length > 0 && (
        <Panel
          title="수금 완료 이력"
          subtitle="기준일까지 다 받은 건입니다 — 지금 받을 돈에는 들어가지 않습니다"
        >
          <Table
            rows={done}
            columns={[
              {
                key: "partner",
                label: "거래처",
                render: (row) => partnerText(row.partner_name, row.partner_id),
              },
              { key: "due", label: "만기", mono: true, render: (row) => row.due_date },
              {
                key: "received",
                label: "받은 금액",
                align: "right",
                render: (row) => moneyWon(row.received_amount_krw),
              },
              { key: "status", label: "상태", render: (row) => receivableStatusText(row.status) },
            ]}
          />
        </Panel>
      )}
    </>
  );
}

/* ── 줄 돈 ────────────────────────────────────────────────────────────── */

function Payables({ simRun, asOf }: { simRun: string; asOf: string }) {
  const state = useConsoleData<PayablesResponse>(
    `ap:${simRun}:${asOf}`,
    () => financeConsole.payables(simRun, asOf),
    true,
  );
  if (state.loading) return <Skeleton what="줄 돈" />;
  if (state.error) return <Failed what="줄 돈" message={state.error} />;
  if (!state.data) return <EmptyRows what="줄 돈" />;
  const data = state.data;
  //  🔴 **잔액이 남은 것과 정산이 끝난 것을 가른다.** 끝난 건에 «64일 초과» 를 붙이면
  //     사용자는 지금 연체된 돈으로 읽는다 — 잔액은 0이고 이미 낸 돈이다.
  const open = data.rows.filter((row) => (toNumber(row.outstanding_amount_krw) ?? 0) > 0);
  const done = data.rows.filter((row) => (toNumber(row.outstanding_amount_krw) ?? 0) <= 0);
  return (
    <>
      <Panel title="줄 돈" subtitle="기준일로 잰 만기입니다 — 오늘 시계가 아니라 이 실행의 기준일입니다">
        <Metrics>
          <Metric label="총 잔액" value={moneyWon(data.summary.total_outstanding_krw)} />
          <Metric label="오늘 만기" value={moneyWon(data.summary.due_today_krw)} />
          <Metric label="7일 내 만기" value={moneyWon(data.summary.due_next_7d_krw)} />
          <Metric label="연체" value={moneyWon(data.summary.overdue_krw)} />
        </Metrics>
        <div className="mt-4">
          <AgingBars
            empty="아직 줄 돈이 없습니다."
            slices={[
              { label: "오늘 만기", value: data.summary.due_today_krw, color: "var(--color-t-warn)" },
              { label: "7일 내 만기", value: data.summary.due_next_7d_krw, color: "var(--color-t-info)" },
              { label: "연체", value: data.summary.overdue_krw, color: "var(--color-t-bad)" },
            ]}
          />
        </div>
        <div className="mt-4">
          {open.length === 0 ? (
            <EmptyRows what="아직 내지 않은 돈" />
          ) : (
            <Table
              rows={open}
              columns={[
                { key: "due", label: "만기", mono: true, render: (row) => row.due_date },
                {
                  key: "days",
                  label: "만기까지",
                  align: "right",
                  render: (row) =>
                    row.days_until_due < 0
                      ? `${-row.days_until_due}일 초과`
                      : `${row.days_until_due}일`,
                },
                {
                  key: "amount",
                  label: "낼 금액",
                  align: "right",
                  render: (row) => moneyWon(row.outstanding_amount_krw),
                },
                { key: "status", label: "상태", render: (row) => payableStatusText(row.status) },
              ]}
            />
          )}
        </div>
      </Panel>
      {done.length > 0 && (
        <Panel
          title="정산 완료 이력"
          subtitle="기준일까지 다 낸 건입니다 — 지금 줄 돈에는 들어가지 않습니다"
        >
          <Table
            rows={done}
            columns={[
              { key: "due", label: "만기", mono: true, render: (row) => row.due_date },
              {
                key: "amount",
                label: "낸 금액",
                align: "right",
                render: (row) => moneyWon(row.original_amount_krw),
              },
              { key: "status", label: "상태", render: (row) => payableStatusText(row.status) },
            ]}
          />
          {/* ⚠️ 며칠 늦게 정산됐는지는 업무상 필요할 수 있지만 기본 화면의 «연체» 와
              같은 자리에 두지 않는다. 지금 밀린 돈이 아니다. */}
          <div className="mt-3">
            <TechDetails summary="정산 지연 일수">
              <Table
                rows={done}
                columns={[
                  { key: "id", label: "payable_id", mono: true, render: (row) => row.payable_id },
                  { key: "due", label: "due_date", mono: true, render: (row) => row.due_date },
                  {
                    key: "days",
                    label: "만기 기준 경과",
                    align: "right",
                    render: (row) =>
                      row.days_until_due < 0
                        ? `${-row.days_until_due}일 초과`
                        : `${row.days_until_due}일`,
                  },
                  { key: "status", label: "status", mono: true, render: (row) => row.status },
                ]}
              />
            </TechDetails>
          </div>
        </Panel>
      )}
    </>
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
  if (!state.data) return <EmptyRows what="비용" />;
  const data = state.data;
  return (
    <>
      <Panel title="비용" subtitle="분류는 장부가 저장한 이름 그대로입니다 — 화면이 재분류하지 않습니다">
        <Metrics>
          <Metric label="누적 비용" value={moneyWon(data.summary.total_expenses_krw)} />
        </Metrics>
        <div className="mt-4">
          {data.summary.category_totals.length === 0 ? (
            <EmptyRows what="비용 분류" />
          ) : (
            <AgingBars
              empty="집계된 비용이 없습니다."
              slices={data.summary.category_totals.map((row, index) => ({
                label: row.display_category,
                value: row.total_amount_krw,
                color: CATEGORY_COLORS[index % CATEGORY_COLORS.length],
              }))}
            />
          )}
        </div>
        <div className="mt-4">
          {data.summary.category_totals.length === 0 ? null : (
            <Table
              rows={data.summary.category_totals}
              columns={[
                { key: "label", label: "분류", render: (row) => row.display_category },
                { key: "count", label: "건수", align: "right", render: (row) => `${row.expense_count}건` },
                { key: "sum", label: "합계", align: "right", render: (row) => moneyWon(row.total_amount_krw) },
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
              { key: "amount", label: "금액", align: "right", render: (row) => moneyWon(row.amount_krw) },
            ]}
          />
        )}
      </Panel>
    </>
  );
}

const CATEGORY_COLORS = [
  "var(--color-t-info)",
  "var(--color-t-good)",
  "var(--color-t-warn)",
  "var(--color-t-bad)",
  "var(--color-mut2)",
];

/* ── 차입 ─────────────────────────────────────────────────────────────── */

/**
 * 🔴 **차입 상세 원장이 없다.** authoritative 값은 `finance_states.current_debt_krw`
 *    수준이고, `loan_id` · 이자율 · 실행일 · 만기 · 상환 일정은 **저장소에 없다.**
 *    화면이 그것을 만들면 존재하지 않는 대출이 보고서에 실린다.
 */
function Loans({ simRun, asOf }: { simRun: string; asOf: string }) {
  const summary = useConsoleData<FinanceSummaryResponse>(
    `summary:${simRun}:${asOf}`,
    () => financeConsole.summary(simRun, asOf),
    true,
  );
  const state = summary.data?.states[0];
  return (
    <>
      <Panel title="현재 차입잔액" subtitle="재무 상태가 들고 있는 값입니다">
        {summary.loading ? (
          <Skeleton what="차입잔액" />
        ) : !state ? (
          <EmptyRows what="재무 상태" />
        ) : (
          <Metrics>
            <Metric label="차입잔액" value={moneyWon(state.current_debt_krw)} hint={state.state_date} />
            <Metric label="조달 방식" value={financingModeText(state.financing_mode)} />
          </Metrics>
        )}
      </Panel>
      {/* 🔴 공용 `Unsupported` 는 «UNSUPPORTED» 를 화면에 찍는다. 뜻은 같지만 사용자
          화면에 내부 상태 이름을 남기지 않으려고 같은 내용을 문장으로 적는다.
          공용 컴포넌트는 이번 판의 수정 범위 밖이라 고치지 않고 쓰지 않는다. */}
      <Panel title="차입 상세">
        <p className="m-0 text-[12px] leading-relaxed text-ink2">
          대출 건별 이자율·실행일·만기·상환 일정은 아직 기록되지 않습니다. 없는 값을 화면이
          만들지 않으므로, 지금 답할 수 있는 것은 위의 차입잔액까지입니다.
        </p>
      </Panel>
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
  if (!state.data) return <EmptyRows what="재무 판단" />;
  const data = state.data;
  return (
    <Panel
      title="재무 판단 이력"
      subtitle="이 실행 전체의 판단 기록입니다 — 화면 위의 데이터 기준일과 무관합니다"
    >
      {data.rows.length === 0 ? (
        <>
          <EmptyRows what="재무 판단" />
          <Blocked
            what="실행 축 연결"
            why="재무 실행 행에는 실행 축 칸이 없어, 마스터가 저장한 요청 키로 찾습니다. 마스터를 거치지 않은 실행은 어느 실행에도 속하지 않습니다."
          />
        </>
      ) : (
        <Table
          rows={data.rows}
          columns={[
            //  🔴 **«기준일» 이라고 부르지 않는다.** 화면 위의 데이터 기준일과 같은 말로
            //     읽히지만, 이 값은 그 판단이 어느 날짜를 두고 내려졌는지다.
            { key: "as_of", label: "판단 기준일", mono: true, render: (row) => row.as_of },
            {
              key: "created",
              label: "실행 시각",
              mono: true,
              render: (row) => row.created_at.slice(0, 16).replace("T", " "),
            },
            { key: "mode", label: "구분", render: (row) => MODE_LABELS[row.mode] ?? row.mode },
            { key: "verdict", label: "판단 결과", render: (row) => verdictText(row.verdict) },
            { key: "runtime", label: "조회 상태", render: (row) => runtimeText(row.runtime_status) },
          ]}
        />
      )}
      {data.rows.length > 0 && (
        <p className="mb-0 mt-3 text-[11.5px] text-ink2">
          판단 기준일은 그 판단이 어느 날짜를 두고 내려졌는지이고, 실행 시각은 시스템이 실제로
          계산한 시점입니다 - 둘 다 화면 위의 데이터 기준일과 다른 축입니다.
        </p>
      )}
    </Panel>
  );
}

/** 내부 모드 이름을 업무 말로. 모르는 값은 원본 그대로 둔다. */
const MODE_LABELS: Record<string, string> = {
  PRE_PURCHASE: "매입 전 자금 확인",
  SCENARIO_VALIDATION: "매입안 검증",
  SALES_VALIDATION: "판매안 검증",
};
