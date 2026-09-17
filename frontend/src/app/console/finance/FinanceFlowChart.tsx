"use client";

/**
 * 현금이 어디서 들어오고 어디로 나갔는가.
 *
 * 🔴 **화면이 합계를 만들지 않는다.** 일마감이 저장한 네 칸을 그대로 쌓는다. 유출은
 *    음수 방향으로 그려 «나간 돈» 이 눈으로 구분되게만 한다 — 값을 바꾸는 것이 아니라
 *    같은 값을 아래쪽에 찍는 것이다.
 *
 * ⚠️ **0 과 «저장 안 됨» 을 가른다.** 물류비·급여/이자가 전 기간 0 인 실행이 있고,
 *   그것은 «비용이 없었다» 가 아니라 **그 칸이 아직 채워지지 않은 것**일 수 있다.
 *   합계가 0 인 항목은 범례에서 그 사실을 적는다.
 */

import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { ClosingItem } from "@/lib/console_api";
import { manwon, moneyWon, shortDate, toNumber } from "./user_text";

type FlowPoint = {
  index: number;
  label: string;
  매입대금: number;
  물류비: number;
  "급여·이자": number;
  수금: number;
  순현금: number | null;
};

const OUTFLOW = [
  { key: "매입대금" as const, color: "var(--color-t-bad)" },
  { key: "물류비" as const, color: "var(--color-t-warn)" },
  { key: "급여·이자" as const, color: "var(--color-mut2)" },
];

export function FinanceFlowChart({ rows }: { rows: ClosingItem[] }) {
  const orderedRows = [...rows].sort((left, right) => left.close_date.localeCompare(right.close_date));
  const points: FlowPoint[] = orderedRows.map((row, index) => ({
    index,
    label: shortDate(row.close_date),
    //  유출은 아래로 — 부호를 뒤집어 «방향» 만 표현한다.
    매입대금: -(toNumber(row.purchase_cash_out_krw) ?? 0),
    물류비: -(toNumber(row.logistics_cash_out_krw) ?? 0),
    "급여·이자": -(toNumber(row.payroll_interest_cash_out_krw) ?? 0),
    수금: toNumber(row.collection_cash_in_krw) ?? 0,
    순현금: toNumber(row.base_net_cash_krw),
  }));
  if (points.length === 0) return null;

  //  전 기간 0 인 항목은 «값이 없다» 고 적는다 — 빈 막대를 설명 없이 두지 않는다.
  const empty = OUTFLOW.filter((series) =>
    points.every((point) => point[series.key] === 0),
  ).map((series) => series.key);

  return (
    <figure className="m-0">
      <div className="h-[260px] w-full sm:h-[320px]">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={points} margin={{ top: 8, right: 12, bottom: 4, left: 8 }} stackOffset="sign">
            <CartesianGrid stroke="var(--color-grid)" vertical={false} />
            <XAxis
              dataKey="index"
              axisLine={false}
              tickLine={false}
              tick={{ fill: "var(--color-mut2)", fontSize: 11 }}
              tickFormatter={(index: number) => points[index]?.label ?? ""}
              minTickGap={28}
            />
            <YAxis
              axisLine={false}
              tickLine={false}
              tick={{ fill: "var(--color-mut2)", fontSize: 11 }}
              tickFormatter={(value: number) => manwon(value)}
              width={72}
            />
            <Tooltip content={<FlowTooltip />} cursor={{ fill: "var(--color-grid)" }} />
            <Legend wrapperStyle={{ fontSize: 11.5 }} />
            <ReferenceLine y={0} stroke="var(--color-hair)" />
            <Bar dataKey="수금" stackId="flow" fill="var(--color-t-good)" isAnimationActive={false} />
            {OUTFLOW.map((series) => (
              <Bar
                key={series.key}
                dataKey={series.key}
                stackId="flow"
                fill={series.color}
                isAnimationActive={false}
              />
            ))}
          </BarChart>
        </ResponsiveContainer>
      </div>
      <p className="mb-0 mt-2 text-[12px] leading-relaxed text-ink2">
        위쪽은 들어온 돈, 아래쪽은 나간 돈입니다. 가로축은 마감된 날만 차례로 놓은 것이라,
        칸 간격이 실제 날짜 간격을 뜻하지 않습니다.
        {empty.length > 0 && (
          <>
            {" "}
            {empty.join(" · ")}는 이 기간 저장된 값이 모두 0입니다 — 비용이 없었다는 뜻일 수도,
            아직 집계되지 않았다는 뜻일 수도 있어 화면이 대신 채우지 않습니다.
          </>
        )}
      </p>
    </figure>
  );
}

type TooltipEntry = { payload: FlowPoint };

function FlowTooltip({ active, payload }: { active?: boolean; payload?: TooltipEntry[] }) {
  const point = payload?.[0]?.payload;
  if (!active || !point) return null;
  return (
    <div className="min-w-56 rounded-lg border border-hair bg-panel p-3 text-[12px] shadow-lg">
      <p className="mb-2 mt-0 font-semibold">{point.label}</p>
      <dl className="m-0 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5">
        <Row label="수금" value={point.수금} tone="good" />
        <Row label="매입대금" value={-point.매입대금} tone="bad" />
        <Row label="물류비" value={-point.물류비} tone="bad" />
        <Row label="급여·이자" value={-point["급여·이자"]} tone="bad" />
        {point.순현금 !== null && (
          <Row label="순현금" value={point.순현금} signed tone={point.순현금 < 0 ? "bad" : "good"} />
        )}
      </dl>
    </div>
  );
}

function Row({
  label,
  value,
  signed = false,
  tone,
}: {
  label: string;
  value: number;
  signed?: boolean;
  tone?: "good" | "bad";
}) {
  return (
    <>
      <dt className="text-ink2">{label}</dt>
      <dd
        className="m-0 text-right font-semibold tabular-nums"
        style={{ color: tone ? `var(--color-t-${tone})` : "var(--color-ink)" }}
      >
        {signed && value > 0 ? "+" : ""}
        {moneyWon(value)}
      </dd>
    </>
  );
}
