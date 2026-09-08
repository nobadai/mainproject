"use client";

/**
 * 재무 탭. 소유: **재무 파트.** 조회 전용입니다.
 *
 * ★ 그래프의 가로축이 여기만 다릅니다 — 다른 탭은 12칸 날짜축인데
 *   이 그래프는 12월 한 달(30칸)입니다. 그래서 공용 날짜축을 안 쓰고
 *   백엔드가 같이 준 눈금(`chart.x_labels`)으로 그립니다.
 */

import { useState } from "react";

import {
  DataTable,
  ErrorBox,
  LineChart,
  Loading,
  Note,
  Panel,
  SourceTag,
  StatRow,
  TabButtons,
} from "@/components/console/Blocks";
import { AS_OF, useTab } from "@/components/console/useTab";
import { finance, type FinanceTab } from "@/lib/screen";

export default function FinancePage() {
  const [state, setState] = useState("base");
  const { data, error } = useTab<FinanceTab>(state, () => finance(AS_OF, state));

  if (error) return <ErrorBox message={error} />;
  if (!data) return <Loading what="재무" />;

  return (
    <>
      <SourceTag sources={[data.source]} />
      <Note note={data.read_only} />
      <StatRow items={data.stats} />

      <Panel
        title="지금 자금 상태"
        subtitle="저장된 상태의 현금 · 운영자금 · 받을 돈 · 부채"
        right={
          <TabButtons
            items={data.states.map((s) => ({ key: s.key, label: s.label.split(" · ")[0] }))}
            value={data.selected}
            onChange={setState}
          />
        }
        footer={`조회 표: ${data.tables_read.join(" · ")}`}
      >
        <Note note={data.explain} />
      </Panel>

      <Panel title="한 달 동안 현금이 어떻게 움직였나" subtitle="일별 마감값을 그대로 이은 것">
        <LineChart chart={data.cash_chart} height={280} />
      </Panel>

      <Panel title="이번 달 돈의 흐름" subtitle="원장 용어 대신 사람 말로">
        <div className="grid gap-2.5 [grid-template-columns:repeat(auto-fit,minmax(180px,1fr))]">
          {data.flows.map((f) => (
            <div
              key={f.term}
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
              <small className="font-mono text-[10px]" style={{ color: "var(--color-mut2)" }}>
                {f.term}
              </small>
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
