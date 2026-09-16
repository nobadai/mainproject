"use client";

import { useState, useSyncExternalStore } from "react";

import { Panel } from "@/components/console/Blocks";
import type { FinanceStateView } from "@/lib/console_api";
import { sessionSnapshot, serverSnapshot, subscribeSession } from "@/lib/session";

import { recordCashAdjustment } from "./credit_api";

export function CashAdjustmentForm({ simRun, asOf, states, onSaved }: { simRun: string; asOf: string; states: FinanceStateView[]; onSaved: () => void }) {
  const session = useSyncExternalStore(subscribeSession, sessionSnapshot, serverSnapshot);
  const [mode, setMode] = useState(states[0]?.financing_mode ?? "");
  const [direction, setDirection] = useState<"INFLOW" | "OUTFLOW">("INFLOW");
  const [category, setCategory] = useState<"OWNER_INJECTION" | "OWNER_WITHDRAWAL" | "OTHER">("OWNER_INJECTION");
  const [amount, setAmount] = useState("");
  const [source, setSource] = useState("");
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const submit = async (event: React.FormEvent) => {
    event.preventDefault(); setSaving(true); setMessage(null);
    try {
      await recordCashAdjustment({ sim_run_id: simRun, financing_mode: mode, adjustment_date: asOf, direction, category, amount_krw: amount, source_ref: source, recorded_by: session?.name ?? "console-user", note: note || undefined });
      setAmount(""); setSource(""); setNote(""); setMessage("자금 조정을 기록했습니다. 현재 재무 상태를 다시 불러옵니다."); onSaved();
    } catch (error) { setMessage(error instanceof Error ? error.message : "자금 조정을 기록하지 못했습니다."); } finally { setSaving(false); }
  };
  return <Panel title="자금 입금 · 출금" subtitle={`${asOf} 기준일의 재무 상태에 실제 자금 변동을 기록합니다`}>
    <form className="grid gap-3 sm:grid-cols-2" onSubmit={submit}>
      <label>재무 축<select required value={mode} onChange={(e) => setMode(e.target.value)}>{states.map((state) => <option key={state.financing_mode} value={state.financing_mode}>{state.financing_mode}</option>)}</select></label>
      <label>구분<select value={direction} onChange={(e) => { const value = e.target.value as "INFLOW" | "OUTFLOW"; setDirection(value); setCategory(value === "INFLOW" ? "OWNER_INJECTION" : "OWNER_WITHDRAWAL"); }}><option value="INFLOW">자금 입금</option><option value="OUTFLOW">자금 출금</option></select></label>
      <label>유형<select value={category} onChange={(e) => setCategory(e.target.value as "OWNER_INJECTION" | "OWNER_WITHDRAWAL" | "OTHER")}>{direction === "INFLOW" ? <option value="OWNER_INJECTION">대표자 추가 투입</option> : <option value="OWNER_WITHDRAWAL">대표자 인출</option>}<option value="OTHER">기타</option></select></label>
      <label>금액 (원)<input required min="0.000001" step="any" type="number" value={amount} onChange={(e) => setAmount(e.target.value)} /></label>
      <label className="sm:col-span-2">입·출금 근거 자료<input required maxLength={240} value={source} onChange={(e) => setSource(e.target.value)} placeholder="예: 통장거래-2026-0916-001" /><small className="mt-1 block text-[11px] text-ink2">통장 거래번호, 이체 확인서, 내부 승인번호처럼 나중에 확인할 수 있는 자료를 적어 주세요.</small></label>
      <label className="sm:col-span-2">사유<input maxLength={1000} value={note} onChange={(e) => setNote(e.target.value)} placeholder="예: 운영자금 추가 투입" /></label>
      <button disabled={saving || !mode} className="w-fit rounded-lg px-4 py-2 text-sm font-semibold text-white disabled:opacity-50" style={{ background: "var(--color-brand)" }}>{saving ? "기록 중" : direction === "INFLOW" ? "입금 기록" : "출금 기록"}</button>
    </form>
    {message && <p className="mb-0 mt-3 text-[13px]">{message}</p>}
  </Panel>;
}
