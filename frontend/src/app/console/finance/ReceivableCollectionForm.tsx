"use client";

import { useMemo, useState, useSyncExternalStore } from "react";

import { Panel } from "@/components/console/Blocks";
import type { FinanceStateView, ReceivableRow } from "@/lib/console_api";
import { sessionSnapshot, serverSnapshot, subscribeSession } from "@/lib/session";

import { recordCollection } from "./credit_api";
import { moneyWon, partnerText } from "./user_text";

export function ReceivableCollectionForm({ simRun, asOf, rows, states, onSaved }: { simRun: string; asOf: string; rows: ReceivableRow[]; states: FinanceStateView[]; onSaved: () => void }) {
  const session = useSyncExternalStore(subscribeSession, sessionSnapshot, serverSnapshot);
  const open = useMemo(() => rows.filter((row) => Number(row.outstanding_amount_krw) > 0), [rows]);
  const [receivableId, setReceivableId] = useState("");
  const [mode, setMode] = useState(states[0]?.financing_mode ?? "");
  const [collectAll, setCollectAll] = useState(true);
  const [amount, setAmount] = useState("");
  const [source, setSource] = useState("");
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const selected = open.find((row) => row.receivable_id === receivableId) ?? null;
  const submit = async (event: React.FormEvent) => {
    event.preventDefault(); setSaving(true); setMessage(null);
    try {
      await recordCollection({ sim_run_id: simRun, financing_mode: mode, collection_date: asOf, receivable_id: receivableId, collect_all: collectAll, amount_krw: collectAll ? undefined : amount, source_ref: source, recorded_by: session?.name ?? "console-user", note: note || undefined });
      setReceivableId(""); setAmount(""); setSource(""); setNote(""); setMessage("수금을 기록했습니다. 받을 돈 목록을 다시 불러옵니다."); onSaved();
    } catch (error) { setMessage(error instanceof Error ? error.message : "수금을 기록하지 못했습니다."); } finally { setSaving(false); }
  };
  return <Panel title="수금 기록" subtitle="실제 입금만 기록합니다. 전액 또는 이번에 받은 금액을 선택할 수 있습니다.">
    {open.length === 0 ? <p className="m-0 text-[16px] text-ink2">기준일에 기록할 미수금이 없습니다.</p> : <form className="finance-form grid gap-3 sm:grid-cols-2" onSubmit={submit}>
      <label className="sm:col-span-2">받을 돈<select required value={receivableId} onChange={(e) => setReceivableId(e.target.value)}><option value="">받을 돈 선택</option>{open.map((row) => <option key={row.receivable_id} value={row.receivable_id}>{partnerText(row.partner_name, row.partner_id)} · 잔액 {moneyWon(row.outstanding_amount_krw)} · 만기 {row.due_date}</option>)}</select></label>
      <label>재무 축<select required value={mode} onChange={(e) => setMode(e.target.value)}>{states.map((state) => <option key={state.financing_mode} value={state.financing_mode}>{state.financing_mode}</option>)}</select></label>
      <label>수금 방식<select value={collectAll ? "ALL" : "PARTIAL"} onChange={(e) => setCollectAll(e.target.value === "ALL")}><option value="ALL">남은 금액 전액 수금</option><option value="PARTIAL">일부 금액 수금</option></select></label>
      {!collectAll && <label>이번에 받은 금액 (원)<input required min="0.000001" step="any" type="number" value={amount} onChange={(e) => setAmount(e.target.value)} /></label>}
      {collectAll && selected && <p className="mb-0 self-end text-[16px] text-ink2">이번 수금액: {moneyWon(selected.outstanding_amount_krw)} (서버가 잔액 기준으로 확정)</p>}
      <label className="sm:col-span-2">입금 근거 자료<input required maxLength={240} value={source} onChange={(e) => setSource(e.target.value)} placeholder="예: 통장거래-2026-0916-001" /><small className="mt-1 block text-[15px] text-ink2">입금 거래번호, 입금표, 확인 메일처럼 실제 수금을 확인할 수 있는 자료를 적어 주세요.</small></label>
      <label className="sm:col-span-2">메모<input maxLength={1000} value={note} onChange={(e) => setNote(e.target.value)} placeholder="예: 거래처 계좌이체 확인" /></label>
      <button disabled={saving || !receivableId || !mode} className="w-fit rounded-lg px-4 py-2 text-[18px] font-semibold text-white disabled:opacity-50" style={{ background: "var(--color-brand)" }}>{saving ? "기록 중" : "수금 기록"}</button>
    </form>}
    {message && <p className="mb-0 mt-3 text-[17px]">{message}</p>}
  </Panel>;
}
