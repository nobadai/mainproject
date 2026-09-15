"use client";

import { useState, useSyncExternalStore } from "react";

import { Panel } from "@/components/console/Blocks";
import { Failed, Skeleton, useConsoleData } from "@/components/console/ConsoleData";
import { salesConsole } from "@/lib/console_api";
import { sessionSnapshot, serverSnapshot, subscribeSession } from "@/lib/session";

import {
  fetchCreditLimitHistory,
  registerCreditLimit,
  type CreditLimitHistoryItem,
} from "./credit_api";
import { creditGradeText, moneyWon } from "./user_text";

type Grade = "OFFICIAL" | "VENDOR" | "SIM_FIXED";
type HistoryState = {
  data: CreditLimitHistoryItem[] | null;
  error: string | null;
  loading: boolean;
};

export function CreditLimitForm({
  simRun,
  asOf,
  refreshKey,
  onSaved,
}: {
  simRun: string;
  asOf: string;
  refreshKey: number;
  onSaved: () => void;
}) {
  const session = useSyncExternalStore(subscribeSession, sessionSnapshot, serverSnapshot);
  const [partner, setPartner] = useState("");
  const [amount, setAmount] = useState("");
  const [date, setDate] = useState(asOf);
  const [grade, setGrade] = useState<Grade>("VENDOR");
  const [sourceRef, setSourceRef] = useState("");
  const [note, setNote] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const partners = useConsoleData(
    `credit-partners:${simRun}:${asOf}`,
    () => salesConsole.activeCustomers(simRun, asOf),
    true,
  );
  const history = useConsoleData(
    `credit-history:${partner}:${asOf}:${refreshKey}`,
    () => fetchCreditLimitHistory(partner, asOf),
    partner !== "",
  );

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setMessage(null);
    setSaving(true);
    try {
      await registerCreditLimit({
        partner_id: partner,
        credit_limit_krw: amount,
        effective_from: date,
        evidence_grade: grade,
        source_ref: sourceRef,
        recorded_by: session?.name ?? "console-user",
        note: note || undefined,
      });
      onSaved();
      setMessage("여신한도 이력을 저장했습니다. 새 적용일부터 판매 검증에 반영됩니다.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "저장하지 못했습니다.");
    } finally {
      setSaving(false);
    }
  }

  const current = history.data?.find((row) => row.is_current) ?? null;
  return (
    <Panel
      title="여신한도 등록·변경"
      subtitle="기존 금액을 덮어쓰지 않고 새 적용일의 이력을 추가합니다"
    >
      {partners.loading ? (
        <Skeleton what="거래처" />
      ) : partners.error ? (
        <Failed what="거래처" message={partners.error} />
      ) : (
        <>
        <form className="grid gap-3 sm:grid-cols-2" onSubmit={submit}>
          <label>
            거래처 *
            <select required value={partner} onChange={(event) => setPartner(event.target.value)}>
              <option value="">거래처 선택</option>
              {(partners.data?.rows ?? []).map((row) => (
                <option key={row.partner_id} value={row.partner_id}>
                  {row.partner_name ?? "이름 없음"} ({row.partner_id})
                </option>
              ))}
            </select>
          </label>
          <label>
            새 한도 (원) *
            <input
              required
              type="number"
              min="0"
              step="any"
              value={amount}
              onChange={(event) => setAmount(event.target.value)}
            />
          </label>
          <label>
            적용 시작일 *
            <input
              required
              type="date"
              value={date}
              onChange={(event) => setDate(event.target.value)}
            />
          </label>
          <label>
            근거 등급
            <select value={grade} onChange={(event) => setGrade(event.target.value as Grade)}>
              <option value="OFFICIAL">공식 계약</option>
              <option value="VENDOR">거래처 확인</option>
              <option value="SIM_FIXED">시뮬레이션 고정값</option>
            </select>
          </label>
          <label className="sm:col-span-2">
            근거 참조 *
            <input
              required
              maxLength={240}
              value={sourceRef}
              onChange={(event) => setSourceRef(event.target.value)}
              placeholder="CONTRACT-CUST-001-20260916"
            />
          </label>
          <label className="sm:col-span-2">
            변경 사유
            <input
              maxLength={1000}
              value={note}
              onChange={(event) => setNote(event.target.value)}
            />
          </label>
          <button
            disabled={saving}
            className="w-fit rounded-lg px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"
            style={{ background: "var(--color-brand)" }}
          >
            {saving ? "저장 중" : "한도 저장"}
          </button>
        </form>
        {(partners.data?.rows.length ?? 0) === 0 && (
          <p className="mb-0 mt-3 text-[12px] text-ink2">
            선택 가능한 활성 고객 거래처가 없습니다.
          </p>
        )}
        </>
      )}
      {message && <p className="mb-0 mt-3 text-[13px]">{message}</p>}
      {partner && <CreditHistory state={history} current={current} />}
    </Panel>
  );
}

function CreditHistory({ state, current }: { state: HistoryState; current: CreditLimitHistoryItem | null }) {
  if (state.loading) return <div className="mt-4"><Skeleton what="여신한도 이력" /></div>;
  if (state.error) {
    return <div className="mt-4"><Failed what="여신한도 이력" message={state.error} /></div>;
  }
  if (!state.data || state.data.length === 0) {
    return <p className="mb-0 mt-4 text-[12px] text-ink2">등록된 여신한도 이력이 없습니다.</p>;
  }
  return (
    <div className="mt-5 border-t pt-4" style={{ borderColor: "var(--color-hair)" }}>
      <h3 className="m-0 text-[13px] font-semibold">현재 적용 한도</h3>
      {current ? (
        <CreditHistoryRow row={current} current />
      ) : (
        <p className="mb-0 mt-2 text-[12px] text-ink2">
          선택한 기준일에 적용되는 한도가 없습니다.
        </p>
      )}
      <h3 className="mb-0 mt-5 text-[13px] font-semibold">변경 이력</h3>
      <div className="mt-2 flex flex-col gap-2">
        {state.data.map((row) => (
          <CreditHistoryRow key={row.partner_credit_limit_id} row={row} />
        ))}
      </div>
    </div>
  );
}

function CreditHistoryRow({ row, current = false }: { row: CreditLimitHistoryItem; current?: boolean }) {
  return (
    <div className="mt-2 rounded-lg border p-3 text-[12px]" style={{ borderColor: "var(--color-hair)" }}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <b>{moneyWon(row.credit_limit_krw)}</b>
        <span>
          {row.effective_from} ~ {row.effective_to ?? "현재"}{current ? " · 기준일 적용" : ""}
        </span>
      </div>
      <p className="mb-0 mt-1 text-ink2">
        {creditGradeText(row.evidence_grade)} · {row.source_ref} · 기록자 {row.recorded_by}
      </p>
      {row.note && <p className="mb-0 mt-1 text-ink2">{row.note}</p>}
    </div>
  );
}
