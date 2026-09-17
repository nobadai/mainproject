"use client";

import { useState, useSyncExternalStore } from "react";

import { Panel } from "@/components/console/Blocks";
import { Failed, Skeleton, useConsoleData } from "@/components/console/ConsoleData";
import { salesConsole } from "@/lib/console_api";
import { sessionSnapshot, serverSnapshot, subscribeSession } from "@/lib/session";

import { fetchCreditLimitHistory, registerCreditLimit, type CreditLimitHistoryItem } from "./credit_api";
import { creditGradeText, moneyWon } from "./user_text";

type Grade = "OFFICIAL" | "VENDOR" | "SIM_FIXED";
type HistoryState = { data: CreditLimitHistoryItem[] | null; error: string | null; loading: boolean };
const DEFAULT_GRADE: Grade = "VENDOR";

function recordedByText(value: string): string {
  return value === "LEGACY_UNKNOWN" ? "기존 데이터 · 입력자 미상" : value;
}

export function CreditLimitForm({ simRun, asOf, refreshKey, onSaved }: { simRun: string; asOf: string; refreshKey: number; onSaved: () => void }) {
  const session = useSyncExternalStore(subscribeSession, sessionSnapshot, serverSnapshot);
  const [partner, setPartner] = useState("");
  const [pendingPartner, setPendingPartner] = useState<string | null>(null);
  const [amount, setAmount] = useState("");
  const [date, setDate] = useState(asOf);
  const [grade, setGrade] = useState<Grade>(DEFAULT_GRADE);
  const [sourceRef, setSourceRef] = useState("");
  const [note, setNote] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const partners = useConsoleData(`credit-partners:${simRun}:${asOf}`, () => salesConsole.activeCustomers(simRun, asOf), true);
  const history = useConsoleData(`credit-history:${partner}:${asOf}:${refreshKey}`, () => fetchCreditLimitHistory(partner, asOf), partner !== "");
  const current = history.data?.find((row) => row.is_current) ?? null;
  const partnerName = (partners.data?.rows ?? []).find((row) => row.partner_id === partner)?.partner_name ?? partner;
  const hasDraft = amount !== "" || sourceRef !== "" || note !== "" || date !== asOf || grade !== DEFAULT_GRADE;
  const evidenceHint = grade === "OFFICIAL" ? "계약서 번호 또는 공식 공문 번호를 입력하세요." : grade === "VENDOR" ? "거래처 확인서·메일·협의 기록의 식별값을 입력하세요." : "이 시뮬레이션에서 사용하기로 확정한 기준의 식별값을 입력하세요.";

  function resetDraft() {
    setAmount(""); setDate(asOf); setGrade(DEFAULT_GRADE); setSourceRef(""); setNote("");
  }
  function requestPartnerChange(next: string) {
    if (next === partner) return;
    if (partner !== "" && hasDraft) { setPendingPartner(next); return; }
    setPartner(next); setMessage(null);
  }
  async function save() {
    setSaving(true); setMessage(null);
    try {
      await registerCreditLimit({ partner_id: partner, credit_limit_krw: amount, effective_from: date, evidence_grade: grade, source_ref: sourceRef, recorded_by: session?.name ?? "console-user", note: note || undefined });
      resetDraft(); setConfirming(false); onSaved();
      setMessage(`${partnerName}의 새 여신한도를 저장했습니다. ${date}부터 판매 재무 검증에 적용됩니다.`);
    } catch (error) {
      setConfirming(false); setMessage(error instanceof Error ? error.message : "저장하지 못했습니다.");
    } finally { setSaving(false); }
  }
  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); setMessage(null); setConfirming(true);
  }

  return (
    <Panel title="여신한도 등록·변경" subtitle="기존 금액을 덮어쓰지 않고 새 적용일의 이력을 추가합니다">
      {partners.loading ? <Skeleton what="거래처" /> : partners.error ? <Failed what="거래처" message={partners.error} /> : <>
        <form className="finance-form grid gap-3 sm:grid-cols-2" onSubmit={submit}>
          <label>거래처 *
            <select required value={partner} onChange={(event) => requestPartnerChange(event.target.value)} disabled={saving}>
              <option value="">거래처 선택</option>
              {(partners.data?.rows ?? []).map((row) => <option key={row.partner_id} value={row.partner_id}>{row.partner_name ?? "이름 없음"} ({row.partner_id})</option>)}
            </select>
          </label>
          <label>새 한도 (원) *<input required type="number" min="0" step="any" value={amount} onChange={(event) => setAmount(event.target.value)} /></label>
          <label>적용 시작일 *<input required type="date" value={date} onChange={(event) => setDate(event.target.value)} /></label>
          <label>근거 등급
            <select value={grade} onChange={(event) => setGrade(event.target.value as Grade)}><option value="OFFICIAL">공식 계약</option><option value="VENDOR">거래처 확인</option><option value="SIM_FIXED">시뮬레이션 고정값</option></select>
          </label>
          <label className="sm:col-span-2">한도 근거 자료 *<input required maxLength={240} value={sourceRef} onChange={(event) => setSourceRef(event.target.value)} placeholder={grade === "OFFICIAL" ? "예: 계약서-2026-0916" : grade === "VENDOR" ? "예: 거래처확인-메일-0916" : "예: 시뮬레이션기준-01"} /><small className="mt-1 block text-[11px] text-ink2">{evidenceHint}</small></label>
          <label className="sm:col-span-2">변경 사유<input maxLength={1000} value={note} onChange={(event) => setNote(event.target.value)} /></label>
          <button disabled={saving} className="w-fit rounded-lg px-4 py-2 text-sm font-semibold text-white disabled:opacity-50" style={{ background: "var(--color-brand)" }}>{saving ? "저장 중" : "한도 변경"}</button>
        </form>
        {(partners.data?.rows.length ?? 0) === 0 && <p className="mb-0 mt-3 text-[12px] text-ink2">선택 가능한 활성 고객 거래처가 없습니다.</p>}
      </>}
      {pendingPartner !== null && <ConfirmBox title="입력 중인 여신한도 정보가 있습니다." description="거래처를 변경하면 입력 내용이 초기화됩니다." onCancel={() => setPendingPartner(null)} onConfirm={() => { setPartner(pendingPartner); setPendingPartner(null); resetDraft(); setMessage(null); }} confirmText="거래처 변경" />}
      {confirming && <ConfirmBox title="여신한도를 변경할까요?" description="아래 조건을 다시 확인한 뒤 한도 변경을 누르세요." onCancel={() => setConfirming(false)} onConfirm={save} confirmText={saving ? "저장 중..." : "한도 변경"} disabled={saving}>
        <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[12px] text-ink2">
          <dt>거래처</dt><dd className="m-0 text-ink">{partnerName}</dd><dt>{asOf} 기준 적용 한도</dt><dd className="m-0 text-ink">{current ? moneyWon(current.credit_limit_krw) : "등록된 한도 없음"}</dd><dt>새 한도</dt><dd className="m-0 text-ink">{amount === "" ? "입력 없음" : moneyWon(amount)}</dd><dt>적용 시작일</dt><dd className="m-0 text-ink">{date}</dd><dt>근거 등급</dt><dd className="m-0 text-ink">{creditGradeText(grade)}</dd><dt>한도 근거 자료</dt><dd className="m-0 break-all text-ink">{sourceRef}</dd>{note && <><dt>변경 사유</dt><dd className="m-0 text-ink">{note}</dd></>}
        </dl>
      </ConfirmBox>}
      {message && <p className="mb-0 mt-3 text-[13px]">{message}</p>}
      {partner && <CreditHistory state={history} current={current} asOf={asOf} />}
    </Panel>
  );
}

function ConfirmBox({ title, description, onCancel, onConfirm, confirmText, disabled = false, children }: { title: string; description: string; onCancel: () => void; onConfirm: () => void; confirmText: string; disabled?: boolean; children?: React.ReactNode }) {
  return <section className="mt-4 rounded-lg border bg-[var(--color-desk)] p-3 text-[12px]" style={{ borderColor: "var(--color-hair)" }}><p className="m-0 font-semibold">{title}</p><p className="mb-0 mt-1 text-ink2">{description}</p>{children}<div className="mt-3 flex flex-wrap gap-2"><button type="button" onClick={onCancel} disabled={disabled} className="rounded-lg border px-3 py-2 font-semibold">취소</button><button type="button" onClick={onConfirm} disabled={disabled} className="rounded-lg border px-3 py-2 font-semibold" style={{ borderColor: "var(--color-brand)", color: "var(--color-brand)" }}>{confirmText}</button></div></section>;
}

function CreditHistory({ state, current, asOf }: { state: HistoryState; current: CreditLimitHistoryItem | null; asOf: string }) {
  const [showAll, setShowAll] = useState(false);
  const [copyMessage, setCopyMessage] = useState<string | null>(null);
  if (state.loading) return <div className="mt-4"><Skeleton what="여신한도 이력" /></div>;
  if (state.error) return <div className="mt-4"><Failed what="여신한도 이력" message={state.error} /></div>;
  if (!state.data || state.data.length === 0) return <p className="mb-0 mt-4 text-[12px] text-ink2">{asOf} 기준 적용되는 여신한도가 없습니다.</p>;
  const rows = showAll ? state.data : state.data.slice(0, 3);
  async function copySourceRef(value: string) {
    try { await navigator.clipboard.writeText(value); setCopyMessage("근거 참조를 복사했습니다."); } catch { setCopyMessage("근거 참조를 복사하지 못했습니다."); }
  }
  return <div className="mt-5 border-t pt-4" style={{ borderColor: "var(--color-hair)" }}>
    <h3 className="m-0 text-[13px] font-semibold">{asOf} 기준 적용 한도</h3>
    {current ? <CreditHistoryRow row={current} current onCopy={copySourceRef} /> : <p className="mb-0 mt-2 text-[12px] text-ink2">{asOf} 기준 적용되는 여신한도가 없습니다.</p>}
    <h3 className="mb-0 mt-5 text-[13px] font-semibold">변경 이력 {state.data.length}건</h3>
    <div className="mt-2 flex flex-col gap-2">{rows.map((row) => <CreditHistoryRow key={row.partner_credit_limit_id} row={row} onCopy={copySourceRef} />)}</div>
    {state.data.length > 3 && <button type="button" onClick={() => setShowAll((value) => !value)} className="mt-3 rounded-lg border px-3 py-2 text-[12px] font-semibold" style={{ borderColor: "var(--color-hair)" }}>{showAll ? "최근 이력만 보기" : "전체 이력 보기"}</button>}
    {copyMessage && <p className="mb-0 mt-2 text-[12px] text-ink2">{copyMessage}</p>}
  </div>;
}

function CreditHistoryRow({ row, current = false, onCopy }: { row: CreditLimitHistoryItem; current?: boolean; onCopy: (value: string) => void }) {
  return <div className="mt-2 rounded-lg border p-3 text-[12px]" style={{ borderColor: "var(--color-hair)" }}><div className="flex flex-wrap items-center justify-between gap-2"><b>{moneyWon(row.credit_limit_krw)}</b><span>{row.effective_from} ~ {row.effective_to ?? "현재"}{current ? " · 기준일 적용" : ""}</span></div><p className="mb-0 mt-1 text-ink2">{creditGradeText(row.evidence_grade)} · {row.source_ref} <button type="button" onClick={() => onCopy(row.source_ref)} className="ml-1 underline">복사</button></p><p className="mb-0 mt-1 text-ink2">기록자 {recordedByText(row.recorded_by)}</p>{row.note && <p className="mb-0 mt-1 text-ink2">{row.note}</p>}</div>;
}
