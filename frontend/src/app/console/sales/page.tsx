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
 */

import { useState, useSyncExternalStore } from "react";

import { Panel } from "@/components/console/Blocks";
import {
  EmptyRows,
  Failed,
  Metric,
  Metrics,
  NoRunSelected,
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
  money,
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

type Tab = "partners" | "orders" | "collections" | "agent" | "runs";
const TABS: { key: Tab; label: string }[] = [
  { key: "partners", label: "거래처" },
  { key: "orders", label: "주문 · 판매" },
  { key: "collections", label: "수금" },
  { key: "agent", label: "Agent 판단" },
  { key: "runs", label: "실행 이력" },
];

export default function SalesPage() {
  const asOf = useSyncExternalStore(subscribeAsOf, asOfSnapshot, serverAsOf);
  const simRun = useSimRun();
  const [tab, setTab] = useState<Tab>("partners");
  return (
    <div className="mx-auto flex w-full max-w-[1400px] flex-col gap-4 sm:gap-5">
      <DomainHeader title="판매" tabs={TABS} active={tab} onChange={setTab} />
      <RunPicker asOf={asOf} />
      {!simRun ? (
        <NoRunSelected />
      ) : (
        <Body simRun={simRun} asOf={asOf} tab={tab} />
      )}
    </div>
  );
}

function Body({
  simRun,
  asOf,
  tab,
}: {
  simRun: string;
  asOf: string;
  tab: Tab;
}) {
  if (tab === "partners") return <Partners simRun={simRun} asOf={asOf} />;
  if (tab === "collections") return <Collections simRun={simRun} asOf={asOf} />;
  if (tab === "orders") return <Orders simRun={simRun} asOf={asOf} />;
  if (tab === "agent") return <Agent simRun={simRun} asOf={asOf} />;
  return <Runs simRun={simRun} />;
}

/* ── 거래처 ───────────────────────────────────────────────────────────── */

function Partners({ simRun, asOf }: { simRun: string; asOf: string }) {
  const [selected, setSelected] = useState<string | null>(null);
  const state = useConsoleData<PartnersResponse>(
    `partners:${simRun}:${asOf}`,
    () => salesConsole.partners(simRun, asOf),
    true,
  );
  if (state.loading) return <Skeleton what="거래처" />;
  if (state.error) return <Failed what="거래처" message={state.error} />;
  const data = state.data!;
  return (
    <>
      <Panel
        title="거래처"
        subtitle="거래처 원장은 실행과 무관하지만, 매출·채권 집계는 이 실행의 것입니다"
      >
        {data.rows.length === 0 ? (
          <EmptyRows what="거래처" />
        ) : (
          <Table
            rows={data.rows}
            columns={[
              {
                key: "id",
                label: "거래처",
                mono: true,
                render: (row) => row.partner_id,
              },
              {
                key: "name",
                label: "이름",
                render: (row) => row.partner_name ?? "이름 없음",
              },
              { key: "status", label: "상태", render: (row) => row.status },
              {
                key: "sales",
                label: "매출",
                align: "right",
                render: (row) => money(row.total_sales_krw),
              },
              {
                key: "count",
                label: "건수",
                align: "right",
                render: (row) => `${row.total_sales_count}건`,
              },
              {
                key: "ar",
                label: "채권",
                align: "right",
                render: (row) => money(row.receivable_balance_krw),
              },
              {
                key: "overdue",
                label: "연체",
                align: "right",
                render: (row) => money(row.overdue_balance_krw),
              },
              {
                key: "last",
                //  ⚠️ 이 실행에서 판매가 없으면 «없음» 이다. 0 원과 다른 사실이다.
                label: "최근 판매",
                mono: true,
                render: (row) => row.latest_sale_date ?? "없음",
              },
            ]}
          />
        )}
        <div className="mt-3 flex flex-wrap gap-2">
          {data.rows.map((row) => (
            <button
              key={row.partner_id}
              onClick={() => setSelected(row.partner_id)}
              className="rounded-lg border px-3 py-1.5 text-[11.5px]"
              style={{ borderColor: "var(--color-hair)" }}
            >
              {row.partner_name ?? row.partner_id} 상세
            </button>
          ))}
        </div>
      </Panel>
      {selected && (
        <PartnerDetailPanel simRun={simRun} asOf={asOf} partnerId={selected} />
      )}
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
  const state = useConsoleData<PartnerDetail>(
    `partner:${simRun}:${asOf}:${partnerId}`,
    () => salesConsole.partnerDetail(simRun, asOf, partnerId),
    true,
  );
  if (state.loading) return <Skeleton what="거래처 상세" />;
  if (state.error) return <Failed what="거래처 상세" message={state.error} />;
  const data = state.data!;
  return (
    <>
      <Panel
        title={`${data.basic.partner_name ?? data.basic.partner_id} 상세`}
        subtitle={`${data.basic.partner_type ?? "유형 미상"} · ${data.basic.factory_region ?? "지역 미상"}`}
      >
        <Metrics>
          <Metric
            label="매출"
            value={money(data.summary.total_sales_krw)}
            hint={`${data.summary.sales_count}건`}
          />
          <Metric
            label="공헌이익"
            value={money(data.summary.contribution_profit_krw)}
          />
          <Metric
            label="공헌이익률"
            //  ⚠️ 매출이 없으면 «데이터 없음» 이다. 0% 는 잰 값이라는 뜻이라 다르다.
            value={percent(data.summary.contribution_margin_rate)}
          />
          <Metric
            label="채권 잔액"
            value={money(data.summary.receivable_balance_krw)}
            hint={`연체 ${money(data.summary.overdue_balance_krw)}`}
          />
        </Metrics>
        <p className="mb-0 mt-3 text-[11.5px] text-ink2">
          여신 한도는 재무 정본입니다 — 판매가 «한도 − 채권» 으로 만들지
          않습니다 ({data.credit_status}).
        </p>
      </Panel>
      <Panel title="최근 판매">
        {data.recent_sales.length === 0 ? (
          <EmptyRows what="판매" />
        ) : (
          <Table
            rows={data.recent_sales}
            columns={[
              {
                key: "date",
                label: "판매일",
                mono: true,
                render: (row) => row.sale_date,
              },
              {
                key: "item",
                label: "품목",
                render: (row) => row.item ?? "품목 미상",
              },
              {
                key: "qty",
                label: "수량",
                align: "right",
                render: (row) => quantity(row.quantity_kg),
              },
              {
                key: "price",
                label: "단가",
                align: "right",
                render: (row) => money(row.unit_price_krw),
              },
              {
                key: "amount",
                label: "금액",
                align: "right",
                render: (row) => money(row.sales_amount_krw),
              },
              {
                key: "profit",
                label: "공헌이익",
                align: "right",
                render: (row) => money(row.contribution_profit_krw),
              },
            ]}
          />
        )}
      </Panel>
      <PartnerProfileForm partnerId={partnerId} />
      <Panel title="품목별 집계">
        {data.item_summary.length === 0 ? (
          <EmptyRows what="품목" />
        ) : (
          <Table
            rows={data.item_summary}
            columns={[
              { key: "item", label: "품목", render: (row) => row.item },
              {
                key: "qty",
                label: "수량",
                align: "right",
                render: (row) => quantity(row.quantity_kg),
              },
              {
                key: "amount",
                label: "매출",
                align: "right",
                render: (row) => money(row.sales_amount_krw),
              },
              {
                key: "profit",
                label: "공헌이익",
                align: "right",
                render: (row) => money(row.contribution_profit_krw),
              },
            ]}
          />
        )}
      </Panel>
    </>
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
    <Panel
      title="수금"
      subtitle="정본은 매출채권이며, 연체 구간은 재무 Aging 규칙을 그대로 씁니다"
    >
      <Metrics>
        <Metric
          label="미수 잔액"
          value={money(data.summary.total_outstanding_krw)}
        />
        <Metric label="연체" value={money(data.summary.overdue_krw)} />
        <Metric label="수금액" value={money(data.summary.collected_krw)} />
      </Metrics>
      <div className="mt-3">
        {data.rows.length === 0 ? (
          <EmptyRows what="수금 대상" />
        ) : (
          <Table
            rows={data.rows}
            columns={[
              {
                key: "partner",
                label: "거래처",
                render: (row) => row.partner_name ?? row.partner_id ?? "미지정",
              },
              {
                key: "sale",
                label: "판매",
                mono: true,
                render: (row) => row.sale_id,
              },
              {
                key: "due",
                label: "만기",
                mono: true,
                render: (row) => row.due_date,
              },
              {
                key: "bucket",
                label: "구간",
                render: (row) => AGING_LABELS[row.aging_bucket],
              },
              {
                key: "overdue",
                label: "연체일",
                align: "right",
                render: (row) =>
                  row.days_overdue === null ? "—" : `${row.days_overdue}일`,
              },
              {
                key: "outstanding",
                label: "미수",
                align: "right",
                render: (row) => money(row.outstanding_amount_krw),
              },
              {
                key: "received",
                label: "수금",
                align: "right",
                render: (row) => money(row.received_amount_krw),
              },
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
      <Panel
        title="확정된 판매와 채권"
        subtitle="저장된 사실만 — 단계를 추정으로 잇지 않습니다"
      >
        {state.loading ? (
          <Skeleton what="판매" />
        ) : state.error ? (
          <Failed what="판매" message={state.error} />
        ) : state.data!.rows.length === 0 ? (
          <EmptyRows what="확정 판매" />
        ) : (
          <Table
            rows={state.data!.rows}
            columns={[
              {
                key: "sale",
                label: "판매",
                mono: true,
                render: (row) => row.sale_id,
              },
              {
                key: "partner",
                label: "거래처",
                render: (row) => row.partner_name ?? "미지정",
              },
              {
                key: "amount",
                label: "금액",
                align: "right",
                render: (row) => money(row.original_amount_krw),
              },
              {
                key: "due",
                label: "회수 만기",
                mono: true,
                render: (row) => row.due_date,
              },
              { key: "status", label: "상태", render: (row) => row.status },
            ]}
          />
        )}
        {state.data && state.data.rows.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-2">
            {[...new Set(state.data.rows.map((row) => row.sale_id))].map(
              (saleId) => (
                <button
                  key={saleId}
                  onClick={() => setSelected(saleId)}
                  className="rounded-lg border px-3 py-1.5 font-mono text-[11px]"
                  style={{ borderColor: "var(--color-hair)" }}
                >
                  {saleId} 흐름
                </button>
              ),
            )}
          </div>
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
function Lifecycle({
  simRun,
  asOf,
  saleId,
}: {
  simRun: string;
  asOf: string;
  saleId: string;
}) {
  const state = useConsoleData<SaleLifecycle>(
    `lifecycle:${simRun}:${asOf}:${saleId}`,
    () => salesConsole.lifecycle(simRun, asOf, saleId),
    true,
  );
  if (state.loading) return <Skeleton what="판매 흐름" />;
  if (state.error) return <Failed what="판매 흐름" message={state.error} />;
  const data = state.data!;
  return (
    <Panel
      title={`${data.sale_id} 흐름`}
      subtitle={`확정 구간 ${data.confirmed_lineage} · 후보 구간 ${data.agent_lineage}`}
    >
      <Table
        rows={data.stages}
        columns={[
          {
            key: "stage",
            label: "단계",
            render: (row) => STAGE_LABELS[row.stage] ?? row.stage,
          },
          {
            key: "status",
            label: "상태",
            render: (row) => `${LIFECYCLE_LABELS[row.status]} (${row.status})`,
          },
          {
            key: "ref",
            label: "참조",
            mono: true,
            //  ⚠️ 참조가 없으면 «없음» 이다. 다른 단계의 ID 를 빌려 오지 않는다.
            render: (row) => row.reference ?? "—",
          },
          {
            key: "when",
            label: "시각",
            mono: true,
            render: (row) => row.occurred_at ?? "—",
          },
          { key: "detail", label: "사유", render: (row) => row.detail },
        ]}
      />
      <p className="mb-0 mt-3 text-[11px] text-ink2">
        {data.agent_lineage === "LIVE"
          ? "후보 → 판매 구간은 확정에 실린 마스터 업무 키로 이어졌습니다."
          : "이 판매에는 마스터 업무 키가 실려 있지 않아 후보 → 판매 구간을 잇지 못합니다. 날짜·품목으로 추정해 잇지 않습니다."}
      </p>
    </Panel>
  );
}

/* ── Agent 판단 ───────────────────────────────────────────────────────── */

function Agent({ simRun, asOf }: { simRun: string; asOf: string }) {
  const state = useConsoleData<SalesRunsResponse>(
    `runs:${simRun}`,
    () => salesConsole.runs(simRun, 10),
    true,
  );
  return (
    <>
      <Panel
        title="최근 Sales 실행"
        subtitle="저장된 실행을 읽습니다 — 화면이 Agent를 다시 돌리지 않습니다"
      >
        {state.loading ? (
          <Skeleton what="Sales 실행" />
        ) : state.error ? (
          <Failed what="Sales 실행" message={state.error} />
        ) : state.data!.rows.length === 0 ? (
          <EmptyRows what="Sales 실행" />
        ) : (
          <Table
            rows={state.data!.rows}
            columns={[
              {
                key: "as_of",
                label: "기준일",
                mono: true,
                render: (row) => row.as_of,
              },
              {
                key: "item",
                label: "품목",
                render: (row) => row.item ?? "품목 미상",
              },
              {
                key: "partner",
                label: "거래처",
                render: (row) => row.partner_name ?? row.partner_id ?? "미지정",
              },
              {
                key: "runtime",
                label: "Runtime",
                render: (row) => row.runtime_status,
              },
              {
                key: "end",
                label: "Master end code",
                mono: true,
                render: (row) => row.master_end_code ?? "없음",
              },
              {
                key: "llm",
                label: "LLM",
                render: (row) => row.llm_status ?? "없음",
              },
            ]}
          />
        )}
      </Panel>
      <SalesCandidatePanel simRun={simRun} asOf={asOf} />
    </>
  );
}

/* ── 실행 이력 ─────────────────────────────────────────────────────────── */

function Runs({ simRun }: { simRun: string }) {
  const state = useConsoleData<SalesRunsResponse>(
    `runs-full:${simRun}`,
    () => salesConsole.runs(simRun),
    true,
  );
  if (state.loading) return <Skeleton what="실행 이력" />;
  if (state.error) return <Failed what="실행 이력" message={state.error} />;
  const data = state.data!;
  return (
    <Panel
      title="Sales 실행 이력"
      subtitle="이 실행에 속한 저장 기록만 표시합니다"
    >
      {data.rows.length === 0 ? (
        <EmptyRows what="Sales 실행" />
      ) : (
        <Table
          rows={data.rows}
          columns={[
            {
              key: "as_of",
              label: "기준일",
              mono: true,
              render: (row) => row.as_of,
            },
            {
              key: "item",
              label: "품목",
              render: (row) => row.item ?? "품목 미상",
            },
            {
              key: "partner",
              label: "거래처",
              render: (row) => row.partner_name ?? row.partner_id ?? "미지정",
            },
            {
              key: "runtime",
              label: "Runtime",
              render: (row) => row.runtime_status,
            },
            //  🔴 판매는 자기 verdict 를 저장하지 않는다. 없는 것을 만들지 않는다.
            {
              key: "verdict",
              label: "Verdict",
              render: (row) => row.verdict ?? "판매 미보유",
            },
            {
              key: "end",
              label: "Master end code",
              mono: true,
              render: (row) => row.master_end_code ?? "없음",
            },
            {
              key: "req",
              label: "요청",
              mono: true,
              render: (row) => row.request_id ?? "없음",
            },
          ]}
        />
      )}
    </Panel>
  );
}
