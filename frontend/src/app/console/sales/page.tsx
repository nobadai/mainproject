"use client";

/**
 * 판매 운영 콘솔.
 *
 * 🔴 **legacy `/api/screen/sales` 를 더 이상 부르지 않는다.** 운영 콘솔은
 *    `/api/console/sales/…` 만 쓴다 — 옛 API 자체는 남겨 둔다.
 *
 * 🔴 **추정 연결을 하지 않는다.** *"같은 날짜 · 같은 품목 · 가장 최근 행"* 으로
 *    candidate → finance → logistics → master → sale 을 이어 붙이지 않는다. 그렇게
 *    이은 lifecycle 은 그럴듯하고 틀렸을 수 있으며, 틀렸다는 사실이 화면 어디에도
 *    남지 않는다. 확정된 `sales` · `receivables` 만 LIVE 로 보여 준다.
 *
 * ★ **판매 현황이 첫 탭이다** (2026-09-14). 전에는 거래처가 먼저라, 화면을 연 사람이
 *   *"얼마나 팔았나"* 를 알기 전에 거래처 ID 표를 먼저 봤다.
 */

import { useState, useSyncExternalStore } from "react";

import { Panel } from "@/components/console/Blocks";
import {
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
import { PartnerProfileForm } from "@/components/console/PartnerProfileForm";
import { SalesCandidatePanel } from "@/components/console/SalesCandidatePanel";
import {
  AGING_LABELS,
  percent,
  quantity,
  salesConsole,
  type CollectionsResponse,
  type PartnerDetail,
  type PartnersResponse,
  type SalesRunsResponse,
  type SaleLifecycle,
  LIFECYCLE_LABELS,
  STAGE_LABELS,
} from "@/lib/console_api";
import { asOfSnapshot, serverAsOf, subscribeAsOf } from "@/lib/demo_as_of";

import { AgingBars } from "../finance/AgingBars";
import { DataBasis, NoRunChosen, TechDetails } from "../finance/TechDetails";
import {
  DATA_SOURCE_NOTE,
  itemText,
  moneyWon,
  partnerStatusText,
  partnerText,
  partnerTypeText,
  percentPoint,
  pricingContractText,
  receivableStatusText,
  runtimeText,
  verdictText,
} from "../finance/user_text";
import { ActionTable } from "./ActionTable";
import { PartnerCreateForm } from "./PartnerCreateForm";
import { SalesItemChart, SalesPartnerChart, SalesTrendChart } from "./SalesCharts";
import { TodayProposalsPanel } from "./TodayProposals";
import {
  salesOverview,
  type WithItemNames,
  type SalesProposalsResponse,
  type SalesSummaryResponse,
  type SalesTrendResponse,
} from "./sales_api";

type Tab = "overview" | "partners" | "orders" | "collections" | "agent" | "runs";
const TABS: { key: Tab; label: string }[] = [
  { key: "overview", label: "판매 현황" },
  { key: "partners", label: "거래처" },
  { key: "orders", label: "주문 · 판매" },
  { key: "collections", label: "수금" },
  { key: "agent", label: "판단 결과" },
  { key: "runs", label: "실행 이력" },
];

export default function SalesPage() {
  const asOf = useSyncExternalStore(subscribeAsOf, asOfSnapshot, serverAsOf);
  const simRun = useSimRun();
  const [tab, setTab] = useState<Tab>("overview");
  return (
    <div className="mx-auto flex w-full max-w-[1400px] flex-col gap-4 sm:gap-5">
      <DomainHeader title="판매" tabs={TABS} active={tab} onChange={setTab} />
      <DataBasis asOf={asOf} note={DATA_SOURCE_NOTE} />
      {/* 🔴 실행 축은 내부 식별자다. 고르는 자리는 남기되 기본 화면에서 내린다. */}
      <TechDetails summary={simRun ? "실행 선택 · 기술 상세" : "실행을 선택해 주세요"} open={!simRun}>
        <RunPicker asOf={asOf} />
      </TechDetails>
      {!simRun ? <NoRunChosen /> : <Body simRun={simRun} asOf={asOf} tab={tab} />}
    </div>
  );
}

function Body({ simRun, asOf, tab }: { simRun: string; asOf: string; tab: Tab }) {
  if (tab === "overview") return <Overview simRun={simRun} asOf={asOf} />;
  if (tab === "partners") return <Partners simRun={simRun} asOf={asOf} />;
  if (tab === "collections") return <Collections simRun={simRun} asOf={asOf} />;
  if (tab === "orders") return <Orders simRun={simRun} asOf={asOf} />;
  if (tab === "agent") return <Agent simRun={simRun} asOf={asOf} />;
  return <Runs simRun={simRun} />;
}

/* ── 판매 현황 ─────────────────────────────────────────────────────────── */

function Overview({ simRun, asOf }: { simRun: string; asOf: string }) {
  const [proposalRefresh, setProposalRefresh] = useState(0);
  const summary = useConsoleData<SalesSummaryResponse>(
    `sales-summary:${simRun}:${asOf}`,
    () => salesOverview.summary(simRun, asOf),
    true,
  );
  const trend = useConsoleData<SalesTrendResponse>(
    `sales-trend:${simRun}:${asOf}`,
    () => salesOverview.trend(simRun, asOf),
    true,
  );
  const proposals = useConsoleData<SalesProposalsResponse>(
    `sales-proposals:${simRun}:${asOf}:${proposalRefresh}`,
    () => salesOverview.proposals(simRun, asOf),
    true,
  );
  const collections = useConsoleData<CollectionsResponse>(
    `collections:${simRun}:${asOf}`,
    () => salesConsole.collections(simRun, asOf),
    true,
  );
  const partners = useConsoleData<PartnersResponse>(
    `partners:${simRun}:${asOf}`,
    () => salesConsole.partners(simRun, asOf),
    true,
  );

  return (
    <>
      <Panel title="얼마나 팔았나" subtitle="저장된 판매와 채권 합계 그대로입니다 — 화면이 다시 세지 않습니다">
        {summary.loading ? (
          <Skeleton what="판매 현황" />
        ) : summary.error ? (
          <Failed what="판매 현황" message={summary.error} />
        ) : !summary.data ? (
          <EmptyRows what="판매" />
        ) : (
          <Metrics>
            <Metric label="누적 매출" value={moneyWon(summary.data.summary.total_sales_amount_krw)} />
            <Metric label="판매 건수" value={`${summary.data.summary.sales_count}건`} />
            <Metric label="판매량" value={quantity(summary.data.summary.total_sales_quantity_kg)} />
            <Metric
              label="공헌이익"
              value={moneyWon(summary.data.summary.contribution_profit_krw)}
              //  🔴 `contribution_margin_pct` 는 **퍼센트 포인트**다 (백엔드 `_pct`).
              //     비율 formatter 를 쓰면 100 이 한 번 더 곱해져 3483.0% 가 된다.
              hint={`이익률 ${percentPoint(summary.data.summary.contribution_margin_pct)}`}
            />
            <Metric label="미수금" value={moneyWon(summary.data.summary.outstanding_receivables_krw)} />
            <Metric
              label="연체금액"
              value={moneyWon(collections.data?.summary.overdue_krw)}
              hint={collections.error ? "읽지 못했습니다" : undefined}
            />
          </Metrics>
        )}
      </Panel>

      {/* ★ 매입 화면의 «금일 매입안» 과 같은 자리다. 통계 다음에 오늘의 안이 오고,
          지난 흐름은 그 뒤에 온다. */}
      <TodayProposalsPanel asOf={asOf} state={proposals} />
      <SalesCandidatePanel
        asOf={asOf}
        simRun={simRun}
        onCreated={() => setProposalRefresh((value) => value + 1)}
      />

      <Panel title="기간별 매출" subtitle="판매가 있었던 날만 표시합니다">
        {trend.loading ? (
          <Skeleton what="매출 추이" />
        ) : trend.error ? (
          <Failed what="매출 추이" message={trend.error} />
        ) : !trend.data ? (
          <EmptyRows what="매출 추이" />
        ) : trend.data.rows.length === 0 ? (
          <EmptyRows what="판매" />
        ) : (
          <SalesTrendChart data={trend.data} />
        )}
      </Panel>

      <div className="grid gap-4 sm:gap-5 lg:grid-cols-2">
        <Panel title="품목별 매출" subtitle="어떤 품목이 잘 팔렸나">
          {/* 🔴 같은 조회의 실패를 «품목 없음» 으로 보여 주지 않는다. */}
          {summary.loading ? (
            <Skeleton what="품목별 매출" />
          ) : summary.error ? (
            <Failed what="품목별 매출" message={summary.error} />
          ) : !summary.data || summary.data.items.length === 0 ? (
            <EmptyRows what="품목" />
          ) : (
            <SalesItemChart data={summary.data} />
          )}
        </Panel>
        <Panel title="거래처별 매출" subtitle="어느 거래처가 많이 샀나">
          {partners.loading ? (
            <Skeleton what="거래처별 매출" />
          ) : partners.error ? (
            <Failed what="거래처별 매출" message={partners.error} />
          ) : !partners.data || partners.data.rows.length === 0 ? (
            <EmptyRows what="거래처" />
          ) : (
            <SalesPartnerChart rows={partners.data.rows} />
          )}
        </Panel>
      </div>

      <Panel title="수금 · 미수금" subtitle="연체 구간은 재무 Aging 규칙을 그대로 씁니다">
        {collections.loading ? (
          <Skeleton what="수금" />
        ) : collections.error ? (
          <Failed what="수금" message={collections.error} />
        ) : !collections.data ? (
          <EmptyRows what="수금" />
        ) : (
          <>
            <Metrics>
              <Metric label="수금액" value={moneyWon(collections.data.summary.collected_krw)} />
              <Metric
                label="미수 잔액"
                value={moneyWon(collections.data.summary.total_outstanding_krw)}
              />
              <Metric label="연체" value={moneyWon(collections.data.summary.overdue_krw)} />
            </Metrics>
            <div className="mt-4">
              <AgingBars
                empty="아직 미수금이 없습니다."
                slices={[
                  { label: "수금 완료", value: collections.data.summary.collected_krw, color: "var(--color-t-good)" },
                  { label: "미수 (정상)", value: normalOutstanding(collections.data), color: "var(--color-t-info)" },
                  { label: "연체", value: collections.data.summary.overdue_krw, color: "var(--color-t-bad)" },
                ]}
              />
            </div>
          </>
        )}
      </Panel>
    </>
  );
}

/**
 * «아직 만기가 안 된 미수» 를 막대 한 칸으로 보이게 한다.
 *
 * ⚠️ **업무 값이 아니라 표시 조각이다.** 정본은 `total_outstanding_krw` 와
 *   `overdue_krw` 두 칸이고, 여기서는 그 둘의 차이를 **막대 폭**으로만 쓴다. 값이
 *   하나라도 없으면 `null` 이라 막대에서 빠진다 — 0 으로 채우지 않는다.
 */
function normalOutstanding(data: CollectionsResponse): number | null {
  const total = Number(data.summary.total_outstanding_krw);
  const overdue = Number(data.summary.overdue_krw);
  if (!Number.isFinite(total) || !Number.isFinite(overdue)) return null;
  return Math.max(total - overdue, 0);
}

/* ── 거래처 ───────────────────────────────────────────────────────────── */

function Partners({ simRun, asOf }: { simRun: string; asOf: string }) {
  const [selected, setSelected] = useState<string | null>(null);
  //  ★ 등록 뒤 목록을 **다시 읽는다.** 키를 바꾸면 `useConsoleData` 가 새로 부른다 —
  //    화면에만 남은 거래처를 만들지 않는다.
  const [reloads, setReloads] = useState(0);
  const state = useConsoleData<PartnersResponse>(
    `partners:${simRun}:${asOf}:${reloads}`,
    () => salesConsole.partners(simRun, asOf),
    true,
  );
  return (
    <>
      <PartnerCreateForm onCreated={() => setReloads((count) => count + 1)} />
      <Panel
        title="거래처"
        subtitle="거래처 원장은 실행과 무관하지만, 매출·채권 집계는 이 실행의 것입니다"
      >
        {state.loading ? (
          <Skeleton what="거래처" />
        ) : state.error ? (
          <Failed what="거래처" message={state.error} />
        ) : state.data!.rows.length === 0 ? (
          <EmptyRows what="거래처" />
        ) : (
          <>
            <Table
              rows={state.data!.rows}
              columns={[
                {
                  key: "name",
                  label: "거래처명",
                  render: (row) => partnerText(row.partner_name, row.partner_id),
                },
                { key: "type", label: "유형", render: (row) => partnerTypeText(row.partner_type) },
                { key: "status", label: "상태", render: (row) => partnerStatusText(row.status) },
                { key: "sales", label: "누적 매출", align: "right", render: (row) => moneyWon(row.total_sales_krw) },
                { key: "count", label: "판매 건수", align: "right", render: (row) => `${row.total_sales_count}건` },
                { key: "ar", label: "미수금", align: "right", render: (row) => moneyWon(row.receivable_balance_krw) },
                { key: "overdue", label: "연체", align: "right", render: (row) => moneyWon(row.overdue_balance_krw) },
                {
                  key: "last",
                  //  ⚠️ 이 실행에서 판매가 없으면 «없음» 이다. 0 원과 다른 사실이다.
                  label: "최근 판매일",
                  mono: true,
                  render: (row) => row.latest_sale_date ?? "없음",
                },
              ]}
            />
            <div className="mt-3 flex flex-wrap gap-2">
              {state.data!.rows.map((row) => (
                <button
                  key={row.partner_id}
                  onClick={() => setSelected(row.partner_id)}
                  className="rounded-lg border px-3 py-1.5 text-[11.5px]"
                  style={{ borderColor: "var(--color-hair)" }}
                >
                  {partnerText(row.partner_name, row.partner_id)} 상세
                </button>
              ))}
            </div>
            <div className="mt-4">
              <TechDetails summary="내부 거래처 코드">
                <Table
                  rows={state.data!.rows}
                  columns={[
                    { key: "id", label: "partner_id", mono: true, render: (row) => row.partner_id },
                    { key: "name", label: "거래처명", render: (row) => row.partner_name ?? "이름 없음" },
                    { key: "type", label: "partner_type", mono: true, render: (row) => row.partner_type ?? "null" },
                    { key: "status", label: "status", mono: true, render: (row) => row.status },
                  ]}
                />
              </TechDetails>
            </div>
          </>
        )}
      </Panel>
      {selected && <PartnerDetailPanel simRun={simRun} asOf={asOf} partnerId={selected} />}
    </>
  );
}

function PartnerDetailPanel({
  simRun,
  asOf,
  partnerId,
}: {
  simRun: string;
  asOf: string;
  partnerId: string;
}) {
  //  ★ 품목 이름 칸까지 포함해 읽는다 (`sales_api.WithItemNames`) — 공용 타입은
  //    이번 판의 수정 범위 밖이라 판매 쪽에서 넓혀 읽는다.
  const state = useConsoleData<WithItemNames<PartnerDetail>>(
    `partner:${simRun}:${asOf}:${partnerId}`,
    () =>
      salesConsole.partnerDetail(simRun, asOf, partnerId) as Promise<
        WithItemNames<PartnerDetail>
      >,
    true,
  );
  if (state.loading) return <Skeleton what="거래처 상세" />;
  if (state.error) return <Failed what="거래처 상세" message={state.error} />;
  const data = state.data!;
  const overdue = Number(data.summary.overdue_balance_krw);
  const hasOverdue = Number.isFinite(overdue) && overdue > 0;
  return (
    <>
      <Panel
        title={`${partnerText(data.basic.partner_name, data.basic.partner_id)} 상세`}
        subtitle={`${data.basic.client_type ?? partnerTypeText(data.basic.partner_type)} · ${data.basic.factory_region ?? "지역 미상"}`}
      >
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <Badge
            text={hasOverdue ? `연체 ${moneyWon(data.summary.overdue_balance_krw)}` : "연체 없음"}
            tone={hasOverdue ? "bad" : "good"}
          />
          <Badge
            text={partnerStatusText(data.basic.status)}
            tone={data.basic.status === "ACTIVE" ? "good" : "neutral"}
          />
          {data.basic.sales_collection_days !== null && (
            <Badge text={`결제 ${data.basic.sales_collection_days}일`} tone="neutral" />
          )}
          {data.basic.pricing_contract_type !== null && (
            <Badge text={pricingContractText(data.basic.pricing_contract_type)} tone="neutral" />
          )}
        </div>
        <Metrics>
          <Metric
            label="매출"
            value={moneyWon(data.summary.total_sales_krw)}
            hint={`${data.summary.sales_count}건`}
          />
          <Metric label="공헌이익" value={moneyWon(data.summary.contribution_profit_krw)} />
          <Metric
            label="공헌이익률"
            //  ⚠️ 매출이 없으면 «데이터 없음» 이다. 0% 는 잰 값이라는 뜻이라 다르다.
            //  ★ 이 칸은 `contribution_margin_rate` 로 **0~1 비율**이다. 판매 현황의
            //    `contribution_margin_pct` 와 단위가 다르므로 formatter 도 다르다.
            value={percent(data.summary.contribution_margin_rate)}
          />
          <Metric label="미수금" value={moneyWon(data.summary.receivable_balance_krw)} />
        </Metrics>
        <p className="mb-0 mt-3 text-[11.5px] text-ink2">
          여신 한도는 재무에서 관리합니다 — 판매 화면이 «한도 − 채권» 으로 만들지 않습니다.
        </p>
      </Panel>

      <Panel title="품목별 매출">
        {data.item_summary.length === 0 ? (
          <EmptyRows what="품목" />
        ) : (
          <>
            <ItemBars
              rows={data.item_summary.map((row) => ({
                label: itemText(row.item_name, row.item),
                value: row.sales_amount_krw,
              }))}
            />
            <div className="mt-4">
              <Table
                rows={data.item_summary}
                columns={[
                  {
                    key: "item",
                    label: "품목",
                    render: (row) => itemText(row.item_name, row.item),
                  },
                  { key: "qty", label: "수량", align: "right", render: (row) => quantity(row.quantity_kg) },
                  { key: "amount", label: "매출", align: "right", render: (row) => moneyWon(row.sales_amount_krw) },
                  {
                    key: "profit",
                    label: "공헌이익",
                    align: "right",
                    render: (row) => moneyWon(row.contribution_profit_krw),
                  },
                ]}
              />
            </div>
          </>
        )}
      </Panel>

      <Panel title="최근 판매">
        {data.recent_sales.length === 0 ? (
          <EmptyRows what="판매" />
        ) : (
          <Table
            rows={data.recent_sales}
            columns={[
              { key: "date", label: "판매일", mono: true, render: (row) => row.sale_date },
              {
                key: "item",
                label: "품목",
                render: (row) => itemText(row.item_name, row.item),
              },
              { key: "qty", label: "수량", align: "right", render: (row) => quantity(row.quantity_kg) },
              { key: "price", label: "단가", align: "right", render: (row) => moneyWon(row.unit_price_krw) },
              { key: "amount", label: "금액", align: "right", render: (row) => moneyWon(row.sales_amount_krw) },
              {
                key: "profit",
                label: "공헌이익",
                align: "right",
                render: (row) => moneyWon(row.contribution_profit_krw),
              },
            ]}
          />
        )}
      </Panel>

      <PartnerProfileForm partnerId={partnerId} />
    </>
  );
}

function ItemBars({ rows }: { rows: { label: string; value: string | number }[] }) {
  const palette = [
    "var(--color-t-info)",
    "var(--color-t-good)",
    "var(--color-t-warn)",
    "var(--color-t-bad)",
    "var(--color-mut2)",
  ];
  return (
    <AgingBars
      empty="이 거래처에는 아직 판매가 없습니다."
      slices={rows.map((row, index) => ({
        label: row.label,
        value: row.value,
        color: palette[index % palette.length],
      }))}
    />
  );
}

function Badge({ text, tone }: { text: string; tone: "good" | "bad" | "neutral" }) {
  const color =
    tone === "neutral" ? "var(--color-ink2)" : `var(--color-t-${tone === "good" ? "good" : "bad"})`;
  const background =
    tone === "neutral" ? "var(--color-grid)" : `var(--color-t-${tone === "good" ? "good" : "bad"}-bg)`;
  return (
    <span className="rounded-full px-3 py-1 text-[11.5px]" style={{ color, background }}>
      {text}
    </span>
  );
}

/* ── 수금 ─────────────────────────────────────────────────────────────── */

function Collections({ simRun, asOf }: { simRun: string; asOf: string }) {
  const state = useConsoleData<CollectionsResponse>(
    `collections:${simRun}:${asOf}`,
    () => salesConsole.collections(simRun, asOf),
    true,
  );
  if (state.loading) return <Skeleton what="수금" />;
  if (state.error) return <Failed what="수금" message={state.error} />;
  const data = state.data!;
  return (
    <Panel title="수금" subtitle="정본은 매출채권이며, 연체 구간은 재무 Aging 규칙을 그대로 씁니다">
      <Metrics>
        <Metric label="미수 잔액" value={moneyWon(data.summary.total_outstanding_krw)} />
        <Metric label="연체" value={moneyWon(data.summary.overdue_krw)} />
        <Metric label="수금액" value={moneyWon(data.summary.collected_krw)} />
      </Metrics>
      <div className="mt-4">
        <AgingBars
          empty="아직 미수금이 없습니다."
          slices={[
            { label: "수금 완료", value: data.summary.collected_krw, color: "var(--color-t-good)" },
            { label: "미수 (정상)", value: normalOutstanding(data), color: "var(--color-t-info)" },
            { label: "연체", value: data.summary.overdue_krw, color: "var(--color-t-bad)" },
          ]}
        />
      </div>
      <div className="mt-4">
        {data.rows.length === 0 ? (
          <EmptyRows what="수금 대상" />
        ) : (
          <Table
            rows={data.rows}
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
                key: "outstanding",
                label: "미수",
                align: "right",
                render: (row) => moneyWon(row.outstanding_amount_krw),
              },
              { key: "received", label: "수금", align: "right", render: (row) => moneyWon(row.received_amount_krw) },
              { key: "status", label: "상태", render: (row) => receivableStatusText(row.status) },
            ]}
          />
        )}
      </div>
    </Panel>
  );
}

/* ── 주문 · 판매 ──────────────────────────────────────────────────────── */

function Orders({ simRun, asOf }: { simRun: string; asOf: string }) {
  const [selected, setSelected] = useState<string | null>(null);
  const state = useConsoleData<CollectionsResponse>(
    `collections:${simRun}:${asOf}`,
    () => salesConsole.collections(simRun, asOf),
    true,
  );
  return (
    <>
      <Panel title="확정된 판매와 채권" subtitle="저장된 사실만 — 단계를 추정으로 잇지 않습니다">
        {state.loading ? (
          <Skeleton what="판매" />
        ) : state.error ? (
          <Failed what="판매" message={state.error} />
        ) : state.data!.rows.length === 0 ? (
          <EmptyRows what="확정 판매" />
        ) : (
          // 🔴 **버튼을 표 밖에 묶어 두지 않는다.** 전에는 이름이 같은 «판매 흐름 보기»
          //    버튼이 행 수만큼 나열돼, 어느 판매의 버튼인지 알 수 없었다. 행마다
          //    액션 칸을 두면 누른 버튼과 그 행의 `sale_id` 가 눈으로 이어진다.
          <ActionTable
            rows={state.data!.rows}
            rowKey={(row) => row.receivable_id}
            columns={[
              {
                key: "partner",
                label: "거래처",
                render: (row) => partnerText(row.partner_name, row.partner_id),
              },
              { key: "amount", label: "금액", align: "right", render: (row) => moneyWon(row.original_amount_krw) },
              { key: "received", label: "수금", align: "right", render: (row) => moneyWon(row.received_amount_krw) },
              { key: "due", label: "회수 만기", mono: true, render: (row) => row.due_date },
              { key: "status", label: "상태", render: (row) => receivableStatusText(row.status) },
              {
                key: "action",
                label: "상세",
                render: (row) => (
                  <button
                    type="button"
                    onClick={() => setSelected(row.sale_id)}
                    aria-pressed={selected === row.sale_id}
                    className="rounded-md border px-2 py-1 text-[11px]"
                    style={{
                      borderColor:
                        selected === row.sale_id ? "var(--color-t-info)" : "var(--color-hair)",
                    }}
                  >
                    흐름 보기
                  </button>
                ),
              },
            ]}
          />
        )}
      </Panel>
      {selected && <Lifecycle simRun={simRun} asOf={asOf} saleId={selected} />}
    </>
  );
}

/**
 * 판매 한 건의 흐름.
 *
 * 🔴 **상태는 전부 백엔드가 낸 값이다.** 화면은 «완료» 를 추론하지 않는다 — 어느
 *    단계가 왜 그 상태인지는 저장된 행이 답한다.
 */
function Lifecycle({ simRun, asOf, saleId }: { simRun: string; asOf: string; saleId: string }) {
  const state = useConsoleData<SaleLifecycle>(
    `lifecycle:${simRun}:${asOf}:${saleId}`,
    () => salesConsole.lifecycle(simRun, asOf, saleId),
    true,
  );
  if (state.loading) return <Skeleton what="판매 흐름" />;
  if (state.error) return <Failed what="판매 흐름" message={state.error} />;
  const data = state.data!;
  return (
    <Panel title="판매 흐름" subtitle="저장된 연결키로만 이었습니다">
      <Table
        rows={data.stages}
        columns={[
          { key: "stage", label: "단계", render: (row) => STAGE_LABELS[row.stage] ?? "기타 단계" },
          { key: "status", label: "상태", render: (row) => LIFECYCLE_LABELS[row.status] ?? "세부 상태를 확인해 주세요" },
          { key: "when", label: "시각", mono: true, render: (row) => row.occurred_at ?? "—" },
          { key: "detail", label: "설명", render: (row) => row.detail },
        ]}
      />
      <p className="mb-0 mt-3 text-[11.5px] text-ink2">
        {data.agent_lineage === "LIVE"
          ? "후보 → 판매 구간은 확정에 실린 업무 키로 이어졌습니다."
          : "이 판매에는 업무 키가 실려 있지 않아 후보 → 판매 구간을 잇지 못합니다. 날짜·품목으로 추정해 잇지 않습니다."}
      </p>
      <div className="mt-3">
        <TechDetails>
          <p className="m-0 font-mono text-[11px] text-ink2">
            {data.sale_id} · 확정 구간 {data.confirmed_lineage} · 후보 구간 {data.agent_lineage}
          </p>
          <div className="mt-2">
            <Table
              rows={data.stages}
              columns={[
                { key: "stage", label: "stage", mono: true, render: (row) => row.stage },
                { key: "status", label: "status", mono: true, render: (row) => row.status },
                { key: "ref", label: "reference", mono: true, render: (row) => row.reference ?? "—" },
              ]}
            />
          </div>
        </TechDetails>
      </div>
    </Panel>
  );
}

/* ── 판단 결과 ────────────────────────────────────────────────────────── */

function Agent({ simRun, asOf }: { simRun: string; asOf: string }) {
  const state = useConsoleData<SalesRunsResponse>(
    `runs:${simRun}`,
    () => salesConsole.runs(simRun, 10),
    true,
  );
  return (
    <>
      <Panel title="최근 판단" subtitle="저장된 판단을 읽습니다 — 화면이 다시 판단하지 않습니다">
        {state.loading ? (
          <Skeleton what="판단" />
        ) : state.error ? (
          <Failed what="판단" message={state.error} />
        ) : state.data!.rows.length === 0 ? (
          <EmptyRows what="판단 기록" />
        ) : (
          <>
            <Table
              rows={state.data!.rows}
              columns={[
                { key: "as_of", label: "기준일", mono: true, render: (row) => row.as_of },
                { key: "item", label: "품목", render: (row) => row.item ?? "품목 미상" },
                {
                  key: "partner",
                  label: "거래처",
                  render: (row) => partnerText(row.partner_name, row.partner_id),
                },
                { key: "verdict", label: "판단 결과", render: (row) => verdictText(row.verdict) },
                { key: "runtime", label: "조회 상태", render: (row) => runtimeText(row.runtime_status) },
              ]}
            />
            <div className="mt-4">
              <TechDetails>
                <Table
                  rows={state.data!.rows}
                  columns={[
                    { key: "runtime", label: "runtime_status", mono: true, render: (row) => row.runtime_status },
                    { key: "llm", label: "llm_status", mono: true, render: (row) => row.llm_status ?? "없음" },
                    {
                      key: "end",
                      label: "master_end_code",
                      mono: true,
                      render: (row) => row.master_end_code ?? "없음",
                    },
                    { key: "req", label: "request_id", mono: true, render: (row) => row.request_id ?? "없음" },
                  ]}
                />
              </TechDetails>
            </div>
          </>
        )}
      </Panel>
      <SalesCandidatePanel simRun={simRun} asOf={asOf} />
    </>
  );
}

/* ── 실행 이력 ─────────────────────────────────────────────────────────── */

function Runs({ simRun }: { simRun: string }) {
  const [page, setPage] = useState(0);
  const [search, setSearch] = useState("");
  const [asOf, setAsOf] = useState("");
  const [runtime, setRuntime] = useState("");
  const state = useConsoleData<SalesRunsResponse>(
    `runs-full:${simRun}`,
    // 화면은 10개씩 나누되, 같은 실행의 오래된 기록도 페이지에서 찾을 수 있게 최대
    // 읽기 한도까지 한 번에 받는다. 이력 조회는 read-only다.
    () => salesConsole.runs(simRun, 500),
    true,
  );
  if (state.loading) return <Skeleton what="실행 이력" />;
  if (state.error) return <Failed what="실행 이력" message={state.error} />;
  const data = state.data!;
  const normalizedSearch = search.trim().toLocaleLowerCase("ko-KR");
  const runtimeChoices = [...new Set(data.rows.map((row) => row.runtime_status))];
  const filteredRows = data.rows.filter((row) => {
    const matchesSearch =
      normalizedSearch === "" ||
      (row.item ?? "").toLocaleLowerCase("ko-KR").includes(normalizedSearch) ||
      partnerText(row.partner_name, row.partner_id).toLocaleLowerCase("ko-KR").includes(normalizedSearch);
    return matchesSearch && (asOf === "" || row.as_of === asOf) && (runtime === "" || row.runtime_status === runtime);
  });
  const pageSize = 10;
  const pageCount = Math.max(1, Math.ceil(filteredRows.length / pageSize));
  const currentPage = Math.min(page, pageCount - 1);
  const pageRows = filteredRows.slice(currentPage * pageSize, (currentPage + 1) * pageSize);
  return (
    <Panel title="판매 실행 이력" subtitle="이 실행에 속한 저장 기록만 표시합니다">
      {data.rows.length === 0 ? (
        <EmptyRows what="판매 실행" />
      ) : (
        <>
          <div className="mb-3 flex flex-wrap gap-2">
            <label className="sr-only" htmlFor="sales-run-search">품목 또는 거래처 검색</label>
            <input
              id="sales-run-search"
              value={search}
              onChange={(event) => {
                setSearch(event.target.value);
                setPage(0);
              }}
              placeholder="품목 또는 거래처 검색"
              className="min-w-[190px] rounded-md border px-3 py-1.5 text-[12px]"
              style={{ borderColor: "var(--color-hair)" }}
            />
            <label className="sr-only" htmlFor="sales-run-date">기준일 필터</label>
            <select
              id="sales-run-date"
              value={asOf}
              onChange={(event) => {
                setAsOf(event.target.value);
                setPage(0);
              }}
              className="rounded-md border px-2 py-1.5 text-[12px]"
              style={{ borderColor: "var(--color-hair)" }}
            >
              <option value="">모든 기준일</option>
              {[...new Set(data.rows.map((row) => row.as_of))].map((date) => <option key={date} value={date}>{date}</option>)}
            </select>
            <label className="sr-only" htmlFor="sales-run-status">조회 상태 필터</label>
            <select
              id="sales-run-status"
              value={runtime}
              onChange={(event) => {
                setRuntime(event.target.value);
                setPage(0);
              }}
              className="rounded-md border px-2 py-1.5 text-[12px]"
              style={{ borderColor: "var(--color-hair)" }}
            >
              <option value="">모든 조회 상태</option>
              {runtimeChoices.map((value) => <option key={value} value={value}>{runtimeText(value)}</option>)}
            </select>
          </div>
          {filteredRows.length === 0 ? (
            <p className="m-0 rounded-lg border px-4 py-5 text-[12px] text-ink2" style={{ borderColor: "var(--color-hair)" }}>
              조건에 맞는 실행 이력이 없습니다. 검색어나 필터를 바꿔 보세요.
            </p>
          ) : (
            <>
              <Table
                rows={pageRows}
                controls={false}
                columns={[
              { key: "as_of", label: "기준일", mono: true, render: (row) => row.as_of },
              { key: "item", label: "품목", render: (row) => row.item ?? "품목 미상" },
              {
                key: "partner",
                label: "거래처",
                render: (row) => partnerText(row.partner_name, row.partner_id),
              },
              { key: "runtime", label: "조회 상태", render: (row) => runtimeText(row.runtime_status) },
              //  🔴 판매는 자기 verdict 를 저장하지 않는다. 없는 것을 만들지 않는다.
              {
                key: "verdict",
                label: "판단 결과",
                render: (row) => (row.verdict === null ? "판매가 판정을 보유하지 않음" : verdictText(row.verdict)),
              },
                ]}
              />
              <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-[12px] text-ink2">
            <span>
              검색 결과 {filteredRows.length}건 중 {currentPage * pageSize + 1}–
              {Math.min((currentPage + 1) * pageSize, filteredRows.length)}건
            </span>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => setPage((value) => Math.max(0, value - 1))}
                disabled={currentPage === 0}
                className="rounded-md border px-2 py-1 disabled:cursor-not-allowed disabled:opacity-50"
                style={{ borderColor: "var(--color-hair)" }}
              >
                이전
              </button>
              <span>{currentPage + 1} / {pageCount}</span>
              <button
                type="button"
                onClick={() => setPage((value) => Math.min(pageCount - 1, value + 1))}
                disabled={currentPage >= pageCount - 1}
                className="rounded-md border px-2 py-1 disabled:cursor-not-allowed disabled:opacity-50"
                style={{ borderColor: "var(--color-hair)" }}
              >
                다음
              </button>
            </div>
              </div>
              <div className="mt-4">
                <TechDetails>
                  <Table
                    rows={pageRows}
                    controls={false}
                    columns={[
                  { key: "runtime", label: "runtime_status", mono: true, render: (row) => row.runtime_status },
                  {
                    key: "end",
                    label: "master_end_code",
                    mono: true,
                    render: (row) => row.master_end_code ?? "없음",
                  },
                  { key: "req", label: "request_id", mono: true, render: (row) => row.request_id ?? "없음" },
                  { key: "run", label: "run_id", mono: true, render: (row) => row.run_id },
                    ]}
                  />
                </TechDetails>
              </div>
            </>
          )}
        </>
      )}
    </Panel>
  );
}
