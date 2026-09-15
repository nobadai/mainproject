"use client";

/**
 * 마스터 대화에서 **판매를 끝까지 진행하는 자리.**
 *
 * ```text
 * 판매 진행 상황 요약 → «판매안을 비교하고 진행하시겠어요?»
 *   [판매안 확인] → 같은 대화 안에 판매안 카드 → [이 판매안 선택]
 *   → «이 조건으로 판매를 확정할까요?» [취소] [판매 확정]
 *   → POST /master/runs/{request_id}/decision → 최종 재검증 → 판매 확정 / 확정 안 됨
 * ```
 *
 * ★ **새 업무 로직이 없다.** 판매안은 판매 화면의 금일 판매안과 같은 API 를 읽고
 *   (`salesOverview.proposals`), 카드는 판매 화면과 같은 카드(`ProposalCard`)이며, 확정은
 *   판매 화면이 쓰는 기존 결정 경로(`approveSalesScenario`)로만 보낸다.
 *
 * 🔴 **추천 · 선택 · 확정을 가른다.** 추천안을 자동으로 고르지 않는다. 선택만으로는
 *    아무것도 쓰지 않는다 — 기록은 «판매 확정» 을 눌렀을 때 한 번만 나간다.
 * 🔴 **다른 안으로 넘어가지 않는다.** 고른 안이 최종 확인에서 막히면 그 사실만 알린다.
 * 🔴 **숫자를 다시 계산하지 않는다.** 수량 · 단가 · 매출 · 이익률 · 여신은 백엔드 값이다.
 */

import { useRef, useState, useSyncExternalStore } from "react";

import { EmptyRows, Failed, Skeleton, useConsoleData } from "@/components/console/ConsoleData";
import { useSimRun } from "@/components/console/RunPicker";
import { sessionSnapshot, serverSnapshot, subscribeSession } from "@/lib/session";

import {
  collectionNeedText,
  itemText,
  moneyWon,
  paymentTermText,
  ratioPercent,
  readableReason,
  salesReviewSentence,
  toNumber,
  verdictText,
} from "@/app/console/finance/user_text";
import { ProposalCard } from "@/app/console/sales/TodayProposals";
import {
  approveSalesScenario,
  salesOverview,
  type SalesDecisionResponse,
  type SalesProposal,
  type SalesProposalsResponse,
} from "@/app/console/sales/sales_api";

const SCENARIO_TYPES: Record<string, string> = {
  CONSERVATIVE: "안정 우선",
  BALANCED: "균형",
  AGGRESSIVE: "판매 기회 우선",
};

const CREDIT_REASON = "SALES_CREDIT_LIMIT_EXCEEDED";

function kindText(value: string | null): string {
  return (value && SCENARIO_TYPES[value]) || "판매안";
}

function keyOf(row: SalesProposal): string {
  return `${row.request_id}:${row.scenario_id}`;
}

type Outcome =
  | { kind: "decision"; row: SalesProposal; decision: SalesDecisionResponse }
  | { kind: "error"; row: SalesProposal; message: string };

export function SalesConversation({ asOf, canApprove }: { asOf: string; canApprove: boolean }) {
  const simRun = useSimRun();
  const session = useSyncExternalStore(subscribeSession, sessionSnapshot, serverSnapshot);
  const [stage, setStage] = useState<"ask" | "declined" | "cards">("ask");
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  //  🔴 **즉시 잠근다.** `submitting` 상태는 다시 그려질 때까지 옛 값이라, 빠른 두 번 클릭이
  //     같은 결정을 두 번 보냈다 (브라우저 검수에서 실제로 2건 나갔다).
  const inFlight = useRef(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  //  확정 뒤 같은 카드가 «미확정» 으로 남지 않게 다시 읽는다.
  const [reload, setReload] = useState(0);
  const state = useConsoleData<SalesProposalsResponse>(
    `conversation-sales:${simRun}:${asOf}:${reload}`,
    () => salesOverview.proposals(simRun, asOf),
    Boolean(simRun),
  );

  if (!simRun) {
    return (
      <p className="m-0 text-sm text-muted">
        어느 시뮬레이션을 볼지 아직 고르지 않았습니다. 화면 위의 실행 선택에서 먼저 골라 주세요.
      </p>
    );
  }
  //  ⚠️ 다시 읽는 동안에도 결과 문장은 남긴다 — 사용자가 방금 무엇이 됐는지 봐야 한다.
  if (state.loading && !outcome) return <Skeleton what="판매 진행 상황" />;
  if (state.error) return <Failed what="판매 진행 상황" message="판매안을 읽지 못했습니다. 잠시 후 다시 시도해 주세요." />;
  const data = state.data;
  const rows = data?.rows ?? [];
  const selected = rows.find((row) => keyOf(row) === selectedKey) ?? null;

  async function confirm(row: SalesProposal) {
    if (inFlight.current || !session || !row.history_run_id) return;
    inFlight.current = true;
    setSubmitting(true);
    try {
      const decision = await approveSalesScenario({
        requestId: row.request_id,
        scenarioId: row.scenario_id,
        historyRunId: row.history_run_id,
        decidedBy: session.name,
      });
      setOutcome({ kind: "decision", row, decision });
    } catch (error) {
      setOutcome({ kind: "error", row, message: error instanceof Error ? error.message : "" });
    } finally {
      inFlight.current = false;
      setSubmitting(false);
      setSelectedKey(null);
      setReload((value) => value + 1);
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <Summary data={data} />

      {rows.length > 0 && stage === "ask" && (
        <div className="flex flex-col gap-2">
          <p className="m-0 text-sm font-semibold">판매안을 비교하고 진행하시겠어요?</p>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => setStage("cards")}
              className="rounded-lg bg-accent px-4 py-1.5 text-[13px] font-semibold text-white"
            >
              판매안 확인
            </button>
            <button
              type="button"
              onClick={() => setStage("declined")}
              className="rounded-lg border border-line px-4 py-1.5 text-[13px] text-muted hover:bg-sunk"
            >
              지금은 안 할게요
            </button>
          </div>
        </div>
      )}

      {stage === "declined" && (
        <div className="flex flex-wrap items-center gap-2 text-[13px] text-muted">
          <span>알겠습니다. 필요할 때 다시 확인할 수 있습니다.</span>
          <button type="button" onClick={() => setStage("cards")} className="rounded-md border border-line px-2.5 py-1 text-[12px] hover:bg-sunk">
            판매안 확인
          </button>
        </div>
      )}

      {stage === "cards" && data && (
        <Cards
          rows={rows}
          selectedKey={selectedKey}
          onSelect={(row) => {
            setOutcome(null);
            setSelectedKey(keyOf(row));
          }}
        />
      )}

      {stage === "cards" && selected && (
        <ConfirmSelection
          row={selected}
          rows={rows}
          sessionName={session?.name ?? null}
          canApprove={canApprove}
          submitting={submitting}
          onCancel={() => setSelectedKey(null)}
          onConfirm={() => void confirm(selected)}
        />
      )}

      {outcome && <OutcomeView outcome={outcome} />}
    </div>
  );
}

/** 판매 진행 상황. **읽은 행을 세기만 한다.** 판정 문장은 백엔드와 같은 규칙이다. */
function Summary({ data }: { data: SalesProposalsResponse | null }) {
  if (!data) return <EmptyRows what="판매안" />;
  if (data.rows.length === 0) {
    return (
      <p className="m-0 text-sm leading-relaxed">
        판매 진행 상황을 확인했습니다.
        <br />
        {data.request_count === 0
          ? "이 날짜에는 판매가 돌지 않았습니다."
          : "팔 수 있는 물량이 없어 오늘은 판매안이 서지 않았습니다."}
      </p>
    );
  }
  const items: { name: string; count: number }[] = [];
  for (const row of data.rows) {
    const name = itemText(null, row.item);
    const entry = items.find((item) => item.name === name);
    if (entry) entry.count += 1;
    else items.push({ name, count: 1 });
  }
  const needCollection = data.rows.filter((row) => (toNumber(row.required_collection_before_sale_krw) ?? 0) > 0).length;
  const confirmed = data.rows.filter((row) => row.sale_status !== null).length;
  return (
    <div className="flex flex-col gap-1 text-sm leading-relaxed">
      <p className="m-0">판매 진행 상황을 확인했습니다.</p>
      {items.map((item) => (
        <p key={item.name} className="m-0">
          {item.name} 판매안 {item.count}개가 준비되어 있습니다.
        </p>
      ))}
      <p className="m-0 text-muted">{salesReviewSentence(data.rows.map((row) => row.finance_verdict))}</p>
      {needCollection > 0 && (
        <p className="m-0 text-muted">{needCollection}개 안은 기존 미수금을 먼저 회수해야 판매할 수 있습니다.</p>
      )}
      {confirmed > 0 && <p className="m-0 text-muted">이미 판매로 확정된 안이 {confirmed}건 있습니다.</p>}
    </div>
  );
}

function Cards({
  rows,
  selectedKey,
  onSelect,
}: {
  rows: SalesProposal[];
  selectedKey: string | null;
  onSelect: (row: SalesProposal) => void;
}) {
  const groups: { item: string; rows: SalesProposal[] }[] = [];
  for (const row of rows) {
    const name = itemText(null, row.item);
    const group = groups.find((entry) => entry.item === name);
    if (group) group.rows.push(row);
    else groups.push({ item: name, rows: [row] });
  }
  return (
    <div className="flex flex-col gap-4">
      <p className="m-0 text-sm">판매안을 제시합니다. 비교한 뒤 진행할 안을 선택해 주세요.</p>
      {groups.map((group) => (
        <section key={group.item} className="flex flex-col gap-2">
          <b className="text-[13.5px]">
            {group.item} <span className="text-[12px] font-normal text-muted">· 판매안 {group.rows.length}개</span>
          </b>
          <div className="grid gap-3 [grid-template-columns:repeat(auto-fit,minmax(min(100%,280px),1fr))]">
            {group.rows.map((row) => (
              <ProposalCard
                key={keyOf(row)}
                row={row}
                selected={selectedKey === keyOf(row)}
                onSelect={() => onSelect(row)}
              />
            ))}
          </div>
        </section>
      ))}
      <p className="m-0 text-[12px] text-muted">
        추천은 시스템이 권한 안이고, 선택은 직접 고르는 안입니다. 추천과 다른 안을 골라도 됩니다. 선택만으로는 판매가 확정되지 않습니다.
      </p>
    </div>
  );
}

/** 선택 뒤 **한 번 더** 묻는다. 기록은 여기서 «판매 확정» 을 눌렀을 때만 나간다. */
function ConfirmSelection({
  row,
  rows,
  sessionName,
  canApprove,
  submitting,
  onCancel,
  onConfirm,
}: {
  row: SalesProposal;
  rows: SalesProposal[];
  sessionName: string | null;
  canApprove: boolean;
  submitting: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const required = toNumber(row.required_collection_before_sale_krw);
  const recommendedElsewhere = !row.recommended && rows.some((other) => other.request_id === row.request_id && other.recommended);
  const blocker =
    row.sale_status !== null
      ? "이미 판매로 확정된 안입니다."
      : !row.history_run_id
        ? "이 판매안은 실행 이력이 없어 현재 화면에서 확정할 수 없습니다. 새 판매안을 생성한 뒤 다시 확인해 주세요."
        : !sessionName
          ? "로그인한 사용자 정보를 확인할 수 없어 판매를 확정할 수 없습니다."
          : !canApprove
            ? "판매 확정은 승인 권한이 있는 사용자만 할 수 있습니다."
            : null;
  const lines: [string, string][] = [
    ["판매 수량", toNumber(row.quantity_kg) === null ? "수량 정보 없음" : `${toNumber(row.quantity_kg)!.toLocaleString("ko-KR")} kg`],
    ["판매 단가", toNumber(row.unit_price_krw) === null ? "단가 정보 없음" : `${toNumber(row.unit_price_krw)!.toLocaleString("ko-KR")}원/kg`],
    ["예상 매출", moneyWon(row.reported_sales_amount_krw)],
    ["예상 이익률", toNumber(row.contribution_margin_rate) === null ? "재무 검토 후 표시" : ratioPercent(row.contribution_margin_rate)],
    ["결제 조건", paymentTermText(row.payment_days)],
    ["재무 검토", row.finance_verdict === null ? "재무 검토 전" : verdictText(row.finance_verdict)],
  ];
  return (
    <section className="rounded-xl border border-accent bg-accent-wash p-4">
      <p className="m-0 text-sm">선택한 판매안입니다.</p>
      <p className="m-0 mt-1 text-[15px] font-semibold">
        {itemText(null, row.item)} · {kindText(row.scenario_type)}
      </p>
      <dl className="m-0 mt-2 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[13px]">
        {lines.map(([name, value]) => (
          <div key={name} className="contents">
            <dt className="text-muted">{name}</dt>
            <dd className="m-0 text-right tabular-nums">{value}</dd>
          </div>
        ))}
      </dl>
      {row.finance_reason_codes.includes(CREDIT_REASON) && required !== null && required > 0 && (
        <p className="m-0 mt-2 text-[12.5px]">현재 조건으로는 바로 판매하기 어렵습니다. {collectionNeedText(required)}</p>
      )}
      {recommendedElsewhere && <p className="m-0 mt-2 text-[12px] text-muted">추천안과 다른 안을 선택했습니다.</p>}
      {blocker ? (
        <p className="m-0 mt-3 text-[13px]">{blocker}</p>
      ) : (
        <p className="m-0 mt-3 text-sm font-semibold">이 조건으로 판매를 확정할까요?</p>
      )}
      <div className="mt-2.5 flex flex-wrap gap-2">
        <button
          type="button"
          onClick={onCancel}
          disabled={submitting}
          className="rounded-lg border border-line bg-surface px-4 py-1.5 text-[13px] disabled:opacity-45"
        >
          취소
        </button>
        {!blocker && (
          <button
            type="button"
            onClick={onConfirm}
            disabled={submitting}
            className="rounded-lg bg-accent px-4 py-1.5 text-[13px] font-semibold text-white disabled:opacity-45"
          >
            {submitting ? "최종 확인 중..." : "판매 확정"}
          </button>
        )}
      </div>
    </section>
  );
}

/** 결과. **성공과 확정 안 됨을 가르고, 다른 안으로 넘어가지 않는다.** */
function OutcomeView({ outcome }: { outcome: Outcome }) {
  const row = outcome.row;
  const confirmed =
    outcome.kind === "decision" &&
    outcome.decision.revalidation_outcome === "PASSED" &&
    outcome.decision.sale?.status === "CONFIRMED";
  if (confirmed) {
    return (
      <section className="rounded-xl border p-4" style={{ borderColor: "var(--color-t-good)" }}>
        <p className="m-0 text-sm font-semibold" style={{ color: "var(--color-t-good)" }}>
          판매가 확정되었습니다.
        </p>
        <p className="m-0 mt-1.5 text-[13px]">
          {itemText(null, row.item)} · {kindText(row.scenario_type)}
        </p>
        <p className="m-0 mt-0.5 text-[13px] tabular-nums text-muted">
          {toNumber(row.quantity_kg) === null ? "수량 정보 없음" : `${toNumber(row.quantity_kg)!.toLocaleString("ko-KR")} kg`} ·{" "}
          {toNumber(row.unit_price_krw) === null ? "단가 정보 없음" : `${toNumber(row.unit_price_krw)!.toLocaleString("ko-KR")}원/kg`} · 판매금액{" "}
          {moneyWon(row.reported_sales_amount_krw)} · {paymentTermText(row.payment_days)}
        </p>
      </section>
    );
  }
  const required = toNumber(row.required_collection_before_sale_krw);
  const creditHint =
    row.finance_reason_codes.includes(CREDIT_REASON) && required !== null && required > 0
      ? `현재 거래처 여신 한도를 초과합니다. ${collectionNeedText(required)}`
      : null;
  let headline = "판매 확정 요청을 완료하지 못했습니다. 판매는 확정되지 않았습니다.";
  let reason: string | null = null;
  if (outcome.kind === "decision") {
    const result = outcome.decision;
    if (result.revalidation_outcome === "FAILED") {
      headline = "최종 확인 과정에서 현재 조건으로는 판매를 확정할 수 없습니다.";
    } else if (result.revalidation_outcome === "CONDITIONAL") {
      headline = "조건이 바뀌어 다시 확인이 필요합니다. 판매는 확정되지 않았습니다.";
    } else if (result.revalidation_outcome === "PASSED") {
      headline = "최종 확인은 통과했지만 판매 등록이 막혔습니다. 판매는 확정되지 않았습니다.";
    } else {
      headline = "최종 확인을 완료하지 못했습니다. 판매는 확정되지 않았습니다.";
    }
    reason = readableReason(result.sale?.reason);
  } else {
    reason = readableReason(outcome.message);
  }
  return (
    <section className="rounded-xl border p-4" style={{ borderColor: "var(--color-t-warn)" }}>
      <p className="m-0 text-sm font-semibold">{headline}</p>
      {creditHint && <p className="m-0 mt-1.5 text-[13px]">{creditHint}</p>}
      {reason && <p className="m-0 mt-1.5 text-[13px] text-muted">{reason}</p>}
      <p className="m-0 mt-1.5 text-[12px] text-muted">다른 판매안은 자동으로 선택하지 않았습니다. 필요하면 직접 다시 골라 주세요.</p>
    </section>
  );
}
