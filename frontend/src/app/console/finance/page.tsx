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
  CardBlock,
  DataTable,
  ErrorBox,
  LineChart,
  Loading,
  Note,
  Panel,
  SourceTag,
  StatRow,
} from "@/components/console/Blocks";
import { useTab } from "@/components/console/useTab";
//  🔴 시연용 기준일 (`#431`). 시연이 끝나면 이 줄과 아래 `asOf` 를 지우고
//     `useTab` 의 `AS_OF` 로 되돌린다.
import { asOfSnapshot, serverAsOf, subscribeAsOf } from "@/lib/demo_as_of";
import { finance, type FinanceTab } from "@/lib/screen";

export default function FinancePage() {
  const asOf = useSyncExternalStore(subscribeAsOf, asOfSnapshot, serverAsOf);
  const { data, error } = useTab<FinanceTab>(asOf, () => finance(asOf));

  if (error) return <ErrorBox message={error} />;
  if (!data) return <Loading what="재무" />;

  return (
    <>
      <SourceTag sources={[data.source]} />
      <p className="m-0 text-[12px] leading-relaxed text-ink2">
        {data.read_only.text} · 근거 · 재무 마감 / 수금·지급 장부
      </p>

      {!data.has_data ? (
        <div className="rounded-lg border border-hair bg-panel px-4 py-5">
          <p className="m-0 text-sm font-semibold">이 날짜에는 아직 재무 기록이 없습니다.</p>
          <p className="mb-0 mt-1 text-[12px] text-ink2">
            재무 데이터가 저장된 이후 날짜를 선택해 주세요.
          </p>
        </div>
      ) : (
        <>
          <Panel title="현재 자금 상태">
            <Note note={data.explain} />
            {data.state_indicator && (
              <p className="mb-0 mt-2 text-[12px] text-ink2">{data.state_indicator}</p>
            )}
          </Panel>

          <StatRow items={data.stats} />

          {data.action_card && <CardBlock card={data.action_card} />}

          {data.state_cards.length > 1 && (
            <Panel title="자금 상태 비교" subtitle="저장된 재무 기준의 차이를 비교합니다">
              <div className="grid gap-3 [grid-template-columns:repeat(auto-fit,minmax(240px,1fr))]">
                {data.state_cards.map((card) => (
                  <CardBlock key={card.key} card={card} />
                ))}
              </div>
            </Panel>
          )}

          {data.cash_chart && (
            <Panel title="최근 30일 현금 흐름" subtitle="일별 마감값을 그대로 이은 것">
              <LineChart chart={data.cash_chart} height={280} />
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
              <StatRow items={data.balances} />
              <Note note={data.balances_note} />
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
    </>
  );
}

function FlowGroup({ title, flows }: { title: string; flows: FinanceTab["flows"] }) {
  return (
    <div>
      <h3 className="mb-2 mt-0 text-sm">{title}</h3>
      <div className="grid gap-2.5 [grid-template-columns:repeat(auto-fit,minmax(150px,1fr))]">
        {flows.map((flow) => (
          <div key={flow.label} className="flex min-w-0 flex-col gap-1 rounded-lg border border-hair px-3.5 py-3">
            <span className="text-[11.5px] text-ink2">{flow.label}</span>
            <strong className="tabular font-mono text-[17px]">{flow.value}</strong>
          </div>
        ))}
      </div>
    </div>
  );
}
