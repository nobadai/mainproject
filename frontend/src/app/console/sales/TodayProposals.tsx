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

import { useRef, useState, useSyncExternalStore } from "react";

import { Panel } from "@/components/console/Blocks";
import {
  capabilityText,
  collectionNeedText,
  itemText,
  longDate,
  moneyWon,
  paymentTermText,
  percentPoint,
  salesReasonText,
  toNumber,
  verdictText,
  verdictTone,
} from "../finance/user_text";
import type { SalesProposal } from "./sales_api";
import { approveSalesScenario, type SalesDecisionResponse } from "./sales_api";
import {
  presentationColor,
  presentationHelp,
  presentationText,
  screenStateText,
  strategyLines,
  unresolvedReasonText,
} from "./proposal_text";
import { sessionSnapshot, serverSnapshot, subscribeSession } from "@/lib/session";

/**
 * 안의 성격. 정본은 판매의 `ScenarioType` 세 값이다.
 *
 * 🔴 **모르는 값을 지어내지 않는다.** 표에 없으면 저장값을 그대로 보여 준다 — 새 유형이
 *    생긴 날 그것이 남의 이름으로 표시되면 안 된다.
 */
const SCENARIO_TYPES: Record<string, string> = {
  CONSERVATIVE: "안정 우선",
  BALANCED: "균형",
  AGGRESSIVE: "판매 기회 우선",
};

/** 안이 무엇을 노리는가. 정본은 판매의 `ScenarioObjective` 세 값이다. */
const OBJECTIVES: Record<string, string> = {
  RISK_DEFENSE: "위험 방어",
  BALANCE: "균형",
  SALES_OPPORTUNITY: "판매 기회",
};

/**
 * 걸리는 점. 🔴 **코드 모양의 문장(`PRICE_CONTEXT_REQUIRED` 같은)은 원문으로 적지 않는다** —
 * 사용자가 읽을 수 있는 문장만 남기고, 코드는 «세부 조건» 한 줄로 알린다.
 */
function concerns(row: SalesProposal): string[] {
  const all = [...row.risks, ...row.uncertainties];
  const readable = [...new Set(all.filter((text) => !/^[A-Z0-9_:.-]+$/.test(text.trim())))];
  return readable.length < all.length ? [...readable, "세부 조건을 확인해 주세요."] : readable;
}

function label(table: Record<string, string>, value: string | null): string | null {
  if (!value) return null;
  return table[value] ?? "판매안";
}

export function TodayProposals({
  rows,
  requestCount,
  hiddenZeroQuantity,
  screenState,
  unresolvedCount,
  rejectedCount,
  onConfirmed,
}: {
  rows: SalesProposal[];
  requestCount: number;
  hiddenZeroQuantity: number;
  /** 🔴 «후보가 없다» · «판정이 없다» · «다 탈락» 을 한 문구로 합치지 않는다. */
  screenState?: string;
  unresolvedCount?: number;
  rejectedCount?: number;
  onConfirmed?: () => void;
}) {
  const [selectedScenarioKey, setSelectedScenarioKey] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  //  🔴 즉시 잠근다 — 상태만으로는 빠른 두 번 클릭이 같은 결정을 두 번 보낸다.
  const inFlight = useRef(false);
  const [decision, setDecision] = useState<SalesDecisionResponse | null>(null);
  const [decisionError, setDecisionError] = useState<string | null>(null);
  const session = useSyncExternalStore(subscribeSession, sessionSnapshot, serverSnapshot);
  const selected = rows.find((row) => `${row.request_id}:${row.scenario_id}` === selectedScenarioKey) ?? null;
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
              onSelect={() => {
                setSelectedScenarioKey(key);
                setDecision(null);
                setDecisionError(null);
                setConfirming(false);
                setSubmitting(false);
              }}
            />
          );
        })}
      </div>
      <ApprovalPanel
        selected={selected}
        sessionName={session?.name ?? null}
        confirming={confirming}
        submitting={submitting}
        result={decision}
        error={decisionError}
        onStart={() => {
          setDecision(null);
          setDecisionError(null);
          setConfirming(true);
        }}
        onCancel={() => setConfirming(false)}
        onConfirm={async () => {
          //  🔴 **판정을 받고 통과한 안만 확정으로 간다.** 미판정 후보를 화면에
          //     보여주는 것과 확정 요청을 보내는 것은 다른 사실이다.
          if (!selected || selected.approval_blocked || selected.sale_status !== null || !selected.history_run_id || !session || inFlight.current) return;
          inFlight.current = true;
          setSubmitting(true);
          setDecisionError(null);
          try {
            setDecision(
              await approveSalesScenario({
                requestId: selected.request_id,
                scenarioId: selected.scenario_id,
                historyRunId: selected.history_run_id,
                decidedBy: session.name,
              }),
            );
            setConfirming(false);
            onConfirmed?.();
          } catch (error) {
            setDecisionError(error instanceof Error ? error.message : "판매 확정 요청을 완료하지 못했습니다.");
          } finally {
            inFlight.current = false;
            setSubmitting(false);
          }
        }}
      />
      {screenState && screenStateText(screenState) && (
        <p
          className="mb-0 mt-3 rounded-lg px-3 py-2 text-[16px] leading-relaxed"
          style={{ background: "var(--color-desk)", color: presentationColor(screenState === "PRESENTABLE" ? "PRESENTABLE" : screenState === "REJECTED" ? "REJECTED" : "UNRESOLVED") }}
        >
          {screenStateText(screenState)}
          {(unresolvedCount ?? 0) > 0 && ` 판정을 기다리는 안 ${unresolvedCount}개`}
          {(rejectedCount ?? 0) > 0 && `, 확정할 수 없는 안 ${rejectedCount}개`}
          {((unresolvedCount ?? 0) > 0 || (rejectedCount ?? 0) > 0) && "."}
        </p>
      )}
      <p className="mb-0 mt-1 text-[16px] leading-relaxed text-ink2">
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

function ApprovalPanel({
  selected,
  sessionName,
  confirming,
  submitting,
  result,
  error,
  onStart,
  onCancel,
  onConfirm,
}: {
  selected: SalesProposal | null;
  sessionName: string | null;
  confirming: boolean;
  submitting: boolean;
  result: SalesDecisionResponse | null;
  error: string | null;
  onStart: () => void;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const canApprove =
    selected !== null
    && !selected.approval_blocked
    && selected.sale_status === null
    && selected.history_run_id !== null
    && sessionName !== null;
  if (result) return <DecisionResult result={result} />;
  return (
    <section className="mt-4 rounded-xl border p-4" style={{ borderColor: "var(--color-hair)" }}>
      <h3 className="m-0 text-[18px] font-semibold">선택한 안으로 판매 확정</h3>
      {!selected ? (
        <p className="mb-0 mt-2 text-[16px] text-ink2">판매안을 하나 선택하면 최종 확인을 진행할 수 있습니다.</p>
      ) : (
        <div className="mt-2 text-[16px] leading-relaxed text-ink2">
          <p className="m-0"><b className="text-ink">{label(SCENARIO_TYPES, selected.scenario_type)}</b> · {selected.quantity_kg === null ? "수량 정보 없음" : `${Number(selected.quantity_kg).toLocaleString("ko-KR")} kg`} · {moneyWon(selected.reported_sales_amount_krw)}</p>
          {selected.sale_status !== null && <p className="mb-0 mt-1 font-semibold text-[var(--color-t-good)]">판매 확정 완료</p>}
          {/* ★ 왜 못 누르는지 말하지 않으면 사용자는 버튼이 고장 난 줄 안다. */}
          {selected.approval_blocked && selected.sale_status === null && (
            <p className="mb-0 mt-1" style={{ color: presentationColor(selected.presentation_state) }}>
              <b>{presentationText(selected.presentation_state)}</b> — {presentationHelp(selected.presentation_state)}
            </p>
          )}
          {!selected.history_run_id && <p className="mb-0 mt-1">이 실행 기록을 확인할 수 없어 판매 확정을 진행할 수 없습니다.</p>}
          {!sessionName && <p className="mb-0 mt-1">승인자 정보를 확인할 수 없어 판매 확정을 진행할 수 없습니다.</p>}
        </div>
      )}
      {confirming && selected ? (
        <div className="mt-3 rounded-lg bg-[var(--color-desk)] p-3 text-[16px]">
          <p className="m-0 font-semibold">판매 확정 전 마지막 확인</p>
          <p className="mb-0 mt-1 text-ink2">서버가 이 안만 다시 확인합니다. 통과하지 못하면 다른 안을 자동으로 선택하지 않습니다.</p>
          <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-ink2">
            <dt>거래처</dt><dd className="m-0 text-ink">{selected.partner_id ?? "정보 없음"}</dd>
            <dt>품목</dt><dd className="m-0 text-ink">{itemText(null, selected.item)}</dd>
            <dt>판매 수량</dt><dd className="m-0 text-ink">{selected.quantity_kg === null ? "정보 없음" : `${Number(selected.quantity_kg).toLocaleString("ko-KR")} kg`}</dd>
            <dt>단가</dt><dd className="m-0 text-ink">{selected.unit_price_krw === null ? "정보 없음" : `${Number(selected.unit_price_krw).toLocaleString("ko-KR")} 원/kg`}</dd>
            <dt>예상 매출</dt><dd className="m-0 text-ink">{moneyWon(selected.reported_sales_amount_krw)}</dd>
            <dt>납품일</dt><dd className="m-0 text-ink">{selected.delivery_date ?? "날짜 미정"}</dd>
            <dt>결제 조건</dt><dd className="m-0 text-ink">{paymentTermText(selected.payment_days)}</dd>
            {/* ★ 세 축을 각자 적는다 — 한 칸에 섞으면 어느 축의 이야기인지 못 되짚는다. */}
            <dt>제시 상태</dt><dd className="m-0 text-ink">{presentationText(selected.presentation_state)}</dd>
            <dt>후보 상태</dt><dd className="m-0 text-ink">{selected.status ?? "정보 없음"}</dd>
            <dt>재무 판정</dt><dd className="m-0 text-ink">{selected.finance_verdict === null ? "재무 검토 전" : verdictText(selected.finance_verdict)}</dd>
          </dl>
          <div className="mt-3 flex flex-wrap gap-2">
            <button type="button" onClick={onCancel} disabled={submitting} className="rounded-lg border px-3 py-2 font-semibold">취소</button>
            <button type="button" onClick={onConfirm} disabled={submitting} className="rounded-lg border px-3 py-2 font-semibold" style={{ borderColor: "var(--color-t-good)", color: "var(--color-t-good)" }}>
              {submitting ? "최종 확인 중..." : "이 조건으로 판매 확정"}
            </button>
          </div>
        </div>
      ) : (
        <button type="button" onClick={onStart} disabled={!canApprove} className="mt-3 rounded-lg border px-3 py-2 text-[16px] font-semibold disabled:cursor-not-allowed disabled:opacity-50" style={{ borderColor: "var(--color-t-good)", color: "var(--color-t-good)" }}>
          선택한 안으로 판매 확정
        </button>
      )}
      {error && <p className="mb-0 mt-3 text-[16px] text-[var(--color-t-bad)]">판매 확정을 완료하지 못했습니다. {error}</p>}
    </section>
  );
}

function DecisionResult({ result }: { result: SalesDecisionResponse }) {
  if (result.revalidation_outcome === "PASSED" && result.sale?.status === "CONFIRMED") {
    return <p className="mt-4 rounded-xl border p-4 text-[16px] font-semibold" style={{ borderColor: "var(--color-t-good)", color: "var(--color-t-good)" }}>최종 확인을 마쳐 판매가 확정되었습니다.</p>;
  }
  const text: Record<string, string> = {
    CONDITIONAL: "조건이 변경되어 다시 확인이 필요합니다.",
    FAILED: "현재 조건으로는 판매를 확정할 수 없습니다.",
    ERROR: "최종 확인을 완료하지 못했습니다.",
  };
  const detail = result.sale?.status === "BLOCKED" ? result.sale.reason ?? "판매 확정이 차단되었습니다." : null;
  return <p className="mt-4 rounded-xl border p-4 text-[16px]" style={{ borderColor: "var(--color-t-warn)" }}>{text[result.revalidation_outcome ?? ""] ?? detail ?? "최종 확인 결과를 확인해 주세요."}{detail && ` ${detail}`}</p>;
}

/**
 * 판정이 왜 안 났는가.
 *
 * ★ **판매가 적어 둔 사실에서만 읽는다.** 없는 이유를 지어내면 사용자가 채울 수 없는
 *   것을 채우려 한다.
 */
function UnresolvedReason({ row }: { row: SalesProposal }) {
  if (row.presentation_state !== "UNRESOLVED") return null;
  if (row.unresolved_reason_codes.length === 0) return null;
  return (
    <section className="rounded-lg px-3 py-2 text-[16px]" style={{ background: "var(--color-desk)" }}>
      <p className="m-0 font-semibold">아직 판정을 받지 못했습니다</p>
      <ul className="m-0 mt-1 list-disc pl-4 text-ink2">
        {row.unresolved_reason_codes.map((code) => (
          <li key={code}>{unresolvedReasonText(code)}</li>
        ))}
      </ul>
      <p className="mb-0 mt-1 text-ink2">탈락한 것이 아닙니다 — 같은 조건으로 다시 확인할 수 있습니다.</p>
    </section>
  );
}

/**
 * 전략이 어떻게 섰는가.
 *
 * 🔴 **저장된 라벨만 적는다.** HTTP 원문이나 provider 응답 본문은 계약에 없다.
 */
function StrategyNote({ row }: { row: SalesProposal }) {
  const lines = strategyLines(row.strategy);
  if (lines.length === 0) return null;
  return (
    <details className="text-[16px]">
      <summary className="cursor-pointer text-ink2">이 안이 어떻게 만들어졌는지 보기</summary>
      <ul className="m-0 mt-1.5 list-disc pl-4 text-ink2">
        {lines.map((line) => (
          <li key={line}>{line}</li>
        ))}
      </ul>
    </details>
  );
}

/**
 * 판매안 카드 한 장. **판매 화면과 마스터 대화가 같은 카드를 쓴다** — 두 자리가 다른
 * 숫자나 다른 추천을 말하지 않게 한 벌로 둔다.
 */
export function ProposalCard({
  row,
  selected,
  onSelect,
}: {
  row: SalesProposal;
  selected: boolean;
  onSelect: () => void;
}) {
  //  ★ 테두리는 **제시 상태**를 따른다 — 사용자가 먼저 알아야 하는 것은 «이 안으로 갈
  //    수 있나» 이고, 재무 판정은 그 답의 근거다.
  const accent = presentationColor(row.presentation_state);
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
        <strong className="text-[18px] font-semibold">{itemText(null, row.item)}</strong>
        {kind && <Tag text={kind} color="var(--color-t-info)" />}
        {row.recommended && <Tag text="추천" color="var(--color-t-good)" />}
        {selected && <Tag text="선택됨" color="var(--color-t-info)" />}
        {/* ★ 확정은 추천·선택과 다른 사실이다 — 같은 실행에 판매 기록이 있을 때만 붙는다. */}
        {row.sale_status !== null && <Tag text="판매 확정" color="var(--color-t-good)" />}
        {/* 🔴 **제시 상태와 재무 판정을 한 배지에 섞지 않는다.** 앞은 «이 안으로 갈 수
            있나» 이고 뒤는 «재무가 뭐라 했나» 다 — 미판정을 탈락으로 읽게 두지 않는다. */}
        <Tag text={presentationText(row.presentation_state)} color={accent} />
        <span className="ml-auto text-[15.5px]" style={{ color: "var(--color-mut)" }}>
          {/* ⚠️ 재무가 아직 안 본 안과 거절된 안은 다른 사실이다. */}
          재무 {row.finance_verdict === null ? "검토 전" : verdictText(row.finance_verdict)}
        </span>
      </header>

      <div className="flex flex-col gap-3.5 p-4">
        <dl className="m-0 flex flex-col gap-1.5 text-[16.5px]">
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
            value={`${row.delivery_date ?? "날짜 미정"} · ${paymentTermText(row.payment_days)}`}
          />
          {aim && <Line label="노리는 것" value={aim} />}
        </dl>

        {/* 🔴 **왜 그 판정인지 말한다.** 결과만 적으면 사용자가 되짚을 수 없다. */}
        <FinanceReason row={row} />

        {/* 🔴 **판정이 안 났으면 무엇을 기다리는지 말한다.** 말하지 않으면 사용자가
            할 수 있는 일이 없고, 그러면 조건을 바꾸는 엉뚱한 일을 한다. */}
        <UnresolvedReason row={row} />

        {/* ★ 이 안의 자세를 무엇이 골랐나. 모델이 실패했으면 왜 실패했는지까지. */}
        <StrategyNote row={row} />

        <button
          type="button"
          onClick={onSelect}
          disabled={row.sale_status !== null}
          className="rounded-lg border px-3 py-2 text-[16px] font-semibold disabled:cursor-not-allowed disabled:opacity-55"
          style={{ borderColor: "var(--color-t-info)", color: "var(--color-t-info)" }}
        >
          {row.sale_status !== null ? "이미 확정된 판매" : selected ? "선택한 판매안" : "이 판매안 선택"}
        </button>

        {concerns(row).length > 0 && (
          <Section title="걸리는 것" tone="var(--color-t-warn)">
            {concerns(row).map((text, index) => (
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
      <CreditFacts row={row} />
    </Section>
  );
}

/**
 * 이 판매가 거래처 여신에 주는 영향.
 *
 * 🔴 **화면이 여신을 세지 않는다.** 한도·미수·남은 여신·판매 후 미수·선회수 필요액은 전부
 *    재무가 이 안을 검토하며 센 값이다. 없으면 적지 않는다 — 0 으로 채우지 않는다.
 *
 * 🔴 **선회수 필요액은 안내다.** 이 화면이 미수를 줄이거나 가격·수량을 바꾸지 않는다.
 */
function CreditFacts({ row }: { row: SalesProposal }) {
  if (toNumber(row.credit_limit_krw) === null && toNumber(row.current_partner_ar_krw) === null) {
    return null;
  }
  const required = toNumber(row.required_collection_before_sale_krw);
  const lines: [string, string][] = [
    ["여신 한도", moneyWon(row.credit_limit_krw)],
    ["현재 미수금", moneyWon(row.current_partner_ar_krw)],
    ["남은 여신", moneyWon(row.available_credit_krw)],
    ["이번 판매", moneyWon(row.reported_sales_amount_krw)],
    ["판매 후 예상 미수금", moneyWon(row.projected_partner_ar_krw)],
  ];
  return (
    <li className="mt-1 flex flex-col gap-1.5 rounded-lg border px-3 py-2" style={{ borderColor: "var(--color-hair)" }}>
      <b className="text-[16px]">거래처 여신</b>
      <dl className="m-0 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-[16px]">
        {lines.map(([name, value]) => (
          <div key={name} className="contents">
            <dt className="text-ink2">{name}</dt>
            <dd className="m-0 text-right tabular-nums">{value}</dd>
          </div>
        ))}
      </dl>
      <span
        className="text-[16px]"
        style={{ color: required !== null && required > 0 ? "var(--color-t-warn)" : "var(--color-t-good)" }}
      >
        {required !== null && required > 0 ? "현재 조건으로는 바로 판매하기 어렵습니다. " : ""}
        {collectionNeedText(row.required_collection_before_sale_krw)}
      </span>
      {required !== null && required > 0 && (
        <span className="text-[15.5px] text-ink2">
          {row.expected_credit_recovery_date
            ? `예상 여신 회복일 ${longDate(row.expected_credit_recovery_date)} — 계약상 결제 예정일을 기준으로 한 예상입니다.`
            : //  ⚠️ «모이지 않는다» 로 단정하지 않는다. 이 칸이 생기기 전에 저장된 판정에는
              //    회복일 자체가 없고, 그 경우와 «예정 채권으로 못 채움» 을 화면이 가를 수 없다.
              "예상 여신 회복일을 알려 줄 수금 예정 정보가 없습니다."}
        </span>
      )}
    </li>
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
  const costAmount = toNumber(row.cost_basis_amount_krw);
  const costQuantity = toNumber(row.cost_basis_quantity_kg);
  const facts: { received: string; meaning: string }[] = [];
  if (confirmed !== null) {
    facts.push({
      received: `확보된 판매 가능 물량 ${confirmed.toLocaleString("ko-KR")} kg`,
      meaning: "현재 확보가 확인된 물량 범위에서 판매안을 만들었습니다.",
    });
  }
  if (conditional !== null && conditional > 0) {
    facts.push({
      received: `조건부 추가 물량 ${conditional.toLocaleString("ko-KR")} kg`,
      meaning: "추가 조달 조건이 충족될 때만 함께 사용할 수 있는 물량입니다.",
    });
  }
  if (row.additional_supply_required === true) {
    facts.push({
      received: "추가 조달 필요 여부: 필요",
      meaning: "현재 확보 물량만으로는 이 판매안을 이행할 수 없습니다.",
    });
  }
  if (costAmount !== null) {
    facts.push({
      received: `재고 원가 ${moneyWon(row.cost_basis_amount_krw)}${
        costQuantity !== null ? ` · 기준 물량 ${costQuantity.toLocaleString("ko-KR")} kg` : ""
      }${row.cost_basis_method === "ACTUAL" ? " · 실제 취득원가" : ""}`,
      meaning: "예상 이익과 재무 검토의 원가 기준으로 사용했습니다.",
    });
  }
  if (row.ml_support_used === true) {
    facts.push({
      received: "시장 가격 예측",
      meaning: "판매 단가를 검토할 때 최신 가격 전망을 함께 참고했습니다.",
    });
  }
  if (row.ml_support_used === false) {
    facts.push({
      received: "시장 가격 예측",
      meaning: "이 판매안에는 시장 가격 예측을 사용하지 않았습니다.",
    });
  }
  const refs = [...row.cost_basis_refs, ...row.evidence_refs];
  if (facts.length === 0 && refs.length === 0) return null;

  return (
    <details className="text-[15.5px]">
      <summary className="cursor-pointer list-none" style={{ color: "var(--color-mut)" }}>
        ▸ 근거 {facts.length + refs.length}건
      </summary>
      <ul className="m-0 mt-2 flex list-none flex-col gap-1.5 p-0 leading-relaxed">
        {facts.map((fact, index) => (
          <li key={index} className="flex gap-2">
            <i aria-hidden style={{ color: "var(--color-mut2)" }}>
              ·
            </i>
            <span className="min-w-0 flex-1">
              <b className="font-semibold text-ink">받은 자료: {fact.received}</b>
              <span className="mt-0.5 block text-ink2">어떻게 반영했나: {fact.meaning}</span>
            </span>
          </li>
        ))}
      </ul>
      {refs.length > 0 && (
        <p className="mb-0 mt-2 text-ink2">
          그 밖에 원가·재고·검증 자료 {refs.length}건을 함께 확인했습니다. 내부 연결번호는 표시하지 않습니다.
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
        className={`m-0 text-right tabular-nums ${hero ? "text-[19px] font-semibold" : ""}`}
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
        className="m-0 text-[15.5px] font-semibold"
        style={{ color: tone ?? "var(--color-mut)" }}
      >
        {title}
      </h3>
      <ul className="m-0 flex list-none flex-col gap-1.5 p-0 text-[15.5px] leading-relaxed">
        {children}
      </ul>
    </section>
  );
}

function Tag({ text, color }: { text: string; color: string }) {
  return (
    <span
      className="rounded-full px-2 py-0.5 text-[14.5px] font-semibold"
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
  onConfirmed,
}: {
  asOf: string;
  state: {
    data: {
      request_count: number;
      hidden_zero_quantity: number;
      state?: string;
      unresolved_count?: number;
      rejected_count?: number;
      rows: SalesProposal[];
    } | null;
    error: string | null;
    loading: boolean;
  };
  onConfirmed?: () => void;
}) {
  return (
    <Panel
      title="금일 판매안"
      subtitle={`${asOf} 에 만든 판매안입니다. 금액과 판단은 저장된 결과 그대로 표시합니다.`}
    >
      {state.loading ? (
        <p className="m-0 text-[16px] text-ink2">판매안을 읽고 있습니다.</p>
      ) : state.error ? (
          <p className="m-0 whitespace-pre-wrap text-[15.5px] text-ink2">
          판매안을 읽지 못했습니다 - {state.error}
        </p>
      ) : !state.data ? (
        <p className="m-0 text-[16px] text-ink2">판매안을 읽지 못했습니다.</p>
      ) : state.data.rows.length === 0 ? (
        <p className="m-0 text-[16px] text-ink2">
          {state.data.request_count === 0
            ? "이 날짜에는 판매가 돌지 않았습니다."
            : state.data.hidden_zero_quantity > 0
              ? `판매가 ${state.data.request_count}건의 요청을 돌았지만, 팔 물량이 없어 ${state.data.hidden_zero_quantity}개의 안이 모두 서지 못했습니다.`
              : `판매가 ${state.data.request_count}건의 요청을 돌았지만 안을 만들지 못했습니다.`}
        </p>
      ) : (
        <TodayProposals
          screenState={state.data.state}
          unresolvedCount={state.data.unresolved_count}
          rejectedCount={state.data.rejected_count}
          rows={state.data.rows}
          requestCount={state.data.request_count}
          hiddenZeroQuantity={state.data.hidden_zero_quantity}
          onConfirmed={onConfirmed}
        />
      )}
    </Panel>
  );
}
