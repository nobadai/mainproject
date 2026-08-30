"use client";

import type { Scenario } from "@/lib/types";

/**
 * 승인 확인.
 *
 * 🔴 **발화문에 없는 둘을 화면이 싣는다** — 어느 실행의 안인지(`target_request_id`)와
 * 누가 승인하는지(`decided_by`). 서버가 *"가장 최근 실행"* 으로 추측하면 **엉뚱한 날의
 * 안을 승인**할 수 있어 거절한다.
 *
 * ★ **승인은 기록이지 발주가 아니다.** 그 사실을 확인 화면에 적는다 — 안 적으면
 *   사용자는 발주가 나간 줄 안다.
 */

const won = (n: number) => n.toLocaleString("ko-KR");

export function DecisionModal({
  scenario,
  targetRequestId,
  decidedBy,
  busy,
  error,
  onConfirm,
  onCancel,
}: {
  scenario: Scenario;
  targetRequestId: string;
  decidedBy: string;
  busy: boolean;
  error: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="fixed inset-0 z-50 grid place-items-center p-5">
      <button
        type="button"
        aria-label="닫기"
        onClick={onCancel}
        className="absolute inset-0 cursor-default bg-ink/25"
      />
      <div className="relative w-full max-w-[430px] rounded-xl border border-line bg-surface p-5 shadow-[0_20px_50px_-20px_rgba(21,26,22,.5)]">
        <h4 className="m-0 text-[17px] font-semibold">
          ‘{scenario.label}’ 안으로 진행할까요?
        </h4>
        <p className="m-0 mb-4 mt-1 text-[13.5px] text-muted">
          승인으로 기록됩니다. 되돌리려면 다른 안을 다시 골라야 합니다.
        </p>

        <dl className="m-0 grid grid-cols-[auto_1fr] gap-x-3.5 gap-y-1 text-[13.5px]">
          <dt className="text-muted">대상 실행</dt>
          <dd className="tabular m-0 font-mono">{targetRequestId}</dd>
          <dt className="text-muted">수량</dt>
          <dd className="tabular m-0 font-mono">
            {scenario.total_qty_kg == null ? "—" : `${won(scenario.total_qty_kg)} kg`}
          </dd>
          <dt className="text-muted">금액</dt>
          <dd className="tabular m-0 font-mono">
            {scenario.total_amount_krw == null ? "—" : `${won(scenario.total_amount_krw)} 원`}
          </dd>
          <dt className="text-muted">승인자</dt>
          <dd className="m-0 font-mono">{decidedBy}</dd>
        </dl>

        <div className="mt-3.5 rounded-lg border border-warn/25 bg-warn-wash p-3">
          <p className="m-0 text-[11px] font-semibold uppercase tracking-wide text-warn">
            기록이지 발주가 아닙니다
          </p>
          <p className="m-0 mt-1 text-[13px] text-muted">
            여기서 끝나는 것은 <b className="font-semibold text-ink">사람이 이 안을 골랐다</b>{" "}
            까지이고, 실제 발주는 별도입니다.
          </p>
        </div>

        {error && (
          <p className="mt-3 rounded-lg border border-warn/30 bg-warn-wash p-2.5 text-[13px] text-warn">
            {error}
          </p>
        )}

        <div className="mt-4 flex gap-2">
          <button
            type="button"
            onClick={onCancel}
            className="flex-1 rounded-lg border border-line bg-surface py-2 text-[13.5px] font-semibold text-muted"
          >
            취소
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={busy}
            className="flex-1 rounded-lg border border-accent bg-accent py-2 text-[13.5px] font-semibold text-white disabled:opacity-60"
          >
            {busy ? "기록 중…" : "승인 기록"}
          </button>
        </div>
      </div>
    </div>
  );
}
