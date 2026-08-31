"use client";

import type { DecisionOut, Scenario } from "@/lib/types";

/**
 * 승인한 안이 **무엇을 하기로 한 것인가** — 승인 직후 화면.
 *
 * 🔴 **"오늘 산 것" 이 아니다.** 승인은 기록이고 발주는 이 시스템 밖이다. 재고도
 *   현금도 안 바뀐다 — 그래서 *"오늘 구매 결과"* 라는 것이 아직 존재하지 않는다.
 *   **여기 있는 것은 전부 계획이다.** 그 사실을 화면이 스스로 적는다.
 *
 * ★ **값을 만들지 않는다.** 서버가 준 안의 칸을 그대로 편다 — 합계를 다시 세지
 *   않는다. 화면이 세기 시작하면 보고서·리포트와 숫자가 갈린다.
 */

const 원 = (value: unknown): string => {
  const n = Number(value);
  return Number.isFinite(n) ? `${Math.round(n).toLocaleString("ko-KR")}원` : "—";
};

const kg = (value: unknown): string => {
  const n = Number(value);
  return Number.isFinite(n) ? `${n.toLocaleString("ko-KR")}kg` : "—";
};

interface SplitLeg {
  seq?: number;
  date?: string;
  qty_kg?: number;
}

interface PaymentLeg {
  seq?: number;
  purchase_date?: string;
  payment_date?: string;
  qty_kg?: number;
  amount_krw?: number;
  amount_max_krw?: number;
}

interface SourcingLeg {
  market?: string;
  grade?: string;
  qty_kg?: number;
  grade_unit_price?: number;
}

export function ApprovedPlan({
  scenario,
  decision,
}: {
  scenario: Scenario;
  decision: DecisionOut;
}) {
  const splits = (scenario.split_plan as SplitLeg[] | undefined) ?? [];
  const payments = (scenario.payment_schedule as PaymentLeg[] | undefined) ?? [];
  const sourcing = (scenario.sourcing_plan as SourcingLeg[] | undefined) ?? [];

  return (
    <div className="flex flex-col gap-3 rounded-xl border border-accent/35 bg-accent-wash/40 p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="m-0 text-base font-semibold text-accent-ink">
          ‘{String(scenario.label ?? "")}’ 안으로 결정했습니다
        </h3>
        <span className="font-mono text-[11.5px] text-faint">
          {decision.decision_seq}회차 · {decision.decided_by} · {decision.request_id}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {[
          { k: "총 수량", v: kg(scenario.total_qty_kg) },
          { k: "총 금액", v: 원(scenario.total_amount_krw) },
          { k: "분할", v: `${splits.length || 1}회` },
          { k: "커버", v: scenario.coverage_days ? `${scenario.coverage_days}일` : "—" },
        ].map((cell) => (
          <div key={cell.k} className="rounded-lg border border-line bg-surface px-3 py-2">
            <p className="m-0 text-[10.5px] uppercase tracking-[0.1em] text-faint">{cell.k}</p>
            <p className="m-0 mt-0.5 font-mono text-[14.5px] font-semibold tabular-nums">
              {cell.v}
            </p>
          </div>
        ))}
      </div>

      {splits.length > 0 && (
        <Section title="언제 발주하나">
          <Table
            head={["회차", "발주일", "수량"]}
            rows={splits.map((s, i) => [
              String(s.seq ?? i + 1),
              String(s.date ?? "—"),
              kg(s.qty_kg),
            ])}
          />
        </Section>
      )}

      {sourcing.length > 0 && (
        <Section title="어디서 사나">
          <Table
            head={["시장", "등급", "수량", "단가"]}
            rows={sourcing.map((s) => [
              String(s.market ?? "—"),
              String(s.grade ?? "—"),
              kg(s.qty_kg),
              원(s.grade_unit_price),
            ])}
          />
        </Section>
      )}

      {payments.length > 0 && (
        <Section title="언제 얼마를 지급하나">
          <Table
            head={["회차", "매입일", "지급일", "금액", "최대"]}
            rows={payments.map((p, i) => [
              String(p.seq ?? i + 1),
              String(p.purchase_date ?? "—"),
              String(p.payment_date ?? "—"),
              원(p.amount_krw),
              원(p.amount_max_krw),
            ])}
          />
        </Section>
      )}

      {/* 🔴 이 화면이 무엇이 아닌지 적는다 — 안 적으면 "오늘 산 것" 으로 읽힌다 */}
      <p className="m-0 rounded-lg border border-line-soft bg-sunk px-3 py-2 text-[12.5px] text-muted">
        🔴 <b className="text-ink">여기 있는 것은 전부 계획입니다.</b> 승인은 기록이고
        실제 발주는 이 시스템 밖입니다 — 재고와 현금은 아직 바뀌지 않았습니다. 하루가
        지나 실제로 무엇이 들어왔는지는 <b className="text-ink">아직 볼 수 없습니다.</b>
      </p>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <p className="mb-1.5 text-[10.5px] font-semibold uppercase tracking-[0.12em] text-faint">
        {title}
      </p>
      {children}
    </div>
  );
}

function Table({ head, rows }: { head: string[]; rows: string[][] }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-line bg-surface">
      <table className="w-full min-w-[380px] border-collapse text-[12.5px]">
        <thead>
          <tr className="bg-sunk text-[10.5px] uppercase tracking-wide text-muted">
            {head.map((h) => (
              <th key={h} className="px-3 py-1.5 text-left font-semibold">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="font-mono tabular-nums">
          {rows.map((cells, i) => (
            <tr key={i} className="border-t border-line-soft">
              {cells.map((c, j) => (
                <td key={j} className="px-3 py-1.5">
                  {c}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
