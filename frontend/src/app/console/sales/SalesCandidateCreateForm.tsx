"use client";

import { useState } from "react";

import { Panel } from "@/components/console/Blocks";

import {
  createSalesCandidates,
  type SalesCandidateReply,
} from "./sales_api";

export function SalesCandidateCreateForm({ asOf }: { asOf: string }) {
  const [item, setItem] = useState("");
  const [partnerId, setPartnerId] = useState("");
  const [quantity, setQuantity] = useState("");
  const [price, setPrice] = useState("");
  const [deliveryDate, setDeliveryDate] = useState(asOf);
  const [paymentDays, setPaymentDays] = useState("");
  const [note, setNote] = useState("");
  const [additionalSupply, setAdditionalSupply] = useState(false);
  const [loading, setLoading] = useState(false);
  const [reply, setReply] = useState<SalesCandidateReply | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!item.trim()) {
      setError("품목은 반드시 입력해야 합니다.");
      return;
    }
    setLoading(true);
    setError(null);
    setReply(null);
    try {
      setReply(
        await createSalesCandidates({
          as_of: asOf,
          item,
          partner_id: partnerId,
          quantity_kg: quantity,
          unit_price_krw: price,
          delivery_date: deliveryDate,
          payment_days: paymentDays,
          allow_additional_sourcing: additionalSupply,
          note,
        }),
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "판매 후보를 만들지 못했습니다.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <Panel
      title="판매 후보 만들기"
      subtitle="입력한 조건으로 후보를 만듭니다. 여기서는 판매가 확정되지 않습니다."
    >
      <form className="grid gap-3 sm:grid-cols-2" onSubmit={submit}>
        <Field label="품목 *">
          <input value={item} onChange={(event) => setItem(event.target.value)} required />
        </Field>
        <Field label="거래처 코드">
          <input value={partnerId} onChange={(event) => setPartnerId(event.target.value)} />
        </Field>
        <Field label="희망 수량 (kg)">
          <input inputMode="decimal" min="0" value={quantity} onChange={(event) => setQuantity(event.target.value)} />
        </Field>
        <Field label="희망 단가 (원/kg)">
          <input inputMode="decimal" min="0" value={price} onChange={(event) => setPrice(event.target.value)} />
        </Field>
        <Field label="희망 납기일">
          <input type="date" value={deliveryDate} onChange={(event) => setDeliveryDate(event.target.value)} />
        </Field>
        <Field label="결제일수">
          <input inputMode="numeric" min="0" value={paymentDays} onChange={(event) => setPaymentDays(event.target.value)} />
        </Field>
        <label className="flex items-center gap-2 text-[13px] sm:col-span-2">
          <input type="checkbox" checked={additionalSupply} onChange={(event) => setAdditionalSupply(event.target.checked)} />
          재고가 부족하면 추가 조달 가능성을 함께 검토합니다
        </label>
        <Field label="요청 메모" wide>
          <textarea value={note} onChange={(event) => setNote(event.target.value)} rows={2} />
        </Field>
        <div className="sm:col-span-2">
          <button className="rounded-lg px-4 py-2 text-sm font-semibold text-white disabled:opacity-50" disabled={loading} style={{ background: "var(--color-brand)" }} type="submit">
            {loading ? "후보 생성 중…" : "판매 후보 생성"}
          </button>
        </div>
      </form>
      {error && <p className="mb-0 mt-3 text-[13px]" style={{ color: "var(--color-t-bad)" }}>{error}</p>}
      {reply && <CandidateResult reply={reply} />}
    </Panel>
  );
}

function Field({ children, label, wide = false }: { children: React.ReactNode; label: string; wide?: boolean }) {
  return <label className={`flex flex-col gap-1 text-[12px] ${wide ? "sm:col-span-2" : ""}`}>{label}{children}</label>;
}

function CandidateResult({ reply }: { reply: SalesCandidateReply }) {
  return (
    <div className="mt-4 rounded-lg border p-3 text-[13px]" style={{ borderColor: "var(--color-hair)" }}>
      <b>생성 결과: {reply.status}</b>
      {reply.user_message && <p className="mb-0 mt-1">{reply.user_message}</p>}
      {reply.scenarios && <p className="mb-0 mt-1">후보 {reply.scenarios.length}건이 생성되었습니다. 검토 후 기존 승인 절차에서 확정할 수 있습니다.</p>}
      {(reply.missing_data?.length || reply.missing_capabilities?.length) ? <p className="mb-0 mt-1 text-ink2">추가 확인: {[...(reply.missing_data ?? []), ...(reply.missing_capabilities ?? [])].join(", ")}</p> : null}
    </div>
  );
}
