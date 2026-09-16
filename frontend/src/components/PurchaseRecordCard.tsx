"use client";

import { useCallback, useEffect, useState, useSyncExternalStore } from "react";

import { ApiError, getPurchaseRecord, postPurchaseRecord } from "@/lib/api";
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
 * ★ **수량 · 금액은 정수만 받는다.** 반 kg · 소수점 원은 장부가 받지 않는다. 보내 놓고
 *   거절당하는 대신 적는 자리에서 알려 준다.
 */

/** 🔴 상태 코드를 화면에 쓰지 않는다 — 사람 말로 옮긴다. */
const STATUS_TEXT: Record<Exclude<PurchaseRecordStatus, "NOT_REQUIRED">, string> = {
  AWAITING_PURCHASE_RECORD: "기록 대기",
  APPLIED: "반영됨",
  NOT_APPLIED: "반영되지 않음",
};

const STATUS_STYLE: Record<Exclude<PurchaseRecordStatus, "NOT_REQUIRED">, string> = {
  AWAITING_PURCHASE_RECORD: "bg-gold-wash text-gold",
  APPLIED: "bg-accent-wash text-accent-ink",
  NOT_APPLIED: "bg-warn-wash text-warn",
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
        <p className="m-0 text-[12.5px] text-warn">{load.text}</p>
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
            <p className="m-0 text-[12.5px] text-muted">
              <b className="text-ink">반영됐습니다.</b> 적은 값으로 매입 원장 · 매입채무 · 입고
              일정이 섰습니다.
            </p>
          ) : (
            data.reason && (
              <p className="m-0 rounded-lg border border-warn/25 bg-warn-wash px-3 py-2 text-[12.5px] text-warn">
                {data.reason}
              </p>
            )
          )}
        </>
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
        <h3 className="m-0 text-base font-semibold text-ink">실매입 기록</h3>
        {status && (
          <span
            className={`rounded px-2 py-0.5 text-[11.5px] font-medium ${STATUS_STYLE[status]}`}
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
  amount_krw: string;
  purchase_date: string;
  arrival_date: string;
}

const toDraft = (leg: PurchaseRecordLeg): LegDraft => ({
  seq: leg.seq,
  qty_kg: String(leg.qty_kg),
  // 선정안에 금액이 없으면 비워 둔다 — 0 으로 채우지 않는다.
  amount_krw: leg.amount_krw == null ? "" : String(leg.amount_krw),
  purchase_date: leg.purchase_date,
  arrival_date: leg.arrival_date,
});

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
        Number(leg.amount_krw) > 0 &&
        leg.purchase_date !== "" &&
        leg.arrival_date !== "",
    );

  // ★ 장부의 수량은 kg, 금액은 원이고 둘 다 정수다. **보내기 전에** 알려 준다.
  const 정수 = (text: string) => Number.isInteger(Number(text));
  const 소수회차 = legs
    .filter((leg) => !정수(leg.qty_kg) || !정수(leg.amount_krw))
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
        legs: legs.map((leg) => ({
          seq: leg.seq,
          qty_kg: Number(leg.qty_kg),
          amount_krw: Number(leg.amount_krw),
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
    `w-full rounded-md border border-line bg-surface px-2 py-1 font-mono text-[12.5px] tabular-nums ${
      isChanged ? "font-semibold text-gold" : "text-ink"
    }`;

  return (
    <>
      <p className="m-0 text-[12.5px] text-muted">
        실제로 산 값을 적어 주세요. 선정안 값을 미리 채워 두었습니다. 적는 순간 그 값으로 매입
        원장 · 매입채무 · 입고 일정이 섭니다.
      </p>
      <p className="m-0 text-[11.5px] text-faint">
        매입일은 승인한 안에 적힌 날 그대로라 여기서 바꿀 수 없습니다. 도착일은 실제로 들어온 날로
        고쳐 주세요. 수량은 1kg, 금액은 1원 단위로 적습니다.
      </p>

      <div className="overflow-x-auto rounded-lg border border-line bg-surface">
        <table className="w-full min-w-[560px] border-collapse text-[12.5px]">
          <thead>
            <tr className="bg-sunk text-[10.5px] uppercase tracking-wide text-muted">
              {["회차", "수량(kg)", "금액(원)", "매입일", "도착일"].map((h) => (
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
                      min={0}
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
                      min={0}
                      step={1}
                      inputMode="numeric"
                      aria-label={`${leg.seq}회차 금액`}
                      value={leg.amount_krw}
                      onChange={(e) => update(index, { amount_krw: e.target.value })}
                      className={inputClass(changed(leg.amount_krw, plan?.amount_krw))}
                    />
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
        <label className="flex flex-col gap-1 text-[11.5px]">
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
          className="rounded-lg bg-accent px-4 py-1.5 text-[13px] font-semibold text-white disabled:opacity-45"
        >
          {busy ? "기록하는 중" : "실매입 기록"}
        </button>
        <span className="text-[11.5px] text-faint">
          {session ? `${session.name}님 이름으로 기록합니다` : "로그인 정보를 읽지 못해 기록할 수 없습니다"}
          {" · "}
          <b className="font-semibold text-gold">굵게</b> 표시한 칸은 선정안과 다릅니다
        </span>
      </div>

      {소수회차.length > 0 && (
        <p className="m-0 rounded-lg border border-warn/25 bg-warn-wash px-3 py-2 text-[12.5px] text-warn">
          {소수회차.join(" · ")}회차의 수량과 금액에 소수점이 있습니다. 수량은 1kg, 금액은 1원
          단위로 적어 주세요.
        </p>
      )}

      {error && (
        <p className="m-0 rounded-lg border border-warn/25 bg-warn-wash px-3 py-2 text-[12.5px] text-warn">
          {error}
        </p>
      )}
    </>
  );
}

/* ── 반영됨 · 반영되지 않음 · 기록값 표 ─────────────────────────────────── */

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
        <table className="w-full min-w-[480px] border-collapse text-[12.5px]">
          <thead>
            <tr className="bg-sunk text-[10.5px] uppercase tracking-wide text-muted">
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
      <p className="m-0 text-[11.5px] text-faint">
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
