"use client";

/**
 * 구간별 금액을 한 줄 막대로 보여 준다. (채권 Aging · 채무 만기)
 *
 * 🔴 **구간을 화면이 다시 나누지 않는다.** 백엔드가 준 구간 합계를 그대로 받아
 *    **비율만** 계산해 폭으로 쓴다 — 비율은 표시이지 업무 값이 아니다.
 *
 * ⚠️ **`null` 은 0 이 아니다.** 값이 없는 구간은 막대에서 빼고, 그 사실을 적는다.
 */

import { money } from "@/lib/console_api";
import { toNumber } from "./user_text";

export type AgingSlice = {
  label: string;
  value: string | number | null | undefined;
  color: string;
  hint?: string;
};

export function AgingBars({ slices, empty }: { slices: AgingSlice[]; empty: string }) {
  const known = slices
    .map((slice) => ({ ...slice, amount: toNumber(slice.value) }))
    .filter((slice): slice is AgingSlice & { amount: number } => slice.amount !== null);
  const total = known.reduce((sum, slice) => sum + slice.amount, 0);
  const missing = slices.length - known.length;

  if (known.length === 0 || total <= 0) {
    return (
      <p className="mb-0 mt-1 text-[12px] text-ink2">
        {total === 0 && known.length > 0 ? empty : "구간 금액이 저장되지 않았습니다."}
      </p>
    );
  }

  return (
    <div>
      <div
        className="flex h-3 w-full overflow-hidden rounded-full"
        style={{ background: "var(--color-grid)" }}
        role="img"
        aria-label={known.map((slice) => `${slice.label} ${money(slice.value)}`).join(", ")}
      >
        {known.map((slice) =>
          slice.amount <= 0 ? null : (
            <span
              key={slice.label}
              title={`${slice.label} · ${money(slice.value)}`}
              style={{ width: `${(slice.amount / total) * 100}%`, background: slice.color }}
            />
          ),
        )}
      </div>
      <ul className="m-0 mt-3 grid list-none gap-x-4 gap-y-2 p-0 sm:grid-cols-2">
        {known.map((slice) => (
          <li key={slice.label} className="flex items-center justify-between gap-3 text-[12px]">
            <span className="inline-flex items-center gap-2 text-ink2">
              <span
                aria-hidden
                className="block h-2.5 w-2.5 shrink-0 rounded-full"
                style={{ background: slice.color }}
              />
              {slice.label}
            </span>
            <span className="text-right">
              <b className="tabular-nums">{money(slice.value)}</b>
              <span className="ml-2 text-ink2 tabular-nums">
                {((slice.amount / total) * 100).toFixed(0)}%
              </span>
            </span>
          </li>
        ))}
      </ul>
      {missing > 0 && (
        <p className="mb-0 mt-2 text-[11.5px] text-ink2">
          {missing}개 구간은 금액이 저장되지 않아 막대에서 뺐습니다.
        </p>
      )}
    </div>
  );
}
