"use client";

import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceArea,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { Chart } from "@/lib/screen";

type CashPoint = {
  index: number;
  label: string;
  current: number | null;
  loan: number | null;
  minimum: number | null;
};

type TooltipEntry = { payload: CashPoint };

const SERIES = {
  current: "현재 자금만 사용",
  loan: "대출 포함",
  minimum: "최소 유지해야 할 현금",
};

export function FinanceCashChart({
  chart,
  availableStates,
}: {
  chart: Chart;
  availableStates: string[];
}) {
  const currentSeries = chart.series.find((series) => series.name === SERIES.current);
  const loanSeries = chart.series.find((series) => series.name === SERIES.loan);
  const minimumSeries = chart.series.find((series) => series.name === SERIES.minimum);
  const showCurrent = availableStates.includes("base") && Boolean(currentSeries);
  const showLoan = availableStates.includes("loan") && Boolean(loanSeries);
  const count = Math.max(
    chart.x_labels.length,
    currentSeries?.data.length ?? 0,
    loanSeries?.data.length ?? 0,
    minimumSeries?.data.length ?? 0,
  );
  const points: CashPoint[] = Array.from({ length: count }, (_, index) => ({
    index,
    label: chart.x_labels[index] ?? "",
    current: showCurrent ? (currentSeries?.data[index] ?? null) : null,
    loan: showLoan ? (loanSeries?.data[index] ?? null) : null,
    minimum: minimumSeries?.data[index] ?? null,
  }));
  const visibleValues = points.flatMap((point) =>
    [point.current, point.loan, point.minimum].filter((value): value is number => value !== null),
  );

  if (visibleValues.length === 0) return null;

  const [domainMin, domainMax] = chartDomain(visibleValues);
  const minimumValues = points
    .map((point) => point.minimum)
    .filter((value): value is number => value !== null);
  const fixedMinimum =
    minimumValues.length > 0 && minimumValues.every((value) => value === minimumValues[0])
      ? minimumValues[0]
      : null;

  return (
    <figure className="m-0" aria-labelledby="finance-cash-chart-title">
      <figcaption id="finance-cash-chart-title" className="sr-only">
        최근 30일의 보유 현금과 최소 운영자금 비교
      </figcaption>
      <div className="mb-3 flex flex-wrap gap-x-5 gap-y-2 text-[12px] text-ink2" aria-label="그래프 범례">
        {showCurrent && <LegendItem color="var(--color-t-info)" label="현재 자금" />}
        {showLoan && <LegendItem color="var(--color-t-good)" label="대출 포함" />}
        {minimumValues.length > 0 && (
          <LegendItem color="var(--color-t-bad)" label="최소 운영자금" dashed />
        )}
      </div>
      <div className="h-[260px] w-full sm:h-[320px]">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={points} margin={{ top: 12, right: 12, bottom: 4, left: 12 }}>
            <CartesianGrid stroke="var(--color-grid)" vertical={false} />
            <XAxis
              dataKey="index"
              axisLine={false}
              tickLine={false}
              tick={{ fill: "var(--color-mut2)", fontSize: 11 }}
              tickFormatter={(index: number) => points[index]?.label ?? ""}
              minTickGap={24}
            />
            <YAxis
              domain={[domainMin, domainMax]}
              axisLine={false}
              tickLine={false}
              tick={{ fill: "var(--color-mut2)", fontSize: 11 }}
              tickFormatter={(value: number) => `${formatNumber(value)}만원`}
              width={92}
            />
            <Tooltip content={<CashTooltip />} cursor={{ stroke: "var(--color-hair)" }} />
            {fixedMinimum !== null && (
              <>
                <ReferenceArea
                  y1={domainMin}
                  y2={fixedMinimum}
                  fill="var(--color-t-bad-bg)"
                  fillOpacity={0.45}
                />
                <ReferenceLine
                  y={fixedMinimum}
                  stroke="var(--color-t-bad)"
                  strokeDasharray="5 4"
                />
              </>
            )}
            {fixedMinimum === null && minimumValues.length > 0 && (
              <Line
                type="linear"
                dataKey="minimum"
                name="최소 운영자금"
                stroke="var(--color-t-bad)"
                strokeDasharray="5 4"
                dot={false}
                connectNulls={false}
                isAnimationActive={false}
              />
            )}
            {showCurrent && (
              <Line
                type="linear"
                dataKey="current"
                name="현재 자금"
                stroke="var(--color-t-info)"
                strokeWidth={2.5}
                dot={false}
                activeDot={{ r: 4 }}
                connectNulls={false}
                isAnimationActive={false}
              />
            )}
            {showLoan && (
              <Line
                type="linear"
                dataKey="loan"
                name="대출 포함"
                stroke="var(--color-t-good)"
                strokeWidth={2.5}
                dot={false}
                activeDot={{ r: 4 }}
                connectNulls={false}
                isAnimationActive={false}
              />
            )}
          </LineChart>
        </ResponsiveContainer>
      </div>
      <p className="mb-0 mt-2 text-[12px] leading-relaxed text-ink2">
        현금이 최소 운영자금 아래로 내려가면 운영 여유가 부족한 상태입니다.
      </p>
    </figure>
  );
}

function CashTooltip({ active, payload }: { active?: boolean; payload?: TooltipEntry[] }) {
  const point = payload?.[0]?.payload;
  if (!active || !point) return null;
  const referenceCash = point.current ?? point.loan;
  const buffer =
    referenceCash !== null && point.minimum !== null ? referenceCash - point.minimum : null;

  return (
    <div className="min-w-52 rounded-lg border border-hair bg-panel p-3 text-[12px] shadow-lg">
      <p className="mb-2 mt-0 font-semibold">{formatChartDate(point.label)}</p>
      <dl className="m-0 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5">
        {point.current !== null && <TooltipValue label="현재 자금" value={point.current} />}
        {point.loan !== null && <TooltipValue label="대출 포함" value={point.loan} />}
        {point.minimum !== null && <TooltipValue label="최소 운영자금" value={point.minimum} />}
        {buffer !== null && (
          <TooltipValue label="운영 여유" value={buffer} signed tone={buffer < 0 ? "bad" : "good"} />
        )}
      </dl>
    </div>
  );
}

function TooltipValue({
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
  const color = tone ? `var(--color-t-${tone})` : "var(--color-ink)";
  return (
    <>
      <dt className="text-ink2">{label}</dt>
      <dd className="m-0 text-right font-semibold tabular-nums" style={{ color }}>
        {signed && value > 0 ? "+" : ""}{formatNumber(value)}만원
      </dd>
    </>
  );
}

function LegendItem({ color, label, dashed = false }: { color: string; label: string; dashed?: boolean }) {
  return (
    <span className="inline-flex items-center gap-2">
      <span
        aria-hidden
        className="block w-5 border-t-2"
        style={{ borderColor: color, borderStyle: dashed ? "dashed" : "solid" }}
      />
      {label}
    </span>
  );
}

function chartDomain(values: number[]): [number, number] {
  const low = Math.min(...values);
  const high = Math.max(...values);
  const span = high - low;
  const padding = span > 0 ? span * 0.12 : Math.max(Math.abs(high) * 0.08, 1);
  return [Math.floor(low - padding), Math.ceil(high + padding)];
}

function formatNumber(value: number): string {
  return Math.round(value).toLocaleString("ko-KR");
}

function formatChartDate(value: string): string {
  if (!value.includes("/")) return "일별 마감";
  const [month, day] = value.split("/");
  return `${Number(month)}월 ${Number(day)}일`;
}
