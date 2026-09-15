"use client";

import { useEffect, useState } from "react";

import { ApiError, burnIn } from "@/lib/api";
import { formatKoreanDate } from "@/lib/procurementLabels";
import type { BurnIn, DailyClosing } from "@/lib/types";

/**
 * 번인 구간 — **에이전트가 판단하기 전에 회사가 어떻게 왔는가.**
 *
 * 🔴 **왜 이 화면이 있나.** 에이전트가 12-31 에 *"살 안이 없다"* 고 답하는데,
 * 그 앞 30일을 안 보면 **시스템이 고장 난 것처럼 읽힌다.** 무차입 현금이
 * 5,820만원에서 -1,328만원까지 떨어지고 미수금이 7,305만원 잠긴 회사에게
 * *"지금 사지 마라"* 는 정상 판단이다. 결론 옆에 경로를 둔다.
 *
 * ★ **읽기 전용이다.** 실제 서비스 사용자에게 보이는 화면이라 개발 경고 · 코드는
 *   싣지 않는다 (2026-09-15 결정).
 *
 * ★ **값을 만들지 않는다.** 증감·합계를 화면이 계산하기 시작하면 재무가 내는
 *   숫자와 갈릴 자리가 생긴다. 서버가 준 값만 그린다.
 */

const 만원 = 10_000;

function money(value: number | null): string {
  if (value === null) return "—";
  return `${Math.round(value / 만원).toLocaleString("ko-KR")}만`;
}

/** 무차입 현금 곡선. **0 선을 반드시 그린다** — 마이너스로 내려간 것이 요점이다. */
function CashCurve({ rows }: { rows: DailyClosing[] }) {
  const values = rows.map((r) => r.base_cash_balance_krw ?? 0);
  if (values.length < 2) return null;

  const max = Math.max(...values, 0);
  const min = Math.min(...values, 0);
  const span = max - min || 1;
  const W = 640;
  const H = 150;
  const x = (i: number) => (i / (values.length - 1)) * W;
  const y = (v: number) => H - ((v - min) / span) * H;
  const zero = y(0);

  const line = values.map((v, i) => `${i === 0 ? "M" : "L"}${x(i)},${y(v)}`).join(" ");
  const area = `${line} L${W},${zero} L0,${zero} Z`;

  return (
    <figure className="m-0">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label={`무차입 현금 잔고가 ${money(values[0])}원에서 ${money(
          values[values.length - 1],
        )}원으로 내려간 30일 곡선`}
        className="block h-auto w-full"
      >
        <path d={area} fill="var(--color-warn-wash)" />
        {/* 0 선 — 이 선을 넘어간 것이 이 그림의 전부다 */}
        <line
          x1="0"
          x2={W}
          y1={zero}
          y2={zero}
          stroke="var(--color-faint)"
          strokeDasharray="3 3"
          strokeWidth="1"
        />
        <path d={line} fill="none" stroke="var(--color-warn)" strokeWidth="2" />
        <circle cx={x(values.length - 1)} cy={y(values[values.length - 1])} r="3.5" fill="var(--color-warn)" />
      </svg>
      <figcaption className="mt-1 text-[11px] text-faint">
        무차입 기준 현금 잔고 · 점선이 0원 — 재무가 답하는 가용 현금은 여기에 대출
        실행분이 더해진 값입니다
      </figcaption>
    </figure>
  );
}

export function BurnInPanel() {
  const [data, setData] = useState<BurnIn | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    burnIn()
      .then(setData)
      .catch((e) =>
        setError(
          e instanceof ApiError && e.status === 0
            ? "서버에 연결하지 못했습니다. 잠시 뒤 다시 시도해 주세요."
            : "지난 30일 기록을 불러오지 못했습니다. 잠시 뒤 다시 시도해 주세요.",
        ),
      );
  }, []);

  if (error)
    return (
      <p className="m-0 rounded-lg border border-warn/40 bg-warn-wash px-3 py-2 text-[13px] text-warn">
        {error}
      </p>
    );
  if (!data) return <p className="m-0 text-[13px] text-faint">불러오는 중…</p>;

  const rows = data.closings;
  const first = rows[0];
  const last = rows[rows.length - 1];
  const 안닫힌날 = rows.filter((r) => !r.closed).length;

  return (
    <div className="flex flex-col gap-4">
      <header>
        <h2 className="m-0 text-base font-semibold">
          자동 판단을 시작하기 전 {rows.length}일
        </h2>
        <p className="m-0 mt-1 text-[12.5px] text-muted">
          {formatKoreanDate(data.period_start)} ~ {formatKoreanDate(data.period_end)} · 이 기간은{" "}
          <b className="text-ink">사람이 운영한 기록</b>입니다. 자동 판단은{" "}
          <b className="text-accent-ink">{formatKoreanDate(data.as_of)}</b>부터 시작합니다.
        </p>
      </header>

      <CashCurve rows={rows} />

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {[
          { k: "현금 (시작)", v: money(first?.base_cash_balance_krw ?? null) },
          { k: "현금 (마지막)", v: money(last?.base_cash_balance_krw ?? null), warn: true },
          { k: "미수금", v: money(last?.receivables_balance_krw ?? null) },
          { k: "재고", v: `${(last?.inventory_qty_kg ?? 0).toLocaleString("ko-KR")}kg` },
        ].map((cell) => (
          <div key={cell.k} className="rounded-lg border border-line bg-sunk px-3 py-2">
            <p className="m-0 text-[10.5px] uppercase tracking-[0.1em] text-faint">
              {cell.k}
            </p>
            <p
              className={`m-0 mt-0.5 font-mono text-[15px] font-semibold tabular-nums ${
                cell.warn ? "text-warn" : "text-ink"
              }`}
            >
              {cell.v}
            </p>
          </div>
        ))}
      </div>

      {안닫힌날 > 0 && (
        <p className="m-0 rounded-lg border border-line-soft bg-sunk px-3 py-2.5 text-[12.5px] text-warn">
          아직 마감되지 않은 날이 {안닫힌날}일 있습니다. 그날 숫자는 바뀔 수 있습니다.
        </p>
      )}

      <details className="rounded-lg border border-line bg-surface">
        <summary className="cursor-pointer px-3 py-2 text-[13px] font-semibold">
          일별 마감 {rows.length}일
        </summary>
        <div className="overflow-x-auto px-3 pb-3">
          <table className="w-full min-w-[520px] border-collapse text-[12.5px]">
            <thead>
              <tr className="text-left text-faint">
                <th className="py-1 font-medium">일자</th>
                <th className="py-1 text-right font-medium">현금(무차입)</th>
                <th className="py-1 text-right font-medium">미수금</th>
                <th className="py-1 text-right font-medium">매출</th>
                <th className="py-1 text-right font-medium">회수</th>
                <th className="py-1 text-right font-medium">매입지출</th>
              </tr>
            </thead>
            <tbody className="font-mono tabular-nums">
              {rows.map((r) => (
                <tr key={r.close_date} className="border-t border-line-soft">
                  <td className="py-1">{r.close_date}</td>
                  <td
                    className={`py-1 text-right ${
                      (r.base_cash_balance_krw ?? 0) < 0 ? "text-warn" : ""
                    }`}
                  >
                    {money(r.base_cash_balance_krw)}
                  </td>
                  <td className="py-1 text-right">{money(r.receivables_balance_krw)}</td>
                  <td className="py-1 text-right">{money(r.sales_recognized_krw)}</td>
                  <td className="py-1 text-right">{money(r.collection_cash_in_krw)}</td>
                  <td className="py-1 text-right">{money(r.purchase_cash_out_krw)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}
