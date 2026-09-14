"use client";

/**
 * 금일 판매안 — **매입 화면의 «금일 매입안» 과 같은 자리다.**
 *
 * ★ 매입 카드가 보여 주는 것과 같은 것을 보여 준다. 얼마를 팔자고 했는지, 그 근거가
 *   무엇인지, 걸리는 것이 무엇인지다.
 *
 * 🔴 **화면이 숫자를 만들지 않는다.** 매출액은 판매가 적어 보낸 값을 그대로 적는다 —
 *    수량×단가로 다시 만들면 저장된 값과 어긋나는 날 화면만 조용히 맞아 보인다.
 *
 * 🔴 **재무 판정을 화면이 흉내 내지 않는다.** 통과 여부는 재무가 남긴 값이고, 없으면
 *    «검토 전» 이다 — 통과도 거절도 아니다.
 */

import { Panel } from "@/components/console/Blocks";
import { moneyWon, itemText, verdictText, verdictTone } from "../finance/user_text";
import type { SalesProposal } from "./sales_api";

/**
 * 안의 성격. 정본은 판매의 `ScenarioType` 세 값이다.
 *
 * 🔴 **모르는 값을 지어내지 않는다.** 표에 없으면 저장값을 그대로 보여 준다 — 새 유형이
 *    생긴 날 그것이 남의 이름으로 표시되면 안 된다.
 */
const SCENARIO_TYPES: Record<string, string> = {
  CONSERVATIVE: "보수",
  BALANCED: "균형",
  AGGRESSIVE: "공격",
};

/** 안이 무엇을 노리는가. 정본은 판매의 `ScenarioObjective` 세 값이다. */
const OBJECTIVES: Record<string, string> = {
  RISK_DEFENSE: "위험 방어",
  BALANCE: "균형",
  SALES_OPPORTUNITY: "판매 기회",
};

function label(table: Record<string, string>, value: string | null): string | null {
  if (!value) return null;
  return table[value] ?? value;
}

export function TodayProposals({
  rows,
  requestCount,
}: {
  rows: SalesProposal[];
  requestCount: number;
}) {
  //  품목별로 묶는다. 매입 화면이 안을 나란히 놓는 것과 같은 읽기 순서다.
  const items = [...new Set(rows.map((row) => itemText(null, row.item)))];
  return (
    <>
      <div className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(320px,1fr))]">
        {rows.map((row) => (
          <ProposalCard key={`${row.request_id}:${row.scenario_id}`} row={row} />
        ))}
      </div>
      <p className="mb-0 mt-1 text-[12px] text-ink2">
        오늘 판매가 {requestCount}건의 요청을 돌아 {rows.length}개의 안을 만들었습니다
        {items.length > 0 && ` (품목 ${items.join(" · ")})`}. 승인은 이 화면에서 하지 않습니다.
      </p>
    </>
  );
}

function ProposalCard({ row }: { row: SalesProposal }) {
  const tone = verdictTone(row.finance_verdict);
  const accent =
    row.finance_verdict === null
      ? "var(--color-hair)"
      : `var(--color-t-${tone === "neutral" ? "info" : tone})`;
  const kind = label(SCENARIO_TYPES, row.scenario_type);
  const aim = label(OBJECTIVES, row.objective);
  return (
    <article
      className="flex min-w-0 flex-col overflow-hidden rounded-xl border bg-panel"
      style={{
        borderColor: accent,
        boxShadow: row.recommended ? `0 0 0 1px ${accent}` : undefined,
      }}
    >
      <header
        className="flex flex-wrap items-center gap-2 border-b px-4 py-3"
        style={{ borderColor: "var(--color-hair-soft)" }}
      >
        <strong className="text-[14px] font-semibold">{itemText(null, row.item)}</strong>
        {kind && <Tag text={kind} color="var(--color-t-info)" />}
        {row.recommended && <Tag text="추천" color="var(--color-t-good)" />}
        <span className="ml-auto text-[11.5px]" style={{ color: "var(--color-mut)" }}>
          {/* ⚠️ 재무가 아직 안 본 안과 거절된 안은 다른 사실이다. */}
          {row.finance_verdict === null ? "재무 검토 전" : verdictText(row.finance_verdict)}
        </span>
      </header>

      <div className="flex flex-col gap-3.5 p-4">
        <dl className="m-0 flex flex-col gap-1.5 text-[12.5px]">
          <Line
            label="파는 양"
            value={row.quantity_kg === null ? "데이터 없음" : `${Number(row.quantity_kg).toLocaleString("ko-KR")} kg`}
            hero
          />
          {/* 🔴 판매가 적어 보낸 매출액이다. 수량×단가로 다시 만들지 않는다. */}
          <Line label="예상 매출" value={moneyWon(row.reported_sales_amount_krw)} />
          <Line
            label="단가"
            value={
              row.unit_price_krw === null
                ? "데이터 없음"
                : `${Number(row.unit_price_krw).toLocaleString("ko-KR")} 원/kg`
            }
          />
          <Line
            label="납품 · 결제"
            value={`${row.delivery_date ?? "날짜 미정"} · ${
              row.payment_days === null ? "결제일수 미정" : `${row.payment_days}일`
            }`}
          />
          {aim && <Line label="노리는 것" value={aim} />}
        </dl>

        {row.rationale.length > 0 && (
          <Section title="근거">
            {row.rationale.map((text, index) => (
              <li key={index} className="flex gap-2">
                <i aria-hidden style={{ color: "var(--color-mut2)" }}>
                  ·
                </i>
                <span className="min-w-0 flex-1">{text}</span>
              </li>
            ))}
          </Section>
        )}

        {(row.risks.length > 0 || row.uncertainties.length > 0) && (
          <Section title="걸리는 것" tone="var(--color-t-warn)">
            {[...row.risks, ...row.uncertainties].map((text, index) => (
              <li key={index} className="flex gap-2">
                <i aria-hidden style={{ color: "var(--color-t-warn)" }}>
                  ·
                </i>
                <span className="min-w-0 flex-1">{text}</span>
              </li>
            ))}
          </Section>
        )}
      </div>
    </article>
  );
}

function Line({ label: name, value, hero }: { label: string; value: string; hero?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="m-0" style={{ color: "var(--color-mut)" }}>
        {name}
      </dt>
      <dd
        className={`m-0 text-right tabular-nums ${hero ? "text-[15px] font-semibold" : ""}`}
      >
        {value}
      </dd>
    </div>
  );
}

function Section({
  title,
  tone,
  children,
}: {
  title: string;
  tone?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="flex flex-col gap-2">
      <h3
        className="m-0 text-[11.5px] font-semibold"
        style={{ color: tone ?? "var(--color-mut)" }}
      >
        {title}
      </h3>
      <ul className="m-0 flex list-none flex-col gap-1.5 p-0 text-[11.5px] leading-relaxed">
        {children}
      </ul>
    </section>
  );
}

function Tag({ text, color }: { text: string; color: string }) {
  return (
    <span
      className="rounded-full px-2 py-0.5 text-[10.5px] font-semibold"
      style={{ color, border: `1px solid ${color}` }}
    >
      {text}
    </span>
  );
}

/** 판매안 패널. **오류와 빈 것과 성공을 가른다.** */
export function TodayProposalsPanel({
  asOf,
  state,
}: {
  asOf: string;
  state: { data: { request_count: number; rows: SalesProposal[] } | null; error: string | null; loading: boolean };
  }) {
  return (
    <Panel
      title="금일 판매안"
      subtitle={`${asOf} 에 판매가 만든 안입니다 — 화면이 다시 만들지 않습니다`}
    >
      {state.loading ? (
        <p className="m-0 text-[12px] text-ink2">판매안을 읽고 있습니다.</p>
      ) : state.error ? (
        <p className="m-0 whitespace-pre-wrap font-mono text-[11.5px] text-ink2">
          판매안을 읽지 못했습니다 - {state.error}
        </p>
      ) : !state.data ? (
        <p className="m-0 text-[12px] text-ink2">판매안을 읽지 못했습니다.</p>
      ) : state.data.rows.length === 0 ? (
        <p className="m-0 text-[12px] text-ink2">
          {state.data.request_count === 0
            ? "이 날짜에는 판매가 돌지 않았습니다."
            : `판매가 ${state.data.request_count}건의 요청을 돌았지만 안을 만들지 못했습니다.`}
        </p>
      ) : (
        <TodayProposals rows={state.data.rows} requestCount={state.data.request_count} />
      )}
    </Panel>
  );
}
