"use client";

import { useCallback, useEffect, useState, useSyncExternalStore } from "react";

import {
  ApiError,
  getPurchaseRecord,
  postPurchaseRecord,
  requestPlanChange,
} from "@/lib/api";
import { formatKoreanDateTime, userErrorText } from "@/lib/procurementLabels";
import { serverSnapshot, sessionSnapshot, subscribeSession } from "@/lib/session";
import type { PurchaseRecordLeg, PurchaseRecordOut, PurchaseRecordStatus } from "@/lib/types";

/**
 * 「실매입 기록」 카드 — 승인한 안(`ApprovedPlan`) 바로 아래 (설계 260915 안 A §5).
 *
 * ★ 사람이 안을 고르면 선정만 기록된다. **실제로 산 값을 여기서 적는 순간** 그 값으로
 *   매입 원장 · 매입채무 · 입고 일정이 선다.
 *
 * ★ **상태는 서버가 정한다.** 화면은 `GET …/purchase-record` 를 읽어 그리고, 적은 뒤에도
 *   응답을 믿지 않고 다시 읽는다 — 장부의 값이 화면의 값이다.
 *
 * ★ 회차 수와 회차 번호는 선정안 그대로다. 사람은 값만 고친다 (추가 · 삭제 없음).
 *
 * ★ 기록자는 로그인 이름이다. 화면에 입력칸으로 두지 않는다.
 *
 * ★ **매입일은 보여만 준다** (2026-09-16). 승인한 안에 적힌 날이 그대로 장부의 날이고,
 *   다른 날로 적으면 서버가 되돌린다 — 되돌려받기 전에 화면이 먼저 말한다.
 *
 * ★ **사람은 금액이 아니라 단가(원/kg)를 적는다** (2026-09-16). 금액은 수량 × 단가로
 *   여기서 바로 서고 읽기 전용으로 보여만 준다 — 매입 원장의 단가 칸이 정수여야 해서,
 *   금액을 받아 나누면 소수 단가가 나고 장부가 그 자리에서 멈춘다.
 *
 * ★ **수량 · 단가는 정수만 받는다.** 반 kg · 소수점 원은 장부가 받지 않는다. 보내 놓고
 *   거절당하는 대신 적는 자리에서 알려 준다.
 */

/** 🔴 상태 코드를 화면에 쓰지 않는다 — 사람 말로 옮긴다. */
const STATUS_TEXT: Record<Exclude<PurchaseRecordStatus, "NOT_REQUIRED">, string> = {
  AWAITING_PURCHASE_RECORD: "기록 대기",
  APPLIED: "반영됨",
  NOT_APPLIED: "입고 처리 중",
};

/**
 * ★ **`NOT_APPLIED` 는 실패가 아니라 진행이다** (2026-09-17). 기록은 남았고 다음 개장
 *   때 원장에 선다 — 경고색으로 칠하면 사람이 *"내가 뭘 잘못 적었나"* 로 읽는다.
 *   그래서 기록 대기와 같은 진행 계열(`gold`)을 쓴다.
 */
const STATUS_STYLE: Record<Exclude<PurchaseRecordStatus, "NOT_REQUIRED">, string> = {
  AWAITING_PURCHASE_RECORD: "bg-gold-wash text-gold",
  APPLIED: "bg-accent-wash text-accent-ink",
  NOT_APPLIED: "bg-gold-wash text-gold",
};

const 원 = (value: number | null | undefined): string =>
  value != null && Number.isFinite(value) ? `${Math.round(value).toLocaleString("ko-KR")}원` : "—";

const kg = (value: number | null | undefined): string =>
  value != null && Number.isFinite(value) ? `${value.toLocaleString("ko-KR")}kg` : "—";

type Load =
  | { kind: "loading" }
  | { kind: "ready"; data: PurchaseRecordOut }
  //   매입 승인이 아니거나 유효한 승인이 없다 — 적을 것이 없으니 카드를 그리지 않는다.
  | { kind: "none" }
  | { kind: "error"; text: string };

export function PurchaseRecordCard({ requestId }: { requestId: string }) {
  const [load, setLoad] = useState<Load>({ kind: "loading" });

  const reload = useCallback(() => {
    return getPurchaseRecord(requestId).then(
      (data) => setLoad({ kind: "ready", data }),
      (error: unknown) => {
        if (error instanceof ApiError && (error.status === 404 || error.status === 409)) {
          setLoad({ kind: "none" });
          return;
        }
        setLoad({
          kind: "error",
          text: userErrorText(
            error instanceof ApiError ? error.status : null,
            error instanceof Error ? error.message : "",
            "실매입 기록 상태를 읽지 못했습니다. 잠시 뒤 다시 시도해 주세요.",
          ),
        });
      },
    );
  }, [requestId]);

  useEffect(() => {
    void reload();
  }, [reload]);

  if (load.kind === "loading" || load.kind === "none") return null;

  if (load.kind === "error")
    return (
      <Shell>
        <p className="m-0 text-[16.5px] text-warn">{load.text}</p>
      </Shell>
    );

  const { data } = load;
  // 자동 승인은 기록 대상이 아니다 — 카드를 보이지 않는다.
  if (data.status === "NOT_REQUIRED") return null;

  return (
    <Shell status={data.status}>
      {data.status === "AWAITING_PURCHASE_RECORD" || !data.record ? (
        // 기록 대기 — 선정안 값을 미리 채운 폼. 승인 회차가 바뀌면 폼을 새로 연다.
        <RecordForm key={data.decision_seq} data={data} onRecorded={reload} />
      ) : (
        <>
          <RecordTable data={data} />
          {data.status === "APPLIED" ? (
            <p className="m-0 text-[16.5px] text-muted">
              <b className="text-ink">반영됐습니다.</b> 적은 값으로 매입 원장 · 매입채무 · 입고
              일정이 섰습니다.
            </p>
          ) : (
            // 원장에 아직 안 섰다는 말이다 — 진행 중이지 실패가 아니라 경고색을 쓰지 않는다.
            data.reason && (
              <p className="m-0 rounded-lg border border-gold/25 bg-gold-wash px-3 py-2 text-[16.5px] text-gold">
                {data.reason}
              </p>
            )
          )}
        </>
      )}
      {/*
        🔴 **이미 원장에 선 것(`APPLIED`)에는 안 보인다.** 실린 것을 되돌리려면 취소
           경로가 따로 있어야 하고, 이 버튼은 그것을 하지 못한다 — 못 하는 일을
           할 수 있는 것처럼 보이는 버튼이 제일 나쁘다.
      */}
      {(data.status === "AWAITING_PURCHASE_RECORD" || data.status === "NOT_APPLIED") && (
        <ChangeRequest requestId={data.request_id} onRequested={reload} />
      )}
    </Shell>
  );
}

function Shell({
  status,
  children,
}: {
  status?: Exclude<PurchaseRecordStatus, "NOT_REQUIRED">;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-3 rounded-xl border border-line bg-surface p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="m-0 text-[20px] font-semibold text-ink">실매입 기록</h3>
        {status && (
          <span
            className={`rounded px-2 py-0.5 text-[15.5px] font-medium ${STATUS_STYLE[status]}`}
          >
            {STATUS_TEXT[status]}
          </span>
        )}
      </div>
      {children}
    </div>
  );
}

/* ── 기록 대기 · 폼 ─────────────────────────────────────────────────────── */

interface LegDraft {
  seq: number;
  qty_kg: string;
  unit_price_krw: string;
  purchase_date: string;
  arrival_date: string;
}

const toDraft = (leg: PurchaseRecordLeg): LegDraft => ({
  seq: leg.seq,
  qty_kg: String(leg.qty_kg),
  // 선정안에 단가가 없으면 비워 둔다 — 0 으로도, 금액 ÷ 수량으로도 채우지 않는다.
  unit_price_krw: leg.unit_price_krw == null ? "" : String(leg.unit_price_krw),
  purchase_date: leg.purchase_date,
  arrival_date: leg.arrival_date,
});

/** 금액은 **수량 × 단가로 난다** — 적는 칸이 아니다. 둘 중 하나가 비면 보여 줄 금액도 없다. */
function legAmount(leg: LegDraft): number | null {
  const qty = Number(leg.qty_kg);
  const unit = Number(leg.unit_price_krw);
  if (leg.qty_kg === "" || leg.unit_price_krw === "") return null;
  if (!Number.isFinite(qty) || !Number.isFinite(unit)) return null;
  return qty * unit;
}

/**
 * 서버 사유를 한 줄로.
 *
 * ★ 본문 검사(도착일이 매입일보다 앞섬 등)는 목록으로 오므로 문장만 뽑는다.
 * ★ 판정 코드 괄호(`(REJECTED)` 같은 것)는 떼고, 코드가 남으면 사람 말로 바꾼다.
 */
function recordErrorText(error: unknown): string {
  const status = error instanceof ApiError ? error.status : null;
  let message = error instanceof Error ? error.message : "";
  try {
    const parsed = JSON.parse(message) as unknown;
    if (Array.isArray(parsed)) {
      message = parsed
        .map((item) => String((item as { msg?: unknown })?.msg ?? ""))
        .map((msg) => msg.replace(/^Value error,\s*/, ""))
        .filter(Boolean)
        .join(" · ");
    }
  } catch {
    /* 문장 그대로 */
  }
  message = message.replace(/\s*\([A-Z_]+\)/g, "");
  return userErrorText(status, message, "기록하지 못했습니다. 적은 값을 확인해 주세요.");
}

function RecordForm({
  data,
  onRecorded,
}: {
  data: PurchaseRecordOut;
  onRecorded: () => Promise<void>;
}) {
  const session = useSyncExternalStore(subscribeSession, sessionSnapshot, serverSnapshot);
  const [legs, setLegs] = useState<LegDraft[]>(() => data.plan.legs.map(toDraft));
  const [grade, setGrade] = useState<string>(data.plan.grade ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const planBySeq = new Map(data.plan.legs.map((leg) => [leg.seq, leg]));

  const filled =
    grade.trim() !== "" &&
    legs.every(
      (leg) =>
        Number(leg.qty_kg) > 0 &&
        Number(leg.unit_price_krw) > 0 &&
        leg.purchase_date !== "" &&
        leg.arrival_date !== "",
    );

  // ★ 장부의 수량은 kg, 단가는 원/kg 이고 둘 다 정수다. **보내기 전에** 알려 준다.
  const 정수 = (text: string) => Number.isInteger(Number(text));
  const 소수회차 = legs
    .filter((leg) => !정수(leg.qty_kg) || !정수(leg.unit_price_krw))
    .map((leg) => leg.seq);

  function update(index: number, patch: Partial<LegDraft>) {
    setLegs((prev) => prev.map((leg, i) => (i === index ? { ...leg, ...patch } : leg)));
  }

  async function submit() {
    if (!session || !filled) return;
    setBusy(true);
    setError(null);
    try {
      const result = await postPurchaseRecord(data.request_id, {
        decision_seq: data.decision_seq,
        grade: grade.trim(),
        recorded_by: session.name,
        // 🔴 금액을 같이 보내지 않는다 — 서버가 수량 × 단가로 만든다. 주인은 하나다.
        legs: legs.map((leg) => ({
          seq: leg.seq,
          qty_kg: Number(leg.qty_kg),
          unit_price_krw: Number(leg.unit_price_krw),
          purchase_date: leg.purchase_date,
          arrival_date: leg.arrival_date,
        })),
      });
      if (result.status === "FAILED")
        setError(
          userErrorText(400, result.reason, "기록은 받았지만 장부에 반영하지 못했습니다."),
        );
      await onRecorded();
    } catch (failure) {
      setError(recordErrorText(failure));
    } finally {
      setBusy(false);
    }
  }

  const changed = (a: string, b: string | number | null | undefined) =>
    b == null ? a !== "" : a !== String(b);

  const inputClass = (isChanged: boolean) =>
    `w-full rounded-md border border-line bg-surface px-2 py-1 font-mono text-[16.5px] tabular-nums ${
      isChanged ? "font-semibold text-gold" : "text-ink"
    }`;

  return (
    <>
      <p className="m-0 text-[16.5px] text-muted">
        실제로 산 값을 적어 주세요. 선정안 값을 미리 채워 두었습니다. 적는 순간 그 값으로 매입
        원장 · 매입채무 · 입고 일정이 섭니다.
      </p>
      <p className="m-0 text-[15.5px] text-faint">
        매입일은 승인한 안에 적힌 날 그대로라 여기서 바꿀 수 없습니다. 도착일은 실제로 들어온 날로
        고쳐 주세요. 수량은 1kg, 단가는 1원 단위로 적습니다. 금액은 수량 × 단가로 자동으로 섭니다.
      </p>

      <div className="overflow-x-auto rounded-lg border border-line bg-surface">
        <table className="w-full min-w-[660px] border-collapse text-[16.5px]">
          <thead>
            <tr className="bg-sunk text-[14.5px] uppercase tracking-wide text-muted">
              {["회차", "수량(kg)", "단가(원/kg)", "금액(원)", "매입일", "도착일"].map((h) => (
                <th key={h} className="px-3 py-1.5 text-left font-semibold">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {legs.map((leg, index) => {
              const plan = planBySeq.get(leg.seq);
              return (
                <tr key={leg.seq} className="border-t border-line-soft">
                  <td className="px-3 py-1.5 font-mono tabular-nums">{leg.seq}</td>
                  <td className="px-2 py-1">
                    <input
                      type="number"
                      min={1}
                      step={1}
                      inputMode="numeric"
                      aria-label={`${leg.seq}회차 수량`}
                      value={leg.qty_kg}
                      onChange={(e) => update(index, { qty_kg: e.target.value })}
                      className={inputClass(changed(leg.qty_kg, plan?.qty_kg))}
                    />
                  </td>
                  <td className="px-2 py-1">
                    <input
                      type="number"
                      min={1}
                      step={1}
                      inputMode="numeric"
                      aria-label={`${leg.seq}회차 단가`}
                      value={leg.unit_price_krw}
                      onChange={(e) => update(index, { unit_price_krw: e.target.value })}
                      className={inputClass(changed(leg.unit_price_krw, plan?.unit_price_krw))}
                    />
                  </td>
                  {/* 🔴 금액은 수량 × 단가로 난 값이다 — 사람이 고치는 칸이 아니다. */}
                  <td className="px-3 py-1.5 font-mono tabular-nums text-muted">
                    {원(legAmount(leg))}
                  </td>
                  {/* 매입일은 승인한 안에 적힌 날 그대로다 — 보여만 준다. */}
                  <td className="px-3 py-1.5 font-mono tabular-nums text-muted">
                    {leg.purchase_date}
                  </td>
                  <td className="px-2 py-1">
                    <input
                      type="date"
                      aria-label={`${leg.seq}회차 도착일`}
                      value={leg.arrival_date}
                      min={leg.purchase_date || undefined}
                      onChange={(e) => update(index, { arrival_date: e.target.value })}
                      className={inputClass(changed(leg.arrival_date, plan?.arrival_date))}
                    />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-[15.5px]">
          <span className="text-muted">등급</span>
          <input
            value={grade}
            onChange={(e) => setGrade(e.target.value)}
            spellCheck={false}
            className={`w-32 ${inputClass(changed(grade, data.plan.grade))}`}
          />
        </label>
        <button
          type="button"
          onClick={() => void submit()}
          disabled={busy || !filled || 소수회차.length > 0 || !session}
          className="rounded-lg bg-accent px-4 py-1.5 text-[17px] font-semibold text-white disabled:opacity-45"
        >
          {busy ? "기록하는 중" : "실매입 기록"}
        </button>
        <span className="text-[15.5px] text-faint">
          {session ? `${session.name}님 이름으로 기록합니다` : "로그인 정보를 읽지 못해 기록할 수 없습니다"}
          {" · "}
          <b className="font-semibold text-gold">굵게</b> 표시한 칸은 선정안과 다릅니다
        </span>
      </div>

      {소수회차.length > 0 && (
        <p className="m-0 rounded-lg border border-warn/25 bg-warn-wash px-3 py-2 text-[16.5px] text-warn">
          {소수회차.join(" · ")}회차의 수량과 단가에 소수점이 있습니다. 수량은 1kg, 단가는 1원
          단위로 적어 주세요.
        </p>
      )}

      {error && (
        <p className="m-0 rounded-lg border border-warn/25 bg-warn-wash px-3 py-2 text-[16.5px] text-warn">
          {error}
        </p>
      )}
    </>
  );
}

/* ── 반영됨 · 입고 처리 중 · 기록값 표 ───────────────────────────────────── */

function RecordTable({ data }: { data: PurchaseRecordOut }) {
  const record = data.record;
  if (!record) return null;
  const planBySeq = new Map(data.plan.legs.map((leg) => [leg.seq, leg]));
  const gradeChanged = data.plan.grade != null && record.grade !== data.plan.grade;
  const cell = (text: string, isChanged: boolean) => (
    <td className={`px-3 py-1.5 ${isChanged ? "font-semibold text-gold" : ""}`}>{text}</td>
  );

  return (
    <>
      <div className="overflow-x-auto rounded-lg border border-line bg-surface">
        <table className="w-full min-w-[480px] border-collapse text-[16.5px]">
          <thead>
            <tr className="bg-sunk text-[14.5px] uppercase tracking-wide text-muted">
              {["회차", "수량", "금액", "매입일", "도착일"].map((h) => (
                <th key={h} className="px-3 py-1.5 text-left font-semibold">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="font-mono tabular-nums">
            {record.legs.map((leg) => {
              const plan = planBySeq.get(leg.seq);
              return (
                <tr key={leg.seq} className="border-t border-line-soft">
                  <td className="px-3 py-1.5">{leg.seq}</td>
                  {cell(kg(leg.qty_kg), plan != null && leg.qty_kg !== plan.qty_kg)}
                  {cell(원(leg.amount_krw), plan != null && leg.amount_krw !== plan.amount_krw)}
                  {cell(leg.purchase_date, plan != null && leg.purchase_date !== plan.purchase_date)}
                  {cell(leg.arrival_date, plan != null && leg.arrival_date !== plan.arrival_date)}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="m-0 text-[15.5px] text-faint">
        등급 <span className={gradeChanged ? "font-semibold text-gold" : "text-ink"}>{record.grade}</span>
        {" · "}
        {record.recorded_by}님 기록
        {formatKoreanDateTime(record.recorded_at) && ` · ${formatKoreanDateTime(record.recorded_at)}`}
        {" · "}
        <b className="font-semibold text-gold">굵게</b> 표시한 칸은 선정안과 다릅니다
      </p>
    </>
  );
}

/* ── 승인 되돌리기 ───────────────────────────────────────────────────────── */

/**
 * 잘못 고른 안 · 잘못 적은 값을 **사람이 화면에서 되돌린다** (2026-09-17).
 *
 * ★ 백엔드에는 이미 있던 길이다 — 승인과 같은 `/decision` 에 조건을 붙인 재요청을
 *   한 회차 더 적는다. 결정은 지워지지 않고 **최신 회차가 승인이 아니게 되는 것**으로
 *   접힌다. 그러면 그 승인이 만든 실매입 기록은 다음 개장 때 원장에 서지 않는다.
 *
 * ★ **되돌린 사람은 승인한 사람과 같은 자리에서 온다** — 로그인 이름이다. 새로 짓거나
 *   입력칸으로 받으면 승인 이력에 두 종류의 이름이 섞인다.
 *
 * ★ **확인을 한 번 더 받는다.** 장부를 바꾸는 요청이라 화면이 그렇게 약속했다.
 */
function ChangeRequest({
  requestId,
  onRequested,
}: {
  requestId: string;
  onRequested: () => Promise<void>;
}) {
  const session = useSyncExternalStore(subscribeSession, sessionSnapshot, serverSnapshot);
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function close() {
    setOpen(false);
    setConfirming(false);
    setReason("");
    setError(null);
  }

  // 🔴 조건이 비면 **보내지 않는다.** 서버도 거절하지만, 거절을 받고 나서 알려 주는
  //    것과 적는 자리에서 알려 주는 것은 다른 일이다.
  function ask() {
    if (reason.trim() === "") {
      setError("무엇을 바꿔야 하는지 한 줄이라도 적어 주세요.");
      return;
    }
    setError(null);
    setConfirming(true);
  }

  async function send() {
    if (!session) return;
    setBusy(true);
    setError(null);
    try {
      await requestPlanChange({
        requestId,
        conditionText: reason.trim(),
        decidedBy: session.name,
      });
      close();
      // 상태의 주인은 서버다 — 응답을 믿지 않고 카드를 다시 읽는다.
      await onRequested();
    } catch (failure) {
      // ★ 서버가 준 사유를 **그대로** 올린다. 덮으면 무엇을 고쳐야 하는지가 사라진다.
      setError(serverReasonText(failure));
      setConfirming(false);
    } finally {
      setBusy(false);
    }
  }

  if (!open)
    return (
      <div className="flex flex-wrap items-center gap-2 border-t border-line-soft pt-3">
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="rounded-lg border border-line bg-surface px-3 py-1.5 text-[16.5px] font-semibold text-muted"
        >
          승인 되돌리기
        </button>
        <span className="text-[15.5px] text-faint">
          안을 잘못 골랐거나 값을 잘못 적었으면 여기서 되돌리고 다시 고를 수 있습니다.
        </span>
      </div>
    );

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-line bg-sunk p-3">
      <p className="m-0 text-[16.5px] text-ink">
        <b className="font-semibold">이 승인을 되돌립니다.</b> 되돌리면 적은 값은 매입 원장에
        서지 않고, 안을 다시 골라야 합니다.
      </p>
      <label className="flex flex-col gap-1 text-[15.5px]">
        <span className="text-muted">무엇을 바꿔야 하나요</span>
        <textarea
          rows={2}
          value={reason}
          onChange={(e) => {
            setReason(e.target.value);
            setConfirming(false);
          }}
          placeholder="예: 단가를 잘못 적었습니다. 12,000원이 아니라 1,200원입니다."
          className="w-full resize-y rounded-md border border-line bg-surface px-2 py-1.5 text-[16.5px] text-ink"
        />
      </label>

      {confirming ? (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[16.5px] text-ink">정말 되돌릴까요?</span>
          <button
            type="button"
            onClick={() => void send()}
            disabled={busy || !session}
            className="rounded-lg bg-warn px-3 py-1.5 text-[16.5px] font-semibold text-white disabled:opacity-45"
          >
            {busy ? "되돌리는 중" : "네, 되돌립니다"}
          </button>
          <button
            type="button"
            onClick={() => setConfirming(false)}
            disabled={busy}
            className="rounded-lg border border-line bg-surface px-3 py-1.5 text-[16.5px] font-semibold text-muted"
          >
            아니요
          </button>
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={ask}
            disabled={!session}
            className="rounded-lg border border-warn bg-surface px-3 py-1.5 text-[16.5px] font-semibold text-warn disabled:opacity-45"
          >
            되돌리기
          </button>
          <button
            type="button"
            onClick={close}
            className="rounded-lg border border-line bg-surface px-3 py-1.5 text-[16.5px] font-semibold text-muted"
          >
            그만두기
          </button>
          <span className="text-[15.5px] text-faint">
            {session
              ? `${session.name}님 이름으로 기록합니다`
              : "로그인 정보를 읽지 못해 되돌릴 수 없습니다"}
          </span>
        </div>
      )}

      {error && (
        <p className="m-0 rounded-lg border border-warn/25 bg-warn-wash px-3 py-2 text-[16.5px] text-warn">
          {error}
        </p>
      )}
    </div>
  );
}

/**
 * 서버가 준 사유를 한 줄로 **그대로** 옮긴다.
 *
 * ★ `recordErrorText` 와 다르다 — 저쪽은 사람 말이 아니면 일반 문장으로 덮지만,
 *   되돌리기는 막힌 이유(*"이미 다른 결정이 있다"* 같은 것)가 그 자리에서 보여야
 *   사람이 다음 수를 고른다.
 * ★ 본문 검사는 목록으로 오므로 문장만 뽑는다.
 */
function serverReasonText(error: unknown): string {
  let message = error instanceof Error ? error.message : "";
  try {
    const parsed = JSON.parse(message) as unknown;
    if (Array.isArray(parsed)) {
      message = parsed
        .map((item) => String((item as { msg?: unknown })?.msg ?? ""))
        .map((msg) => msg.replace(/^Value error,\s*/, ""))
        .filter(Boolean)
        .join(" · ");
    }
  } catch {
    /* 문장 그대로 */
  }
  return message.trim() === "" ? "되돌리지 못했습니다." : message;
}
