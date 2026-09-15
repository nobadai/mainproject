"use client";

/**
 * 자금 흐름 그래프.
 *
 * 🔴 **화면이 금액을 만들지 않는다.** 일마감이 저장한 `base_cash_balance_krw` ·
 *    `loan_cash_balance_krw` · `minimum_operating_cash_krw` 를 그대로 찍는다.
 *
 * ★ **계열을 켜고 끌 수 있어야 한다.** 실측(SIM-CHAIN-V13, 71일)에서 대출 포함과
 *   제외가 **항상 45,272,104 원 상수 간격**이라, 둘을 한 축에 같이 그리면 축 폭이
 *   66,104,805 원으로 벌어지고 각 계열 자체 변동(20,832,701 원)은 축의 31.5% 로
 *   눌린다. 대출 제외만 그리면 같은 변동이 축의 74.3% 를 쓴다 — 선이 평평해 보이는
 *   것은 데이터가 평평해서가 아니다.
 */

import { useState } from "react";
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

import type { ClosingItem } from "@/lib/console_api";
import { manwon, moneyWon, shortDate, toNumber } from "./user_text";

export type CashSeries = "base" | "loan";

type CashPoint = {
  index: number;
  label: string;
  base: number | null;
  loan: number | null;
  minimum: number | null;
};

type TooltipEntry = { payload: CashPoint };

const LABEL: Record<CashSeries, string> = {
  base: "대출 제외 현금",
  loan: "대출 포함 현금",
};

const COLOR: Record<CashSeries, string> = {
  base: "var(--color-t-info)",
  loan: "var(--color-t-good)",
};

export function FinanceCashChart({ rows }: { rows: ClosingItem[] }) {
  //  ★ 기본은 **대출 제외만** 이다. 둘 다 켠 상태로 시작하면 첫 화면이 이미 눌려 있다.
  const [shown, setShown] = useState<Set<CashSeries>>(new Set<CashSeries>(["base"]));
  const [showMinimum, setShowMinimum] = useState(true);

  function toggle(series: CashSeries) {
    setShown((current) => {
      const next = new Set(current);
      if (next.has(series)) next.delete(series);
      else next.add(series);
      //  ⚠️ 둘 다 끄면 빈 그래프가 남는다. 마지막 하나는 끄지 않는다.
      return next.size === 0 ? current : next;
    });
  }

  const points: CashPoint[] = rows.map((row, index) => ({
    index,
    label: shortDate(row.close_date),
    base: shown.has("base") ? toNumber(row.base_cash_balance_krw) : null,
    loan: shown.has("loan") ? toNumber(row.loan_cash_balance_krw) : null,
    minimum: showMinimum ? toNumber(row.minimum_operating_cash_krw) : null,
  }));

  const visible = points.flatMap((point) =>
    [point.base, point.loan, point.minimum].filter((value): value is number => value !== null),
  );
  if (visible.length === 0) return null;

  const [low, high] = domain(visible);
  const minimums = points
    .map((point) => point.minimum)
    .filter((value): value is number => value !== null);
  //  최소 운영현금이 기간 내내 한 값이면 선 대신 «부족 구간» 을 칠한다.
  const flatMinimum =
    minimums.length > 0 && minimums.every((value) => value === minimums[0]) ? minimums[0] : null;

  return (
    <figure className="m-0">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        {(["base", "loan"] as CashSeries[]).map((series) => (
          <SeriesToggle
            key={series}
            label={LABEL[series]}
            color={COLOR[series]}
            on={shown.has(series)}
            onClick={() => toggle(series)}
          />
        ))}
        <SeriesToggle
          label="최소 운영현금"
          color="var(--color-t-bad)"
          dashed
          on={showMinimum}
          onClick={() => setShowMinimum((value) => !value)}
        />
      </div>
      <div className="h-[280px] w-full sm:h-[340px]">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={points} margin={{ top: 12, right: 12, bottom: 4, left: 8 }}>
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
              domain={[low, high]}
              axisLine={false}
              tickLine={false}
              tick={{ fill: "var(--color-mut2)", fontSize: 11 }}
              tickFormatter={(value: number) => manwon(value)}
              width={72}
            />
            <Tooltip content={<CashTooltip />} cursor={{ stroke: "var(--color-hair)" }} />
            {/* 0원 선 — 현금이 음수로 내려간 날을 눈으로 찾게 한다 */}
            {low < 0 && <ReferenceLine y={0} stroke="var(--color-hair)" />}
            {flatMinimum !== null && (
              <>
                <ReferenceArea
                  y1={low}
                  y2={flatMinimum}
                  fill="var(--color-t-bad-bg)"
                  fillOpacity={0.45}
                />
                <ReferenceLine y={flatMinimum} stroke="var(--color-t-bad)" strokeDasharray="5 4" />
              </>
            )}
            {flatMinimum === null && minimums.length > 0 && (
              <Line
                type="linear"
                dataKey="minimum"
                stroke="var(--color-t-bad)"
                strokeDasharray="5 4"
                dot={false}
                connectNulls={false}
                isAnimationActive={false}
              />
            )}
            {(["base", "loan"] as CashSeries[]).map((series) =>
              shown.has(series) ? (
                <Line
                  key={series}
                  type="linear"
                  dataKey={series}
                  stroke={COLOR[series]}
                  strokeWidth={2.5}
                  dot={false}
                  activeDot={{ r: 4 }}
                  connectNulls={false}
                  isAnimationActive={false}
                />
              ) : null,
            )}
          </LineChart>
        </ResponsiveContainer>
      </div>
      <p className="mb-0 mt-2 text-[12px] leading-relaxed text-ink2">
        빨간 구간은 최소 운영현금에 못 미치는 범위입니다. 두 선을 함께 켜면 대출 금액만큼
        간격이 벌어져 각 선의 하루 변화가 작게 보입니다 — 변화를 보려면 한쪽만 켜세요.
        가로축은 마감된 날만 차례로 놓은 것이라, 칸 간격이 실제 날짜 간격을 뜻하지 않습니다.
      </p>
    </figure>
  );
}

/**
 * 🔴 **«여유» 가 어느 현금 기준인지 숨기지 않는다.** 전에는 `base ?? loan` 으로 하나만
 *    골라 «여유» 라고만 적어, 두 선을 함께 켜 놓은 사람은 그 숫자가 어느 선의 것인지
 *    알 수 없었다. 켜져 있는 계열마다 따로 적는다.
 */
function CashTooltip({ active, payload }: { active?: boolean; payload?: TooltipEntry[] }) {
  const point = payload?.[0]?.payload;
  if (!active || !point) return null;
  const gap = (cash: number | null) =>
    cash !== null && point.minimum !== null ? cash - point.minimum : null;
  const baseGap = gap(point.base);
  const loanGap = gap(point.loan);
  const both = point.base !== null && point.loan !== null;
  return (
    <div className="min-w-56 rounded-lg border border-hair bg-panel p-3 text-[12px] shadow-lg">
      <p className="mb-2 mt-0 font-semibold">{point.label}</p>
      <dl className="m-0 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5">
        {point.base !== null && <Row label={LABEL.base} value={point.base} />}
        {point.loan !== null && <Row label={LABEL.loan} value={point.loan} />}
        {point.minimum !== null && <Row label="최소 운영현금" value={point.minimum} />}
        {baseGap !== null && (
          <Row
            label={both ? "대출 제외 기준 여유" : "운영 여유"}
            value={baseGap}
            signed
            tone={baseGap < 0 ? "bad" : "good"}
          />
        )}
        {loanGap !== null && (
          <Row
            label={both ? "대출 포함 기준 여유" : "운영 여유"}
            value={loanGap}
            signed
            tone={loanGap < 0 ? "bad" : "good"}
          />
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

function SeriesToggle({
  label,
  color,
  on,
  dashed = false,
  onClick,
}: {
  label: string;
  color: string;
  on: boolean;
  dashed?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={on}
      className="inline-flex items-center gap-2 rounded-full border px-3 py-1 text-[11.5px]"
      style={{
        borderColor: on ? color : "var(--color-hair)",
        opacity: on ? 1 : 0.5,
      }}
    >
      <span
        aria-hidden
        className="block w-4 border-t-2"
        style={{ borderColor: color, borderStyle: dashed ? "dashed" : "solid" }}
      />
      {label}
    </button>
  );
}

function domain(values: number[]): [number, number] {
  const low = Math.min(...values);
  const high = Math.max(...values);
  const span = high - low;
  const padding = span > 0 ? span * 0.12 : Math.max(Math.abs(high) * 0.08, 1);
  return [Math.floor(low - padding), Math.ceil(high + padding)];
}
