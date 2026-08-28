import { ALL_LABELS, AXIS_KO, n, pct, won } from "@/lib/format";
import type { Scenario } from "@/lib/types";

import { Bar, CapMeter } from "./ui";

interface Row {
  k: string;
  hero?: boolean;
  cell: (s: Scenario) => React.ReactNode;
}

/**
 * 시나리오 비교표.
 *
 * ★ **만들지 않은 안의 열을 빼지 않는다.** 자리를 남기고 빗금으로 비운다.
 *   열이 통째로 사라지면 "원래 2안이구나"로 읽히고, 자리가 남아 지워져 있으면
 *   "있어야 할 것이 지워졌다"로 읽힌다. 두 기준일을 나란히 놓는 시연의 핵심이다.
 */
export function ComparisonTable({
  scenarios,
  financeCap,
  voidReason,
}: {
  scenarios: Scenario[];
  financeCap: number;
  voidReason: string | null;
}) {
  const byLabel = Object.fromEntries(scenarios.map((s) => [s.label, s]));
  const maxQty = Math.max(...scenarios.map((s) => s.total_qty_kg));

  const rows: Row[] = [
    {
      k: "커버 일수",
      cell: (s) => (
        <span className="num text-lg2 font-semibold">
          {s.coverage_days}
          <small className="ml-1.5 font-sans text-sm2 font-medium text-ink-3">일</small>
        </span>
      ),
    },
    {
      k: "총 매입량",
      hero: true,
      cell: (s) => (
        <>
          <span className="num text-hero font-semibold tracking-[-0.04em]">
            {n(s.total_qty_kg)}
            <small className="ml-1.5 font-sans text-md2 font-medium text-ink-3">kg</small>
          </span>
          <Bar ratio={s.total_qty_kg / maxQty} />
        </>
      ),
    },
    {
      k: "총 매입액",
      hero: true,
      cell: (s) => (
        <span className="num text-hero font-semibold tracking-[-0.04em]">
          {n(s.total_amount_krw)}
          <small className="ml-1.5 font-sans text-md2 font-medium text-ink-3">원</small>
        </span>
      ),
    },
    {
      k: "재무 상한 소진",
      cell: (s) => <CapMeter amount={s.total_amount_krw} cap={financeCap} />,
    },
    {
      k: "상한가",
      cell: (s) => (
        <span className="num text-lg2 font-semibold">
          {n(s.max_price)}
          <small className="ml-1.5 font-sans text-sm2 font-medium text-ink-3">원/kg</small>
        </span>
      ),
    },
    {
      k: "예상 마진율",
      cell: (s) => <span className="num text-lg2 font-semibold">{pct(s.expected_margin_rate)}</span>,
    },
    {
      k: "매입 회차",
      cell: (s) => (
        <span className="num text-lg2 font-semibold">
          {s.split_plan.length === 1 ? "일괄" : `${s.split_plan.length}회 분할`}
        </span>
      ),
    },
  ];

  return (
    <div className="overflow-hidden rounded-xl border border-rule bg-surface shadow-sm">
      <div className="overflow-x-auto">
        <table className="w-full min-w-[760px] border-collapse">
          <thead>
            <tr>
              <th className="w-[190px] border-r border-b-2 border-rule border-b-rule-2 bg-surface-2" />
              {ALL_LABELS.map((label) => {
                const s = byLabel[label];
                return (
                  <th
                    key={label}
                    className={`border-b-2 border-rule-2 bg-surface-2 px-6 py-5 text-right text-lg2 font-semibold tracking-tight ${
                      s ? "" : "text-ink-3"
                    }`}
                  >
                    {label}
                    <span className="mt-[3px] block font-mono text-xs2 font-medium text-ink-3">
                      {s ? `${AXIS_KO[s.strategy_type]} 축` : "해당 없음"}
                    </span>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {rows.map((r, ri) => (
              <tr key={r.k}>
                <td className="border-r border-b border-rule bg-surface-2 px-6 py-4 text-left text-sm2 font-medium whitespace-nowrap text-ink-2">
                  {r.k}
                </td>
                {ALL_LABELS.map((label) => {
                  const s = byLabel[label];
                  if (s) {
                    return (
                      <td
                        key={label}
                        className={`border-b border-rule px-6 text-right ${
                          r.hero ? "py-5" : "py-4"
                        }`}
                      >
                        {r.cell(s)}
                      </td>
                    );
                  }
                  // 빈 열은 첫 행에서 전체 행을 rowSpan 으로 덮는다
                  if (ri !== 0) return null;
                  return (
                    <td
                      key={label}
                      rowSpan={rows.length}
                      className="void-cell border-b border-rule px-6 text-center align-middle"
                    >
                      <div className="inline-flex max-w-[280px] flex-col items-center gap-2.5 rounded-xl border-2 border-dashed border-rule-2 bg-surface px-7 py-[22px]">
                        <div className="text-lg2 font-semibold tracking-tight text-ink-2">
                          만들지 않음
                        </div>
                        <div className="text-sm2 leading-relaxed text-ink-3">
                          {voidReason ?? "이번 실행에서는 만들어지지 않았다."}
                        </div>
                        <div className="font-mono text-xs2 text-ink-3">situation = uncertain</div>
                      </div>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="border-t border-rule bg-surface-2 px-6 py-3.5 text-sm2 text-ink-3">
        표의 모든 수치는 시뮬레이션 고정값이다 — 안별 상세의 근거에 출처가 하나씩 달려 있다. 재무
        매입 상한 {won(financeCap)} 기준.
      </div>
    </div>
  );
}
