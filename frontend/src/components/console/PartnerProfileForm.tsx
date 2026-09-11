"use client";

/**
 * 거래처 기본정보 수정.
 *
 * 🔴 **여신 한도를 여기서 고치지 않는다.** 정본은 재무의 `partner_credit_limits` 이고,
 *    거래처 기본정보에 같은 이름의 칸을 두면 두 곳이 다른 한도를 말하는 날이 온다.
 *
 * 🔴 **저장은 화면에 남기지 않는다.** `localStorage` 에 담아 두면 새로고침 한 번에
 *    사라지거나, 더 나쁘게는 장부와 다른 값이 화면에만 남는다. 저장 뒤에는 **다시
 *    읽어** 장부가 가진 값을 보여 준다.
 */

import { useEffect, useState } from "react";

import { Panel } from "@/components/console/Blocks";
import { Failed, Skeleton } from "@/components/console/ConsoleData";
import {
  ApiError,
  partnerProfile,
  savePartnerProfile,
  type PartnerProfile,
  type PartnerProfileUpdate,
} from "@/lib/api";

type Draft = {
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

function toDraft(profile: PartnerProfile): Draft {
  return {
    partner_name: profile.partner_name,
    partner_type: profile.partner_type,
    client_type: profile.client_type ?? "",
    factory_region: profile.factory_region ?? "",
    factory_city: profile.factory_city ?? "",
    factory_area: profile.factory_area ?? "",
    //  ⚠️ `null` 은 빈 칸으로 보인다. 저장할 때 0 으로 바꾸지 않는다.
    sales_collection_days:
      profile.sales_collection_days === null ? "" : String(profile.sales_collection_days),
    pricing_contract_type: profile.pricing_contract_type ?? "",
    note: profile.note ?? "",
    active: profile.active,
  };
}

/** 바뀐 칸만 고른다. **안 건드린 칸은 보내지 않는다.** */
function changed(draft: Draft, profile: PartnerProfile): PartnerProfileUpdate {
  const update: PartnerProfileUpdate = {};
  const base = toDraft(profile);
  if (draft.partner_name !== base.partner_name) update.partner_name = draft.partner_name;
  if (draft.partner_type !== base.partner_type) update.partner_type = draft.partner_type;
  if (draft.client_type !== base.client_type) update.client_type = draft.client_type || null;
  if (draft.factory_region !== base.factory_region)
    update.factory_region = draft.factory_region || null;
  if (draft.factory_city !== base.factory_city) update.factory_city = draft.factory_city || null;
  if (draft.factory_area !== base.factory_area) update.factory_area = draft.factory_area || null;
  if (draft.pricing_contract_type !== base.pricing_contract_type)
    update.pricing_contract_type = draft.pricing_contract_type || null;
  if (draft.note !== base.note) update.note = draft.note || null;
  if (draft.active !== base.active) update.active = draft.active;
  if (draft.sales_collection_days !== base.sales_collection_days) {
    update.sales_collection_days =
      draft.sales_collection_days.trim() === "" ? null : Number(draft.sales_collection_days);
  }
  return update;
}

export function PartnerProfileForm({ partnerId }: { partnerId: string }) {
  const [profile, setProfile] = useState<PartnerProfile | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  //  ★ 거래처가 바뀌면 **그리는 중에** 상태를 되돌린다. 효과 안에서 되돌리면 옛
  //    거래처의 값이 한 프레임 보인다.
  const [shown, setShown] = useState(partnerId);
  if (shown !== partnerId) {
    setShown(partnerId);
    setProfile(null);
    setDraft(null);
    setError(null);
    setSaved(false);
  }

  useEffect(() => {
    let alive = true;
    partnerProfile(partnerId)
      .then((value) => {
        if (!alive) return;
        setProfile(value);
        setDraft(toDraft(value));
      })
      .catch((failure: unknown) => {
        if (alive) setError(message(failure));
      });
    return () => {
      alive = false;
    };
  }, [partnerId]);

  if (error && !profile) return <Failed what="거래처 기본정보" message={error} />;
  if (!profile || !draft) return <Skeleton what="거래처 기본정보" />;

  const update = changed(draft, profile);
  const dirty = Object.keys(update).length > 0;

  function save() {
    setSaving(true);
    setError(null);
    setSaved(false);
    savePartnerProfile(partnerId, update)
      .then((value) => {
        //  ★ 저장된 행으로 화면을 다시 세운다 — 보낸 값이 아니라 장부의 값이다.
        setProfile(value);
        setDraft(toDraft(value));
        setSaved(true);
      })
      .catch((failure: unknown) => setError(message(failure)))
      .finally(() => setSaving(false));
  }

  return (
    <Panel title="거래처 기본정보" subtitle="스키마가 가진 칸만 고칩니다 — 여신 한도는 재무 정본입니다">
      <div className="grid gap-2 sm:grid-cols-3">
        <Field label="이름" value={draft.partner_name} onChange={(v) => setDraft({ ...draft, partner_name: v })} />
        <Field label="유형" value={draft.partner_type} onChange={(v) => setDraft({ ...draft, partner_type: v })} />
        <Field label="고객 분류" value={draft.client_type} onChange={(v) => setDraft({ ...draft, client_type: v })} />
        <Field label="지역" value={draft.factory_region} onChange={(v) => setDraft({ ...draft, factory_region: v })} />
        <Field label="시·군" value={draft.factory_city} onChange={(v) => setDraft({ ...draft, factory_city: v })} />
        <Field label="읍·면" value={draft.factory_area} onChange={(v) => setDraft({ ...draft, factory_area: v })} />
        <Field
          label="결제일수"
          value={draft.sales_collection_days}
          onChange={(v) => setDraft({ ...draft, sales_collection_days: v })}
        />
        <Field
          label="가격 계약"
          value={draft.pricing_contract_type}
          onChange={(v) => setDraft({ ...draft, pricing_contract_type: v })}
        />
        <label className="flex items-end gap-2 text-[11.5px]">
          <input
            type="checkbox"
            checked={draft.active}
            onChange={(event) => setDraft({ ...draft, active: event.target.checked })}
          />
          <span>거래 중</span>
        </label>
      </div>
      <Field label="메모" value={draft.note} onChange={(v) => setDraft({ ...draft, note: v })} />
      <div className="mt-3 flex items-center gap-3">
        <button
          onClick={save}
          disabled={!dirty || saving}
          className="rounded-lg border px-3 py-2 text-[12px] disabled:opacity-50"
          style={{ borderColor: "var(--color-hair)" }}
        >
          {saving ? "저장 중" : "저장"}
        </button>
        <span className="text-[11.5px] text-ink2">
          {dirty ? `${Object.keys(update).length}개 칸이 바뀌었습니다` : "바뀐 칸이 없습니다"}
          {saved && !dirty && " · 저장됐습니다"}
        </span>
      </div>
      {error && <p className="mb-0 mt-2 text-[11.5px] text-ink2">{error}</p>}
      <p className="mb-0 mt-2 text-[11px] text-ink2">
        담당자·전화·이메일 칸은 `partners` 에 없습니다 — 마이그레이션이 필요합니다. 여신 한도는{" "}
        {profile.credit_source} 가 답합니다.
      </p>
    </Panel>
  );
}

function message(failure: unknown): string {
  return failure instanceof ApiError
    ? `[${failure.status || "연결 실패"}] ${failure.message}`
    : String(failure);
}

function Field({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="mt-2 flex flex-col gap-1 text-[11.5px]">
      <span className="text-ink2">{label}</span>
      <input
        value={value}
        onChange={(event) => onChange(event.target.value)}
        spellCheck={false}
        className="rounded-md border px-2 py-1 text-[12px]"
        style={{ borderColor: "var(--color-hair)" }}
      />
    </label>
  );
}
