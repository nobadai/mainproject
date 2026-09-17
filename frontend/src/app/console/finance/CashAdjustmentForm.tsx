"use client";

import { useState, useSyncExternalStore } from "react";

import { Panel } from "@/components/console/Blocks";
import type { FinanceStateView } from "@/lib/console_api";
import { sessionSnapshot, serverSnapshot, subscribeSession } from "@/lib/session";

import { recordCashAdjustment } from "./credit_api";
import { financingModeText } from "./user_text";

type Direction = "INFLOW" | "OUTFLOW";
type Category = "OWNER_INJECTION" | "OWNER_WITHDRAWAL" | "OTHER";

function categoryText(category: Category): string {
  if (category === "OWNER_INJECTION") return "대표자 추가 투입";
  if (category === "OWNER_WITHDRAWAL") return "대표자 인출";
  return "기타 자금 조정";
}

export function CashAdjustmentForm({ simRun, asOf, states, onSaved }: { simRun: string; asOf: string; states: FinanceStateView[]; onSaved: () => void }) {
  const session = useSyncExternalStore(subscribeSession, sessionSnapshot, serverSnapshot);
  const [mode, setMode] = useState(states[0]?.financing_mode ?? "");
  const [direction, setDirection] = useState<Direction>("INFLOW");
  const [category, setCategory] = useState<Category>("OWNER_INJECTION");
  const [amount, setAmount] = useState("");
  const [source, setSource] = useState("");
  const [note, setNote] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const chooseDirection = (value: Direction) => {
    setDirection(value);
    setCategory(value === "INFLOW" ? "OWNER_INJECTION" : "OWNER_WITHDRAWAL");
  };
  const requestConfirmation = (event: React.FormEvent) => {
    event.preventDefault();
    setMessage(null);
    setConfirming(true);
  };
  const submit = async () => {
    setSaving(true);
    setMessage(null);
    try {
      await recordCashAdjustment({ sim_run_id: simRun, financing_mode: mode, adjustment_date: asOf, direction, category, amount_krw: amount, source_ref: source, recorded_by: session?.name ?? "console-user", note: note || undefined });
      setAmount(""); setSource(""); setNote(""); setConfirming(false);
      setMessage(`${direction === "INFLOW" ? "입금" : "출금"}을 기록했습니다. 현재 재무 상태를 다시 불러옵니다.`);
      onSaved();
    } catch (error) {
      setConfirming(false);
      setMessage(error instanceof Error ? error.message : "자금 조정을 기록하지 못했습니다.");
    } finally { setSaving(false); }
  };

  return (
    <Panel title="자금 입금 · 출금" subtitle={`${asOf} 기준일 장부에 실제 자금 변동을 기록합니다`}>
      <form className="finance-form grid gap-4 sm:grid-cols-2" onSubmit={requestConfirmation}>
        {states.length > 1 ? (
          <label className="sm:col-span-2">장부 기준
            <select required value={mode} onChange={(event) => setMode(event.target.value)}>{states.map((state) => <option key={state.financing_mode} value={state.financing_mode}>{financingModeText(state.financing_mode)}</option>)}</select>
            <small className="mt-1 block text-[15px] text-ink2">변동을 반영할 재무 장부를 선택합니다.</small>
          </label>
        ) : <p className="m-0 rounded-lg bg-surface2 px-3 py-2 text-[16px] text-ink2 sm:col-span-2">기록 장부: {financingModeText(mode)}</p>}
        <fieldset className="sm:col-span-2">
          <legend className="mb-2 text-[18px] font-semibold">어떤 변동인가요?</legend>
          <div className="grid gap-2 sm:grid-cols-2">{(["INFLOW", "OUTFLOW"] as const).map((value) => <button key={value} type="button" aria-pressed={direction === value} onClick={() => chooseDirection(value)} className="rounded-lg border px-3 py-3 text-left text-[18px] font-semibold" style={{ borderColor: direction === value ? "var(--color-brand)" : "var(--color-hair)" }}>
            {value === "INFLOW" ? "돈을 넣을게요" : "돈을 뺄게요"}<span className="mt-1 block text-[15px] font-normal text-ink2">{value === "INFLOW" ? "추가 투입이나 기타 입금" : "대표자 인출이나 기타 출금"}</span>
          </button>)}</div>
        </fieldset>
        <label>처리 유형<select value={category} onChange={(event) => setCategory(event.target.value as Category)}>{direction === "INFLOW" ? <option value="OWNER_INJECTION">대표자 추가 투입</option> : <option value="OWNER_WITHDRAWAL">대표자 인출</option>}<option value="OTHER">기타</option></select></label>
        <label>금액 (원)<input required min="0.000001" step="any" inputMode="decimal" type="number" value={amount} onChange={(event) => setAmount(event.target.value)} placeholder="예: 1000000" /><small className="mt-1 block text-[15px] text-ink2">0보다 큰 실제 변동 금액을 입력합니다.</small></label>
        <label className="sm:col-span-2">확인 자료<input required maxLength={240} value={source} onChange={(event) => setSource(event.target.value)} placeholder="예: 통장 거래번호 또는 이체 확인서 번호" /><small className="mt-1 block text-[15px] text-ink2">통장 거래번호·이체 확인서·내부 승인번호처럼 나중에 확인할 수 있는 자료를 적어 주세요.</small></label>
        <label className="sm:col-span-2">메모 (선택)<input maxLength={1000} value={note} onChange={(event) => setNote(event.target.value)} placeholder="예: 운영자금 추가 투입" /></label>
        <button disabled={saving || !mode} className="w-fit rounded-lg px-4 py-2 text-[18px] font-semibold text-white disabled:opacity-50" style={{ background: "var(--color-brand)" }}>{direction === "INFLOW" ? "입금 내용 확인" : "출금 내용 확인"}</button>
      </form>
      {confirming && <section className="mt-4 rounded-xl border p-4" style={{ borderColor: "var(--color-hair)" }}>
        <h3 className="m-0 text-[18px] font-semibold">이 자금 변동을 기록할까요?</h3>
        <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 text-[17px]"><dt className="text-ink2">구분</dt><dd className="m-0">{direction === "INFLOW" ? "입금" : "출금"}</dd><dt className="text-ink2">처리 유형</dt><dd className="m-0">{categoryText(category)}</dd><dt className="text-ink2">금액</dt><dd className="m-0">{Number(amount).toLocaleString("ko-KR")}원</dd><dt className="text-ink2">기준일</dt><dd className="m-0">{asOf}</dd><dt className="text-ink2">확인 자료</dt><dd className="m-0 break-all">{source}</dd></dl>
        <div className="mt-4 flex gap-2"><button type="button" onClick={() => setConfirming(false)} disabled={saving} className="rounded-lg border px-3 py-2 text-[18px]">수정</button><button type="button" onClick={submit} disabled={saving} className="rounded-lg px-3 py-2 text-[18px] font-semibold text-white disabled:opacity-50" style={{ background: "var(--color-brand)" }}>{saving ? "기록 중" : "이 내용으로 기록"}</button></div>
      </section>}
      {message && <p className="mb-0 mt-3 text-[17px]">{message}</p>}
    </Panel>
  );
}
