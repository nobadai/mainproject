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
      <Note note={data.read_only} />
      <StatRow items={data.stats} />

      <Panel
        title="저장된 재무 상태"
        subtitle="실제로 저장된 상태만 비교합니다"
      >
        <Note note={data.explain} />
        <div className="grid gap-3 [grid-template-columns:repeat(auto-fit,minmax(240px,1fr))]">
          {data.state_cards.map((card) => (
            <CardBlock key={card.key} card={card} />
          ))}
        </div>
      </Panel>

      <Panel title="최근 30일 현금 흐름" subtitle="일별 마감값을 그대로 이은 것">
        <LineChart chart={data.cash_chart} height={280} />
      </Panel>

      <Panel title="기준일까지 누적 자금 흐름" subtitle="원장 용어 대신 사람 말로">
        <div className="grid gap-2.5 [grid-template-columns:repeat(auto-fit,minmax(180px,1fr))]">
          {data.flows.map((f) => (
            <div
              key={f.label}
              className="flex min-w-0 flex-col gap-1 rounded-xl border px-3.5 py-3"
              style={{ borderColor: "var(--color-hair)" }}
            >
              <span className="text-[11.5px]" style={{ color: "var(--color-mut)" }}>
                {f.label}
              </span>
              <strong
                className="tabular font-mono text-[17px]"
                style={{
                  color:
                    f.tone === "good"
                      ? "var(--color-t-good)"
                      : f.tone === "warn"
                        ? "var(--color-t-warn)"
                        : undefined,
                }}
              >
                {f.value}
              </strong>
            </div>
          ))}
        </div>
      </Panel>

      <Panel title="받을 돈과 줄 돈" subtitle="지금 남아 있는 것만">
        <StatRow items={data.balances} />
        <Note note={data.balances_note} />
      </Panel>

      <Panel title="최근 일별 마감" subtitle="상세 숫자가 필요할 때">
        <DataTable table={data.closings} />
      </Panel>
    </>
  );
}
