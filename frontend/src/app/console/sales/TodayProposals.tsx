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

import { useState } from "react";

import { Panel } from "@/components/console/Blocks";
import {
  capabilityText,
  itemText,
  moneyWon,
  percentPoint,
  salesReasonText,
  toNumber,
  verdictText,
  verdictTone,
} from "../finance/user_text";
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
  hiddenZeroQuantity,
}: {
  rows: SalesProposal[];
  requestCount: number;
  hiddenZeroQuantity: number;
}) {
  const [selectedScenarioKey, setSelectedScenarioKey] = useState<string | null>(null);
  //  품목별로 묶는다. 매입 화면이 안을 나란히 놓는 것과 같은 읽기 순서다.
  const items = [...new Set(rows.map((row) => itemText(null, row.item)))];
  return (
    <>
      <div className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(340px,1fr))]">
        {rows.map((row) => {
          const key = `${row.request_id}:${row.scenario_id}`;
          return (
          <ProposalCard
            key={key}
            row={row}
            selected={selectedScenarioKey === key}
            onSelect={() => setSelectedScenarioKey(key)}
          />
          );
        })}
        ))}
      </div>
      <p className="mb-0 mt-1 text-[12px] leading-relaxed text-ink2">
        오늘 판매가 {requestCount}건의 요청을 돌아 {rows.length}개의 안을 만들었습니다
        {items.length > 0 && ` (품목 ${items.join(" · ")})`}. 추천과 선택은 다르며, 선택은 아직 판매 확정이 아닙니다.
        {/* 🔴 지운 것이 아니라 뺀 것이다. 몇 건인지 숫자로 남긴다. */}
        {hiddenZeroQuantity > 0 && (
          <>
            {" "}
            팔 물량이 없어 안이 서지 않은 {hiddenZeroQuantity}건은 목록에서 뺐습니다.
          </>
        )}
      </p>
    </>
  );
}

function ProposalCard({
  row,
  selected,
  onSelect,
}: {
  row: SalesProposal;
  selected: boolean;
  onSelect: () => void;
}) {
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
        {selected && <Tag text="선택됨" color="var(--color-t-info)" />}
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

        {/* 🔴 **왜 그 판정인지 말한다.** 결과만 적으면 사용자가 되짚을 수 없다. */}
        <FinanceReason row={row} />

        <button
          type="button"
          onClick={onSelect}
          className="rounded-lg border px-3 py-2 text-[12px] font-semibold"
          style={{ borderColor: "var(--color-t-info)", color: "var(--color-t-info)" }}
        >
          {selected ? "선택한 판매안" : "이 판매안 선택"}
        </button>

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

        {/* ★ 근거는 펼쳐서 본다. 어떤 자료가 들어와 이 안이 섰는지가 여기 있다. */}
        <Evidence row={row} />
      </div>
    </article>
  );
}

/**
 * 판정을 가른 사유.
 *
 * 🔴 **판정이 없으면 왜 없는지 말한다.** «재무 검토 전» 만 적으면 밀린 것인지 부를
 *    일이 없었던 것인지 알 수 없다 — 판매가 적어 둔 미완 검증을 그대로 보여 준다.
 */
function FinanceReason({ row }: { row: SalesProposal }) {
  const rate = toNumber(row.contribution_margin_rate);
  if (row.finance_verdict === null) {
    return (
      <Section title="재무 검토" tone="var(--color-mut)">
        <li className="flex gap-2">
          <i aria-hidden style={{ color: "var(--color-mut2)" }}>
            ·
          </i>
          <span className="min-w-0 flex-1">
            {row.missing_capabilities.length > 0
              ? `${row.missing_capabilities.map(capabilityText).join(" · ")}가 아직 호출되지 않았습니다.`
              : "이 안에 대한 재무 판정 기록이 없습니다."}
          </span>
        </li>
      </Section>
    );
  }
  const tone = verdictTone(row.finance_verdict);
  return (
    <Section
      title={`재무 검토 · ${verdictText(row.finance_verdict)}`}
      tone={tone === "neutral" ? "var(--color-mut)" : `var(--color-t-${tone})`}
    >
      {row.finance_reason_codes.length === 0 ? (
        <li className="flex gap-2">
          <i aria-hidden style={{ color: "var(--color-t-good)" }}>
            ·
          </i>
          <span className="min-w-0 flex-1">모든 재무 규칙을 통과했습니다.</span>
        </li>
      ) : (
        row.finance_reason_codes.map((code) => (
          <li key={code} className="flex gap-2">
            <i aria-hidden style={{ color: `var(--color-t-${tone === "neutral" ? "info" : tone})` }}>
              ·
            </i>
            <span className="min-w-0 flex-1">{salesReasonText(code)}</span>
          </li>
        ))
      )}
      {/* 판정을 뒷받침한 숫자. 없으면 적지 않는다 — 0 으로 채우지 않는다. */}
      {rate !== null && (
        <li className="flex gap-2 text-ink2">
          <i aria-hidden>·</i>
          <span className="min-w-0 flex-1">
            기여이익 {moneyWon(row.contribution_margin_krw)} · 이익률{" "}
            {percentPoint(rate * 100)}
          </span>
        </li>
      )}
      {toNumber(row.projected_partner_ar_krw) !== null && (
        <li className="flex gap-2 text-ink2">
          <i aria-hidden>·</i>
          <span className="min-w-0 flex-1">
            성사 후 거래처 미수 {moneyWon(row.projected_partner_ar_krw)} · 여신한도{" "}
            {moneyWon(row.credit_limit_krw)}
          </span>
        </li>
      )}
    </Section>
  );
}

/**
 * 이 안이 무엇에 기대어 섰는가. **접어 두되 지우지 않는다.**
 *
 * ⚠️ 참조 문자열은 내부 키라 기본 화면에 펼쳐 두면 카드가 개발 로그가 된다. 사람이
 *   읽을 요약을 먼저 적고, 원본 참조는 열어야 보이게 한다.
 */
function Evidence({ row }: { row: SalesProposal }) {
  const confirmed = toNumber(row.confirmed_quantity_kg);
  const conditional = toNumber(row.conditional_quantity_kg);
  const facts: string[] = [];
  if (confirmed !== null) {
    facts.push(`확보된 물량 ${confirmed.toLocaleString("ko-KR")} kg`);
  }
  if (conditional !== null && conditional > 0) {
    facts.push(`조건부 물량 ${conditional.toLocaleString("ko-KR")} kg`);
  }
  if (row.additional_supply_required === true) facts.push("추가 조달이 필요한 안입니다");
  if (toNumber(row.cost_basis_amount_krw) !== null) {
    facts.push(
      `재고원가 ${moneyWon(row.cost_basis_amount_krw)}` +
        (row.cost_basis_method ? ` (${row.cost_basis_method === "ACTUAL" ? "실제 취득원가" : row.cost_basis_method})` : ""),
    );
  }
  if (row.ml_support_used === true) facts.push("가격 예측을 참고했습니다");
  if (row.ml_support_used === false) facts.push("가격 예측을 쓰지 않았습니다");
  row.rationale.forEach((text) => facts.push(text));

  const refs = [...row.cost_basis_refs, ...row.evidence_refs];
  if (facts.length === 0 && refs.length === 0) return null;

  return (
    <details className="text-[11.5px]">
      <summary className="cursor-pointer list-none" style={{ color: "var(--color-mut)" }}>
        ▸ 근거 {facts.length + refs.length}건
      </summary>
      <ul className="m-0 mt-2 flex list-none flex-col gap-1.5 p-0 leading-relaxed">
        {facts.map((text, index) => (
          <li key={index} className="flex gap-2">
            <i aria-hidden style={{ color: "var(--color-mut2)" }}>
              ·
            </i>
            <span className="min-w-0 flex-1">{text}</span>
          </li>
        ))}
      </ul>
      {refs.length > 0 && (
        <>
          <p className="mb-1 mt-2" style={{ color: "var(--color-mut)" }}>
            참조한 자료 {refs.length}건
          </p>
          <p className="m-0 break-all font-mono text-[10.5px] text-ink2">{refs.join(", ")}</p>
        </>
      )}
      {row.source_ref && (
        <p className="mb-0 mt-2 break-all font-mono text-[10.5px] text-ink2">
          출처 {row.source_ref}
        </p>
      )}
    </details>
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
  state: {
    data: {
      request_count: number;
      hidden_zero_quantity: number;
      rows: SalesProposal[];
    } | null;
    error: string | null;
    loading: boolean;
  };
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
            : state.data.hidden_zero_quantity > 0
              ? `판매가 ${state.data.request_count}건의 요청을 돌았지만, 팔 물량이 없어 ${state.data.hidden_zero_quantity}개의 안이 모두 서지 못했습니다.`
              : `판매가 ${state.data.request_count}건의 요청을 돌았지만 안을 만들지 못했습니다.`}
        </p>
      ) : (
        <TodayProposals
          rows={state.data.rows}
          requestCount={state.data.request_count}
          hiddenZeroQuantity={state.data.hidden_zero_quantity}
        />
      )}
    </Panel>
  );
}
