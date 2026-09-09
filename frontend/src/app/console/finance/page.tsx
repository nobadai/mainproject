"use client";

/**
 * 재무 탭. 소유: **재무 파트.** 조회 전용입니다.
 *
 * ★ 그래프의 가로축이 여기만 다릅니다 — 다른 탭은 12칸 날짜축인데
 *   이 그래프는 최근 30일 일마감을 봅니다. 그래서 공용 날짜축을 안 쓰고
 *   백엔드가 같이 준 눈금(`chart.x_labels`)으로 그립니다.
 */

import { useSyncExternalStore } from "react";

import {
  DataTable,
  ErrorBox,
  Loading,
  Panel,
  SourceTag,
} from "@/components/console/Blocks";
import { useTab } from "@/components/console/useTab";
//  🔴 시연용 기준일 (`#431`). 시연이 끝나면 이 줄과 아래 `asOf` 를 지우고
//     `useTab` 의 `AS_OF` 로 되돌린다.
import { asOfSnapshot, serverAsOf, subscribeAsOf } from "@/lib/demo_as_of";
import { finance, type Card, type FinanceTab, type Stat, type Tone } from "@/lib/screen";

import { FinanceCashChart } from "./FinanceCashChart";

export default function FinancePage() {
  const asOf = useSyncExternalStore(subscribeAsOf, asOfSnapshot, serverAsOf);
  const { data, error } = useTab<FinanceTab>(asOf, () => finance(asOf));

  if (error) return <ErrorBox message={error} />;
  if (!data) return <Loading what="재무" />;

  return (
    <div className="mx-auto flex w-full max-w-[1400px] flex-col gap-4 sm:gap-5">
      <SourceTag sources={[data.source]} />
      <p className="m-0 text-[12px] leading-relaxed text-ink2 sm:text-[12.5px]">
        {data.read_only.text} · 근거 · 재무 마감 / 수금·지급 장부
      </p>

      {!data.has_data ? (
        <div className="rounded-lg border border-hair bg-panel px-5 py-8 text-center sm:py-10">
          <p className="m-0 text-[15px] font-semibold">이 날짜에는 아직 재무 기록이 없습니다.</p>
          <p className="mb-0 mt-1 text-[12px] text-ink2">
            재무 데이터가 저장된 이후 날짜를 선택해 주세요.
          </p>
        </div>
      ) : (
        <>
          <section className="rounded-lg border border-hair bg-panel px-4 py-4 sm:px-5">
            <p className="m-0 text-[11.5px] font-semibold text-ink2">현재 자금 상태</p>
            <p className="mb-0 mt-1 text-[17px] font-semibold leading-snug sm:text-[19px]">
              {data.explain.text}
            </p>
            {data.state_indicator && (
              <p className="mb-0 mt-2 text-[11.5px] text-ink2">{data.state_indicator}</p>
            )}
          </section>

          <MetricGrid items={data.stats} />

          {data.action_card && (
            <ActionSummary card={data.action_card} requestedAsOf={data.requested_as_of} />
          )}

          {data.state_cards.length > 1 && (
            <Panel title="자금 상태 비교" subtitle="저장된 재무 기준의 차이를 비교합니다">
              <StateComparison cards={data.state_cards} />
            </Panel>
          )}

          {data.cash_chart && (
            <Panel title="최근 30일 현금 흐름" subtitle="일별 마감값을 그대로 이은 것">
              <FinanceCashChart
                chart={data.cash_chart}
                availableStates={data.states.map((state) => state.key)}
              />
            </Panel>
          )}

          {data.flows.length > 0 && (
            <Panel title="기준일까지 누적 자금 흐름">
              <div className="grid gap-4 lg:grid-cols-2">
                <FlowGroup title="들어온 돈" flows={data.flows.filter((flow) => flow.group === "in")} />
                <FlowGroup title="나간 돈" flows={data.flows.filter((flow) => flow.group === "out")} />
              </div>
              <p className="mb-0 mt-3 text-[12px] text-ink2">
                판매로 잡힌 금액과 실제로 입금된 수금액은 서로 다른 값입니다.
              </p>
            </Panel>
          )}

          {data.balances.length > 0 && (
            <Panel title="정산 현황">
              <MetricGrid items={data.balances} compact />
              {data.balances_note && (
                <p className="m-0 text-[12px] leading-relaxed text-ink2">
                  {data.balances_note.text.replaceAll("**", "")}
                </p>
              )}
            </Panel>
          )}

          {data.closings && (
            <Panel title="최근 일별 마감" subtitle="상세 숫자가 필요할 때">
              <details>
                <summary className="cursor-pointer text-[13px] font-semibold">상세 보기</summary>
                <div className="mt-3">
                  <DataTable table={data.closings} />
                </div>
              </details>
            </Panel>
          )}
        </>
      )}
    </div>
  );
}

function MetricGrid({ items, compact = false }: { items: Stat[]; compact?: boolean }) {
  return (
    <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2 xl:grid-cols-4">
      {items.map((item) => (
        <div
          key={item.label}
          className={`flex min-w-0 flex-col rounded-lg border border-hair bg-panel px-4 ${compact ? "py-3.5" : "min-h-32 py-4"}`}
        >
          <span className="text-[11.5px] font-medium text-ink2">{item.label}</span>
          <div className="mt-2 flex min-w-0 items-baseline gap-1">
            <strong
              className="min-w-0 text-[clamp(1.35rem,3vw,1.8rem)] font-semibold leading-none tabular-nums"
              style={{ color: metricColor(item.label, item.tone) }}
            >
              {item.value}
            </strong>
            {item.unit && <span className="shrink-0 text-[11px] text-ink2">{item.unit}</span>}
          </div>
          {item.detail && (
            <span className="mt-auto pt-2 text-[11px] leading-snug text-ink2">{item.detail}</span>
          )}
        </div>
      ))}
    </div>
  );
}

function ActionSummary({ card, requestedAsOf }: { card: Card; requestedAsOf: string }) {
  return (
    <section className="overflow-hidden rounded-lg border border-hair bg-panel">
      <header className="border-b border-hair px-4 py-3 sm:px-5">
        <h2 className="m-0 text-[14px] font-semibold">{card.title}</h2>
      </header>
      <dl className="m-0 grid grid-cols-1 divide-y divide-hair sm:grid-cols-2 sm:divide-x sm:divide-y-0 xl:grid-cols-4">
        {card.stats.map((item) => {
          const isPastDueDate = item.label === "다음 수금 예정" && item.value < requestedAsOf;
          const label = isPastDueDate ? "가장 오래된 미수금" : item.label;
          const value = item.label === "다음 수금 예정" ? formatDate(item.value) : item.value;
          return (
            <div
              key={item.label}
              className="flex items-center justify-between gap-4 px-4 py-3 sm:block"
            >
              <dt className="text-[11.5px] text-ink2">{label}</dt>
              <dd
                className="m-0 text-right text-[15px] font-semibold tabular-nums sm:mt-1 sm:text-left"
                style={{ color: metricColor(label, isPastDueDate ? "bad" : item.tone) }}
              >
                {value}{item.unit ? ` ${item.unit}` : ""}
              </dd>
            </div>
          );
        })}
      </dl>
    </section>
  );
}

function StateComparison({ cards }: { cards: Card[] }) {
  const labels = ["현금 잔액", "최소 운영자금", "운영자금 여유", "현재 차입 잔액"];
  return (
    <div className="thin-scroll overflow-x-auto">
      <table className="w-full min-w-[560px] border-collapse text-[12px]">
        <thead>
          <tr className="border-b border-hair text-left text-ink2">
            <th className="px-3 py-2 font-medium">구분</th>
            {cards.map((card) => (
              <th key={card.key} className="px-3 py-2 text-right font-semibold text-ink">
                {card.title}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {labels.map((label) => (
            <tr key={label} className="border-b border-hair last:border-0">
              <th className="px-3 py-2.5 text-left font-medium text-ink2">{label}</th>
              {cards.map((card) => {
                const stat = card.stats.find((item) => item.label === label);
                return (
                  <td
                    key={card.key}
                    className="px-3 py-2.5 text-right font-semibold tabular-nums"
                    style={{ color: stat ? metricColor(label, stat.tone) : undefined }}
                  >
                    {stat ? `${stat.value}${stat.unit ? ` ${stat.unit}` : ""}` : "-"}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function metricColor(label: string, tone: Tone): string {
  if (label.includes("차입")) return "var(--color-ink)";
  if (tone === "good") return "var(--color-t-good)";
  if (tone === "warn") return "var(--color-t-warn)";
  if (tone === "bad") return "var(--color-t-bad)";
  return "var(--color-ink)";
}

function formatDate(value: string): string {
  const parts = value.split("-").map(Number);
  if (parts.length !== 3 || parts.some(Number.isNaN)) return value;
  return `${parts[1]}월 ${parts[2]}일`;
}

function FlowGroup({ title, flows }: { title: string; flows: FinanceTab["flows"] }) {
  return (
    <div>
      <h3 className="mb-2 mt-0 text-sm">{title}</h3>
      <div className="grid gap-2.5 [grid-template-columns:repeat(auto-fit,minmax(150px,1fr))]">
        {flows.map((flow) => (
          <div key={flow.label} className="flex min-w-0 flex-col gap-1 rounded-lg border border-hair px-3.5 py-3">
            <span className="text-[11.5px] text-ink2">{flow.label}</span>
            <strong className="text-[17px] font-semibold tabular-nums">{flow.value}</strong>
          </div>
        ))}
      </div>
    </div>
  );
}
