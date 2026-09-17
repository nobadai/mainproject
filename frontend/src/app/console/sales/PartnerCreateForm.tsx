"use client";

/**
 * 새 거래처 등록.
 *
 * 🔴 **여신 한도 칸이 없다.** 정본은 재무의 `partner_credit_limits` 이고, 판매가 그
 *    표를 쓰지 않는다. 등록 뒤에 «재무에서 따로 설정한다» 고 안내만 한다.
 *
 * 🔴 **`localStorage` 에 담지 않는다.** 저장 뒤에는 목록을 **다시 읽어** 장부가 가진
 *    행을 보여 준다 — 화면에만 남은 거래처는 새로고침 한 번에 사라진다.
 *
 * 🔴 **거래처 코드를 코드가 지어내지 않는다.** 저장소에 `partner_id` 생성 규칙이
 *    없어(2026-09-14 전수 확인) 임의 형식을 만들면 그날부터 그것이 규칙이 된다.
 *    사용자에게 받되 «내부 거래처 코드» 라는 이름으로 묻는다.
 */

import { useState } from "react";

import { Panel } from "@/components/console/Blocks";
import {
  createPartner,
  PARTNER_TYPES,
  SalesApiError,
  type PartnerCreateInput,
  type PartnerProfileRow,
} from "./sales_api";

type Draft = {
  partner_id: string;
  partner_name: string;
  partner_type: string;
  client_type: string;
  factory_region: string;
  factory_city: string;
  factory_area: string;
  sales_collection_days: string;
  pricing_contract_type: string;
  note: string;
  active: boolean;
};

const EMPTY: Draft = {
  partner_id: "",
  partner_name: "",
  partner_type: "CUSTOMER",
  client_type: "",
  factory_region: "",
  factory_city: "",
  factory_area: "",
  sales_collection_days: "",
  pricing_contract_type: "",
  note: "",
  active: true,
};

/** 사용자가 안 적은 칸은 **보내지 않는다** — 빈 문자열로 채우면 «빈 이름» 이 저장된다. */
function toInput(draft: Draft): PartnerCreateInput {
  const input: PartnerCreateInput = {
    partner_id: draft.partner_id.trim(),
    partner_name: draft.partner_name.trim(),
    partner_type: draft.partner_type,
    active: draft.active,
  };
  const optional = [
    ["client_type", draft.client_type],
    ["factory_region", draft.factory_region],
    ["factory_city", draft.factory_city],
    ["factory_area", draft.factory_area],
    ["pricing_contract_type", draft.pricing_contract_type],
    ["note", draft.note],
  ] as const;
  for (const [key, value] of optional) {
    const trimmed = value.trim();
    if (trimmed !== "") input[key] = trimmed;
  }
  const days = draft.sales_collection_days.trim();
  //  ⚠️ 빈 칸은 «안 정했다» 이지 0일이 아니다.
  if (days !== "") input.sales_collection_days = Number(days);
  return input;
}

/** 서버에 보내기 전에 화면이 먼저 잡을 수 있는 것만 잡는다. 나머지는 서버가 답한다. */
function localProblems(draft: Draft): string[] {
  const problems: string[] = [];
  if (draft.partner_id.trim() === "") problems.push("내부 거래처 코드를 입력해 주세요.");
  if (draft.partner_name.trim() === "") problems.push("거래처명을 입력해 주세요.");
  const days = draft.sales_collection_days.trim();
  if (days !== "") {
    const parsed = Number(days);
    if (!Number.isInteger(parsed) || parsed < 0 || parsed > 365) {
      problems.push("결제일수는 0에서 365 사이의 정수로 입력해 주세요.");
    }
  }
  return problems;
}

export function PartnerCreateForm({ onCreated }: { onCreated: () => void }) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [problems, setProblems] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [created, setCreated] = useState<PartnerProfileRow | null>(null);

  function reset() {
    setDraft(EMPTY);
    setProblems([]);
    setCreated(null);
  }

  function submit() {
    const local = localProblems(draft);
    if (local.length > 0) {
      setProblems(local);
      return;
    }
    setSaving(true);
    setProblems([]);
    setCreated(null);
    createPartner(toInput(draft))
      .then((row) => {
        //  ★ 보여 주는 것은 보낸 값이 아니라 **저장된 행**이다.
        setCreated(row);
        setDraft(EMPTY);
        //  ★ 목록을 다시 읽는다 — 화면에만 남은 거래처를 만들지 않는다.
        onCreated();
      })
      .catch((failure: unknown) => setProblems([explain(failure)]))
      .finally(() => setSaving(false));
  }

  if (!open) {
    return (
      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={() => {
            reset();
            setOpen(true);
          }}
          className="rounded-lg border px-3 py-2 text-[16px]"
          style={{ borderColor: "var(--color-t-info)", color: "var(--color-t-info)" }}
        >
          + 거래처 추가
        </button>
        {created && (
          <span className="text-[15.5px]" style={{ color: "var(--color-t-good)" }}>
            {created.partner_name} 이(가) 등록됐습니다.
          </span>
        )}
      </div>
    );
  }

  return (
    <Panel title="새 거래처 등록" subtitle="거래처 원장에 바로 저장됩니다 — 실행과 무관한 행입니다">
      <div className="grid gap-x-3 gap-y-1 sm:grid-cols-3">
        <Field
          label="거래처명"
          required
          value={draft.partner_name}
          onChange={(v) => setDraft({ ...draft, partner_name: v })}
          placeholder="예: 안성 김치제조공장"
        />
        <Select
          label="거래처 유형"
          required
          value={draft.partner_type}
          onChange={(v) => setDraft({ ...draft, partner_type: v })}
          options={PARTNER_TYPES}
        />
        <Field
          label="내부 거래처 코드"
          required
          value={draft.partner_id}
          onChange={(v) => setDraft({ ...draft, partner_id: v })}
          placeholder="예: KIMCHI_FACTORY_002"
          hint="시스템이 거래처를 구분하는 코드입니다. 중복될 수 없습니다."
        />
        <Field
          label="고객 분류"
          value={draft.client_type}
          onChange={(v) => setDraft({ ...draft, client_type: v })}
          placeholder="예: 김치제조공장"
        />
        <Field
          label="지역"
          value={draft.factory_region}
          onChange={(v) => setDraft({ ...draft, factory_region: v })}
          placeholder="예: 경기도"
        />
        <Field
          label="시·군"
          value={draft.factory_city}
          onChange={(v) => setDraft({ ...draft, factory_city: v })}
          placeholder="예: 안성시"
        />
        <Field
          label="읍·면"
          value={draft.factory_area}
          onChange={(v) => setDraft({ ...draft, factory_area: v })}
        />
        <Field
          label="결제일수"
          value={draft.sales_collection_days}
          onChange={(v) => setDraft({ ...draft, sales_collection_days: v })}
          placeholder="예: 30"
          hint="비워 두면 «정하지 않음» 으로 저장됩니다."
        />
        <Field
          label="가격계약 유형"
          value={draft.pricing_contract_type}
          onChange={(v) => setDraft({ ...draft, pricing_contract_type: v })}
        />
      </div>
      <Field label="메모" value={draft.note} onChange={(v) => setDraft({ ...draft, note: v })} />
      <label className="mt-3 flex items-center gap-2 text-[15.5px]">
        <input
          type="checkbox"
          checked={draft.active}
          onChange={(event) => setDraft({ ...draft, active: event.target.checked })}
        />
        <span>지금부터 거래 중인 거래처로 둡니다</span>
      </label>

      {problems.length > 0 && (
        <ul
          className="m-0 mt-3 list-none rounded-lg px-3 py-2 text-[16px]"
          style={{ background: "var(--color-t-bad-bg)", color: "var(--color-t-bad)" }}
        >
          {problems.map((problem) => (
            <li key={problem}>{problem}</li>
          ))}
        </ul>
      )}
      {created && (
        <p
          className="mb-0 mt-3 rounded-lg px-3 py-2 text-[16px]"
          style={{ background: "var(--color-t-good-bg)", color: "var(--color-t-good)" }}
        >
          <b>{created.partner_name}</b> 이(가) 등록됐습니다. 여신 한도는 재무에서 별도로
          설정됩니다 — 한도가 정해지기 전까지 이 거래처의 판매는 재무 검증을 통과하지 못합니다.
        </p>
      )}

      <div className="mt-4 flex items-center gap-3">
        <button
          type="button"
          onClick={submit}
          disabled={saving}
          className="rounded-lg border px-3 py-2 text-[16px] disabled:opacity-50"
          style={{ borderColor: "var(--color-t-info)", color: "var(--color-t-info)" }}
        >
          {saving ? "등록 중" : "등록"}
        </button>
        <button
          type="button"
          onClick={() => {
            reset();
            setOpen(false);
          }}
          disabled={saving}
          className="rounded-lg border px-3 py-2 text-[16px] disabled:opacity-50"
          style={{ borderColor: "var(--color-hair)" }}
        >
          닫기
        </button>
        <span className="text-[15.5px] text-ink2">
          여신 한도는 재무에서 별도로 설정됩니다 — 이 화면에서 정하지 않습니다.
        </span>
      </div>
    </Panel>
  );
}

/** 서버 오류를 사용자 문장으로. **원인을 숨기지 않는다.** */
function explain(failure: unknown): string {
  if (!(failure instanceof SalesApiError)) return String(failure);
  if (failure.status === 409) return failure.message;
  if (failure.status === 422) return `입력을 확인해 주세요 — ${failure.message}`;
  if (failure.status === 0) return failure.message;
  return `저장하지 못했습니다 (${failure.status}) — ${failure.message}`;
}

function Field({
  label,
  value,
  onChange,
  required = false,
  placeholder,
  hint,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  required?: boolean;
  placeholder?: string;
  hint?: string;
}) {
  return (
    <label className="mt-2 flex flex-col gap-1 text-[15.5px]">
      <span className="text-ink2">
        {label}
        {required && <span style={{ color: "var(--color-t-bad)" }}> *</span>}
      </span>
      <input
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
        spellCheck={false}
        className="rounded-md border px-2 py-1 text-[16px]"
        style={{ borderColor: "var(--color-hair)" }}
      />
      {hint && <span className="text-[15px] text-ink2">{hint}</span>}
    </label>
  );
}

function Select({
  label,
  value,
  onChange,
  options,
  required = false,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: { value: string; label: string }[];
  required?: boolean;
}) {
  return (
    <label className="mt-2 flex flex-col gap-1 text-[15.5px]">
      <span className="text-ink2">
        {label}
        {required && <span style={{ color: "var(--color-t-bad)" }}> *</span>}
      </span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="rounded-md border px-2 py-1 text-[16px]"
        style={{ borderColor: "var(--color-hair)" }}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}
