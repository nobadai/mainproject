"use client";

import { useState } from "react";

import { Panel } from "@/components/console/Blocks";
import { registerCreditLimit } from "./credit_api";
import { sessionSnapshot, serverSnapshot, subscribeSession } from "@/lib/session";
import { useSyncExternalStore } from "react";

export function CreditLimitForm({ asOf, onSaved }: { asOf: string; onSaved: () => void }) {
  const session = useSyncExternalStore(subscribeSession, sessionSnapshot, serverSnapshot);
  const [partner, setPartner] = useState(""); const [amount, setAmount] = useState("");
  const [date, setDate] = useState(asOf); const [grade, setGrade] = useState<"OFFICIAL" | "VENDOR" | "SIM_FIXED">("VENDOR");
  const [note, setNote] = useState(""); const [message, setMessage] = useState<string | null>(null);
  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); setMessage(null);
    try { await registerCreditLimit({ partner_id: partner, credit_limit_krw: amount, effective_from: date, evidence_grade: grade, recorded_by: session?.name ?? "console-user", note }); onSaved(); setMessage("여신한도 이력을 저장했습니다. 새 적용일부터 판매 검증에 반영됩니다."); }
    catch (error) { setMessage(error instanceof Error ? error.message : "저장하지 못했습니다."); }
  }
  return <Panel title="여신한도 등록·변경" subtitle="기존 금액을 덮어쓰지 않고 새 적용일의 이력을 추가합니다."><form className="grid gap-3 sm:grid-cols-2" onSubmit={submit}>
    <label>거래처 코드 *<input required value={partner} onChange={(e) => setPartner(e.target.value)} /></label>
    <label>한도 (원) *<input required inputMode="decimal" min="0" value={amount} onChange={(e) => setAmount(e.target.value)} /></label>
    <label>적용 시작일 *<input required type="date" value={date} onChange={(e) => setDate(e.target.value)} /></label>
    <label>근거 등급<select value={grade} onChange={(e) => setGrade(e.target.value as typeof grade)}><option value="OFFICIAL">공식 계약</option><option value="VENDOR">거래처 확인</option><option value="SIM_FIXED">시뮬레이션 고정값</option></select></label>
    <label className="sm:col-span-2">사유<input value={note} onChange={(e) => setNote(e.target.value)} /></label>
    <button className="w-fit rounded-lg px-4 py-2 text-sm font-semibold text-white" style={{ background: "var(--color-brand)" }}>한도 저장</button>
  </form>{message && <p className="mb-0 mt-3 text-[13px]">{message}</p>}</Panel>;
}
