"use client";

/**
 * 비용 운영 — **등록 · 지급 · 취소.**
 *
 * 🔴 **화면이 숫자를 만들지 않는다.** 지급 뒤 남은 현금도, 마감 분류도, 미래 투영도
 *    재무가 센 값이다. 여기서는 사용자가 무엇을 하려는지 받아 그대로 보내고, 돌아온
 *    사실을 적는다.
 *
 * 🔴 **비활성 버튼은 안내이지 검증이 아니다.** 이미 지급된 비용의 지급 버튼을 숨기는
 *    것은 사용자를 돕는 일이고, 그것이 실제로 막히는 자리는 원장 잠금 안이다. 둘 다
 *    있어야 한다 — 화면만 있으면 두 창을 띄운 사용자가 두 번 지급한다.
 *
 * ★ **지급일 미상을 «발생일에 지급» 으로 적지 않는다.** 마감은 지급일을 모르는 기존
 *   행에 한해 발생일을 지급 기준일로 읽지만(읽기 전용 호환), 화면이 그 날짜를 지급일로
 *   보여 주면 사용자가 통장과 맞춰 보다 원인을 못 찾는다.
 */

import { useEffect, useState } from "react";

import { Panel } from "@/components/console/Blocks";
import type { ExpenseRow, ExpenseStatus, FinanceStateView } from "@/lib/console_api";

import { cancelExpense, createExpense, fetchExpenseCategories, settleExpense } from "./credit_api";
import { expenseStatusText, moneyWon } from "./user_text";

const CATEGORY_LABELS: Record<string, string> = {
  RENT: "임차료", UTILITY: "수도광열비", COMMISSION: "수수료",
  PACKAGING: "포장비", DISPOSAL: "폐기비용", OTHER: "기타",
  PAYROLL: "급여", INTEREST: "이자비용", LOAN_INTEREST: "대출이자",
};

function categoryLabel(name: string): string {
  return CATEGORY_LABELS[name] ?? name;
}

/** 지급일 칸에 무엇을 적을 것인가. **모르면 모른다고 적는다.** */
export function paidDateText(row: ExpenseRow): string {
  if (row.status !== "PAID") return "—";
  if (row.paid_date_known && row.paid_date) return row.paid_date;
  return "지급일 미상 (기존 데이터)";
}

function StatusTag({ status }: { status: ExpenseStatus }) {
  const tone =
    status === "PAID" ? "var(--color-t-good)"
      : status === "CANCELLED" ? "var(--color-mut2)"
        : "var(--color-t-warn)";
  return (
    <span className="rounded-full px-2 py-0.5 text-[11px] font-semibold text-white" style={{ background: tone }}>
      {expenseStatusText(status)}
    </span>
  );
}

export function ExpenseCreateForm({ simRun, asOf, onSaved }: { simRun: string; asOf: string; onSaved: () => void }) {
  const [categories, setCategories] = useState<string[]>([]);
  const [category, setCategory] = useState("");
  const [amount, setAmount] = useState("");
  const [arose, setArose] = useState(asOf);
  const [due, setDue] = useState(asOf);
  const [evidence, setEvidence] = useState("");
  const [delivery, setDelivery] = useState("");
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  //  ★ 분류 목록은 **백엔드가 준다.** 화면이 손으로 적으면, 원장이 새 분류를 받는 날
  //    화면만 모르게 된다.
  useEffect(() => {
    let live = true;
    fetchExpenseCategories()
      .then((names) => { if (live) { setCategories(names); setCategory((now) => now || names[0] || ""); } })
      .catch(() => { if (live) setMessage("비용 분류를 불러오지 못했습니다."); });
    return () => { live = false; };
  }, []);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault(); setSaving(true); setMessage(null);
    try {
      await createExpense({
        sim_run_id: simRun, expense_date: arose, due_date: due, expense_category: category,
        amount_krw: amount, evidence_id: evidence,
        related_delivery_id: delivery || undefined, note: note || undefined,
      });
      setAmount(""); setEvidence(""); setDelivery(""); setNote("");
      setMessage("비용을 등록했습니다. 아직 지급되지 않은 상태로 기록됩니다.");
      onSaved();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "비용을 등록하지 못했습니다.");
    } finally { setSaving(false); }
  };

  return (
    <Panel title="비용 등록" subtitle="등록한 비용은 «지급 예정» 으로 기록됩니다 — 현금은 지급할 때 빠집니다">
      <form className="grid gap-3 sm:grid-cols-2" onSubmit={submit}>
        <label>비용 분류
          <select required value={category} onChange={(e) => setCategory(e.target.value)}>
            {categories.map((name) => <option key={name} value={name}>{categoryLabel(name)}</option>)}
          </select>
        </label>
        <label>금액 (원)
          <input required min="0.000001" step="any" inputMode="decimal" type="number" value={amount} onChange={(e) => setAmount(e.target.value)} placeholder="예: 1500000" />
        </label>
        <label>발생일
          <input required type="date" value={arose} onChange={(e) => setArose(e.target.value)} />
          <small className="mt-1 block text-[11px] text-ink2">비용이 생긴 날입니다.</small>
        </label>
        <label>지급 예정일
          <input required type="date" value={due} min={arose} onChange={(e) => setDue(e.target.value)} />
          <small className="mt-1 block text-[11px] text-ink2">이 날짜로 앞으로 나갈 돈에 잡힙니다.</small>
        </label>
        <label className="sm:col-span-2">확인 자료
          <input required maxLength={240} value={evidence} onChange={(e) => setEvidence(e.target.value)} placeholder="예: 세금계산서-2026-0916-001" />
          <small className="mt-1 block text-[11px] text-ink2">계산서 번호·계약서·청구서처럼 나중에 확인할 수 있는 자료를 적어 주세요.</small>
        </label>
        <label>관련 납품 (선택)
          <input maxLength={120} value={delivery} onChange={(e) => setDelivery(e.target.value)} placeholder="예: DELIVERY-2026-0916-01" />
          <small className="mt-1 block text-[11px] text-ink2">납품에 붙은 비용이면 물류비로 집계됩니다.</small>
        </label>
        <label>메모 (선택)
          <input maxLength={1000} value={note} onChange={(e) => setNote(e.target.value)} />
        </label>
        <button disabled={saving || !category} className="w-fit rounded-lg px-4 py-2 text-sm font-semibold text-white disabled:opacity-50" style={{ background: "var(--color-brand)" }}>
          {saving ? "등록 중" : "비용 등록"}
        </button>
      </form>
      {message && <p className="mb-0 mt-3 text-[13px]">{message}</p>}
    </Panel>
  );
}

export function ExpenseActions({ simRun, asOf, rows, states, onChanged }: {
  simRun: string; asOf: string; rows: ExpenseRow[]; states: FinanceStateView[]; onChanged: () => void;
}) {
  const accrued = rows.filter((row) => row.status === "ACCRUED");
  const [mode, setMode] = useState(states[0]?.financing_mode ?? "");
  const [pending, setPending] = useState<ExpenseRow | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const run = async (action: "settle" | "cancel", row: ExpenseRow) => {
    setBusy(true); setMessage(null);
    try {
      if (action === "settle") {
        const result = await settleExpense(row.expense_id, { sim_run_id: simRun, financing_mode: mode, paid_date: asOf });
        setMessage(`지급을 기록했습니다. 남은 현금은 ${moneyWon(result.current_cash_krw)}입니다.`);
      } else {
        await cancelExpense(row.expense_id, { sim_run_id: simRun });
        setMessage("비용을 취소했습니다. 현금은 변하지 않습니다.");
      }
      setPending(null);
      onChanged();
    } catch (error) {
      setPending(null);
      setMessage(error instanceof Error ? error.message : "처리하지 못했습니다.");
    } finally { setBusy(false); }
  };

  if (accrued.length === 0) {
    return (
      <Panel title="지급 대기 비용">
        <p className="m-0 text-[12px] text-ink2">기준일까지 지급을 기다리는 비용이 없습니다.</p>
      </Panel>
    );
  }

  return (
    <Panel title="지급 대기 비용" subtitle="지급하면 그 기준일 장부에서 현금이 빠집니다. 취소하면 현금은 변하지 않습니다.">
      {states.length > 1 && (
        <label className="mb-3 block">지급할 장부
          <select value={mode} onChange={(e) => setMode(e.target.value)}>
            {states.map((state) => <option key={state.financing_mode} value={state.financing_mode}>{state.financing_mode}</option>)}
          </select>
        </label>
      )}
      <ul className="m-0 grid list-none gap-2 p-0">
        {accrued.map((row) => (
          <li key={row.expense_id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border px-3 py-2" style={{ borderColor: "var(--color-hair)" }}>
            <span className="text-[13px]">
              <b>{row.display_category}</b> · {moneyWon(row.amount_krw)}
              <span className="ml-2 text-ink2">지급 예정 {row.due_date ?? "미지정"}</span>
            </span>
            <span className="flex gap-2">
              <button type="button" disabled={busy || !mode} onClick={() => setPending(row)} className="rounded-lg px-3 py-1.5 text-[13px] font-semibold text-white disabled:opacity-50" style={{ background: "var(--color-brand)" }}>지급</button>
              <button type="button" disabled={busy} onClick={() => run("cancel", row)} className="rounded-lg border px-3 py-1.5 text-[13px] disabled:opacity-50">취소</button>
            </span>
          </li>
        ))}
      </ul>
      {pending && (
        <section className="mt-4 rounded-xl border p-4" style={{ borderColor: "var(--color-hair)" }}>
          <h3 className="m-0 text-sm font-semibold">이 비용을 지급 처리할까요?</h3>
          <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 text-[13px]">
            <dt className="text-ink2">비용</dt><dd className="m-0 break-all">{pending.expense_id}</dd>
            <dt className="text-ink2">분류</dt><dd className="m-0">{pending.display_category}</dd>
            <dt className="text-ink2">금액</dt><dd className="m-0">{moneyWon(pending.amount_krw)}</dd>
            <dt className="text-ink2">지급일</dt><dd className="m-0">{asOf}</dd>
            <dt className="text-ink2">현재 상태</dt><dd className="m-0"><StatusTag status={pending.status} /></dd>
          </dl>
          <div className="mt-4 flex gap-2">
            <button type="button" onClick={() => setPending(null)} disabled={busy} className="rounded-lg border px-3 py-2 text-sm">그만두기</button>
            <button type="button" onClick={() => run("settle", pending)} disabled={busy} className="rounded-lg px-3 py-2 text-sm font-semibold text-white disabled:opacity-50" style={{ background: "var(--color-brand)" }}>{busy ? "처리 중" : "이 내용으로 지급"}</button>
          </div>
        </section>
      )}
      {message && <p className="mb-0 mt-3 text-[13px]">{message}</p>}
    </Panel>
  );
}

export { StatusTag as ExpenseStatusTag };
