"use client";

/**
 * 판매 시각화 — 기간별 · 품목별 · 거래처별 · 미수 구간.
 *
 * 🔴 **화면이 접지 않는다.** 날짜별 합계는 `/console/sales/trend` 가, 품목별은
 *    `/console/sales/summary` 가, 거래처별은 `/console/sales/partners` 가 이미 접어서
 *    준다. 목록을 받아 프론트에서 더하면 `limit` 이 걸린 일부만 더해 **합계가 조용히
 *    틀린다.**
 *
 * ⚠️ **`null` 은 0 이 아니다.** 값이 없는 항목은 막대에서 빼고 그 사실을 적는다.
 */

import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { PartnerRow } from "@/lib/console_api";
import { itemText, manwon, moneyWon, partnerText, shortDate, toNumber } from "../finance/user_text";
import type { SalesSummaryResponse, SalesTrendResponse } from "./sales_api";

const BAR_COLORS = [
  "var(--color-t-info)",
  "var(--color-t-good)",
  "var(--color-t-warn)",
  "var(--color-t-bad)",
  "var(--color-mut2)",
];

/* ── A. 기간별 매출 추이 ───────────────────────────────────────────────── */

type TrendPoint = { index: number; label: string; 매출: number; 공헌이익: number; 건수: number };

export function SalesTrendChart({ data }: { data: SalesTrendResponse }) {
  const points: TrendPoint[] = data.rows.map((row, index) => ({
    index,
    label: shortDate(row.sale_date),
    매출: toNumber(row.sales_amount_krw) ?? 0,
    공헌이익: toNumber(row.contribution_profit_krw) ?? 0,
    건수: row.sales_count,
  }));
  if (points.length === 0) return null;

  return (
    <figure className="m-0">
      <div className="h-[250px] w-full sm:h-[300px]">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={points} margin={{ top: 8, right: 12, bottom: 4, left: 8 }}>
            <CartesianGrid stroke="var(--color-grid)" vertical={false} />
            <XAxis
              dataKey="index"
              axisLine={false}
              tickLine={false}
              tick={{ fill: "var(--color-mut2)", fontSize: 15 }}
              tickFormatter={(index: number) => points[index]?.label ?? ""}
              minTickGap={24}
            />
            <YAxis
              axisLine={false}
              tickLine={false}
              tick={{ fill: "var(--color-mut2)", fontSize: 15 }}
              tickFormatter={(value: number) => manwon(value)}
              width={68}
            />
            <Tooltip
              cursor={{ fill: "var(--color-grid)" }}
              content={({ active, payload }) => {
                const point = (payload?.[0]?.payload ?? null) as TrendPoint | null;
                if (!active || !point) return null;
                return (
                  <Box title={point.label}>
                    <Line label="매출" value={point.매출} />
                    <Line label="공헌이익" value={point.공헌이익} tone="good" />
                    <Line label="판매 건수" value={point.건수} unit="건" />
                  </Box>
                );
              }}
            />
            <Bar dataKey="매출" fill="var(--color-t-info)" isAnimationActive={false} />
            <Bar dataKey="공헌이익" fill="var(--color-t-good)" isAnimationActive={false} />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <p className="mb-0 mt-2 text-[16px] leading-relaxed text-ink2">
        판매가 있었던 {points.length}일만 표시합니다 — 판 날이 없는 날을 0원으로 채우지 않습니다.
        {/* ⚠️ 가로축은 날짜가 아니라 «판매가 있었던 날» 의 차례다. 칸 간격을 실제 날짜
            간격으로 읽으면 하루 차이와 열흘 차이가 같아 보인다. */}{" "}
        칸 간격은 실제 날짜 간격을 뜻하지 않습니다.
      </p>
    </figure>
  );
}

/* ── B. 품목별 매출 ────────────────────────────────────────────────────── */

type ItemPoint = { name: string; 매출: number; 공헌이익: number; 수량: number };

export function SalesItemChart({ data }: { data: SalesSummaryResponse }) {
  const points: ItemPoint[] = data.items.map((row) => ({
    name: itemText(row.item_name, row.item_id),
    매출: toNumber(row.sales_amount_krw) ?? 0,
    공헌이익: toNumber(row.contribution_profit_krw) ?? 0,
    수량: toNumber(row.total_quantity_kg) ?? 0,
  }));
  if (points.length === 0) return null;

  return (
    <div className="h-[220px] w-full sm:h-[260px]">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={points} layout="vertical" margin={{ top: 4, right: 16, bottom: 4, left: 8 }}>
          <CartesianGrid stroke="var(--color-grid)" horizontal={false} />
          <XAxis
            type="number"
            axisLine={false}
            tickLine={false}
            tick={{ fill: "var(--color-mut2)", fontSize: 15 }}
            tickFormatter={(value: number) => manwon(value)}
          />
          <YAxis
            type="category"
            dataKey="name"
            axisLine={false}
            tickLine={false}
            tick={{ fill: "var(--color-mut2)", fontSize: 15 }}
            width={96}
          />
          <Tooltip
            cursor={{ fill: "var(--color-grid)" }}
            content={({ active, payload }) => {
              const point = (payload?.[0]?.payload ?? null) as ItemPoint | null;
              if (!active || !point) return null;
              return (
                <Box title={point.name}>
                  <Line label="매출" value={point.매출} />
                  <Line label="공헌이익" value={point.공헌이익} tone="good" />
                  <Line label="수량" value={point.수량} unit="kg" />
                </Box>
              );
            }}
          />
          <Bar dataKey="매출" isAnimationActive={false} radius={[0, 4, 4, 0]}>
            {points.map((point, index) => (
              <Cell key={point.name} fill={BAR_COLORS[index % BAR_COLORS.length]} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

/* ── C. 거래처별 매출 ──────────────────────────────────────────────────── */

type PartnerPoint = { name: string; 매출: number; 미수: number; 건수: number };

/** 화면에 담는 거래처 수. 잘린 것은 아래 문장이 **사실대로** 말한다. */
const PARTNER_TOP_N = 8;

export function SalesPartnerChart({ rows }: { rows: PartnerRow[] }) {
  const selling = rows
    .map((row) => ({
      name: partnerText(row.partner_name, row.partner_id),
      매출: toNumber(row.total_sales_krw) ?? 0,
      미수: toNumber(row.receivable_balance_krw) ?? 0,
      건수: row.total_sales_count,
    }))
    .filter((point) => point.매출 > 0)
    .sort((a, b) => b.매출 - a.매출);
  const points: PartnerPoint[] = selling.slice(0, PARTNER_TOP_N);
  const hidden = selling.length - points.length;
  if (points.length === 0) return null;

  return (
    <>
    <div className="h-[220px] w-full sm:h-[260px]">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={points} layout="vertical" margin={{ top: 4, right: 16, bottom: 4, left: 8 }}>
          <CartesianGrid stroke="var(--color-grid)" horizontal={false} />
          <XAxis
            type="number"
            axisLine={false}
            tickLine={false}
            tick={{ fill: "var(--color-mut2)", fontSize: 15 }}
            tickFormatter={(value: number) => manwon(value)}
          />
          <YAxis
            type="category"
            dataKey="name"
            axisLine={false}
            tickLine={false}
            tick={{ fill: "var(--color-mut2)", fontSize: 15 }}
            width={110}
          />
          <Tooltip
            cursor={{ fill: "var(--color-grid)" }}
            content={({ active, payload }) => {
              const point = (payload?.[0]?.payload ?? null) as PartnerPoint | null;
              if (!active || !point) return null;
              return (
                <Box title={point.name}>
                  <Line label="매출" value={point.매출} />
                  <Line label="미수금" value={point.미수} tone={point.미수 > 0 ? "bad" : undefined} />
                  <Line label="판매 건수" value={point.건수} unit="건" />
                </Box>
              );
            }}
          />
          <Bar dataKey="매출" isAnimationActive={false} radius={[0, 4, 4, 0]}>
            {points.map((point, index) => (
              <Cell key={point.name} fill={BAR_COLORS[index % BAR_COLORS.length]} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
      </div>
      <p className="mb-0 mt-2 text-[16px] text-ink2">
        {hidden > 0
          ? `매출이 있는 거래처 ${selling.length}곳 중 상위 ${points.length}곳입니다 - 나머지 ${hidden}곳은 표시하지 않았습니다.`
          : `매출이 있는 거래처 ${selling.length}곳을 모두 표시했습니다.`}
      </p>
    </>
  );
}

/* ── 공용 툴팁 조각 ────────────────────────────────────────────────────── */

function Box({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="min-w-52 rounded-lg border border-hair bg-panel p-3 text-[16px] shadow-lg">
      <p className="mb-2 mt-0 font-semibold">{title}</p>
      <dl className="m-0 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5">{children}</dl>
    </div>
  );
}

function Line({
  label,
  value,
  unit,
  tone,
}: {
  label: string;
  value: number;
  unit?: string;
  tone?: "good" | "bad";
}) {
  return (
    <>
      <dt className="text-ink2">{label}</dt>
      <dd
        className="m-0 text-right font-semibold tabular-nums"
        style={{ color: tone ? `var(--color-t-${tone})` : "var(--color-ink)" }}
      >
        {unit ? `${value.toLocaleString("ko-KR")} ${unit}` : moneyWon(value)}
      </dd>
    </>
  );
}
