"use client";

import { useState } from "react";

import { AGENT_LABEL } from "@/components/LlmTrace";
import { ApiError, runHistory } from "@/lib/api";
import {
  formatKoreanDate,
  formatKoreanDateTime,
  requestLabel,
  scenarioName,
  stepLabel,
  stepResultLabel,
  userErrorText,
} from "@/lib/procurementLabels";
import type { DecisionOut, RunHistory as RunHistoryData } from "@/lib/types";

/**
 * 실행 이력 — *"그 매입 판단이 어떻게 됐나"* 에 **한 번의 호출로** 답한다.
 *
 * ★ 실제 서비스 사용자에게 보이는 화면이다 (2026-09-15 결정 · #681 후속).
 *   내부 코드 · 모드 이름 · 도구 이름 · 상태 코드 · id · 소요 시간은 싣지 않는다.
 *   단계 이름과 결과는 `procurementLabels.ts` 사전 한 곳에서 온다.
 *
 * ★ **번복을 지우지 않는다.** 결정은 회차를 올려 쌓이고 최신 하나만 `is_current` 다.
 *
 * ★ 이번 세션에서 만든 판단을 목록으로 두되, **번호 직접 입력도 받는다** — 어제 판단도
 *   봐야 하기 때문이다.
 */

/**
 * 못 불러왔을 때의 문장. **여기서 문구를 적지 않는다** — 404 만 이 화면이 알고,
 * 나머지는 `userErrorText` 한 곳에서 온다 (2026-09-16).
 *
 * 🔴 전에는 연결 실패 문장이 여기 손으로 복제돼 있었다. 사전 한쪽만 고치면 화면에
 *    따라 다른 말이 나오던 자리다.
 */
function loadErrorText(e: unknown): string {
  const status = e instanceof ApiError ? e.status : null;
  //  이 화면만 아는 것 — 번호를 잘못 적었다.
  if (status === 404) return "그 번호의 매입 판단을 찾지 못했습니다. 번호를 다시 확인해 주세요.";
  return userErrorText(
    status,
    e instanceof Error ? e.message : "",
    "매입 판단을 불러오지 못했습니다. 잠시 뒤 다시 시도해 주세요.",
  );
}

export function RunHistoryPanel({ known }: { known: string[] }) {
  const [id, setId] = useState(known[known.length - 1] ?? "");
  const [data, setData] = useState<RunHistoryData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function load(target: string) {
    const key = target.trim();
    if (!key || busy) return;
    setId(key);
    setBusy(true);
    setError(null);
    try {
      setData(await runHistory(key));
    } catch (e) {
      setData(null);
      setError(loadErrorText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void load(id);
        }}
        className="flex flex-wrap items-center gap-2"
      >
        <label className="flex min-w-[220px] flex-1 items-center gap-2 text-[16.5px] text-muted">
          <span className="shrink-0">매입 판단 번호</span>
          <input
            value={id}
            onChange={(e) => setId(e.target.value)}
            /**
             * 🔴 칸이 **실제 값**으로 차 있다 — 마운트 때 마지막 번호가 들어가고,
             * 아래 칩을 누르면 `load()` 가 `setId(key)` 로 다시 채운다. 그 상태로
             * 칸을 클릭해 다른 번호를 치면 **지워지지 않고 뒤에 붙는다.**
             * 그래서 포커스를 받을 때 전체를 고른다.
             */
            onFocus={(e) => e.currentTarget.select()}
            placeholder="매입 판단 번호를 입력하세요"
            className="min-w-0 flex-1 rounded-lg border border-line bg-surface px-3 py-2 text-[17px] text-ink outline-none focus:border-accent"
          />
        </label>
        <button
          type="submit"
          disabled={busy || !id.trim()}
          className="rounded-lg bg-accent px-4 py-2 text-[17.5px] font-semibold text-white disabled:opacity-45"
        >
          {busy ? "조회 중…" : "조회"}
        </button>
      </form>

      {known.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[15px] text-faint">방금 만든 판단</span>
          {known.map((key, i) => (
            <button
              key={key}
              type="button"
              onClick={() => void load(key)}
              className={`rounded-full border px-2.5 py-0.5 text-[15.5px] ${
                key === data?.request_id
                  ? "border-accent bg-accent-wash text-accent-ink"
                  : "border-line bg-sunk text-muted hover:border-accent"
              }`}
            >
              {requestLabel(key) ?? `${i + 1}번째 판단`}
            </button>
          ))}
        </div>
      )}

      {error && (
        <div className="rounded-lg border border-warn/25 bg-warn-wash p-3 text-[17px] text-warn">
          {error}
        </div>
      )}

      {data && <HistoryView data={data} />}
    </div>
  );
}

export function HistoryView({ data }: { data: RunHistoryData }) {
  const item =
    typeof data.request_payload?.item === "string" && data.request_payload.item
      ? data.request_payload.item
      : null;
  const ranAt = formatKoreanDateTime(data.created_at);

  return (
    <>
      <div className="flex flex-col gap-1 border-b border-line pb-3">
        <h3 className="m-0 text-[19px] font-semibold">
          {requestLabel(data.request_id) ?? "매입 판단"}
        </h3>
        <p className="m-0 text-[16.5px] text-muted">
          기준일 {formatKoreanDate(data.as_of)}
          {item && ` · 품목 ${item}`}
          {ranAt && ` · ${ranAt}에 실행`}
        </p>
        {/* 같은 번호로 다시 돌리면 실행이 쌓이고 서버는 마지막 것을 돌려준다 */}
        <p className="m-0 text-[15.5px] text-faint">
          같은 판단을 여러 번 다시 만들었다면 가장 마지막 결과를 보여 줍니다.
        </p>
      </div>

      <Steps rows={data.plan} />
      <Decisions rows={data.decisions} latestRunId={data.run_id ?? null} />
    </>
  );
}

function Steps({ rows }: { rows: Record<string, unknown>[] }) {
  if (rows.length === 0)
    return (
      <p className="m-0 text-[17px] text-muted">
        이 판단은 부서 확인을 한 단계도 거치지 못했습니다.
      </p>
    );

  return (
    <section>
      <p className="mb-1.5 text-[16px] font-semibold text-muted">
        이 매입 판단이 거친 단계 · {rows.length}단계
      </p>
      <ol className="m-0 flex list-none flex-col gap-1 p-0">
        {rows.map((row, i) => {
          const result = stepResultLabel(row.runtime_status, row.business_status);
          const attention = result !== "통과";
          const dept = typeof row.agent === "string" ? AGENT_LABEL[row.agent] : undefined;
          return (
            <li
              key={i}
              className="flex flex-wrap items-baseline gap-x-2.5 gap-y-0.5 rounded-lg border border-line-soft bg-surface px-3 py-2 text-[17px]"
            >
              <span className="w-5 shrink-0 text-right text-[16px] text-faint">{i + 1}</span>
              {dept && <b className="shrink-0 font-semibold">{dept}</b>}
              <span className="flex-1">{stepLabel(row.agent, row.mode)}</span>
              <span
                className={`rounded px-1.5 py-px text-[15.5px] font-medium ${
                  attention ? "bg-warn-wash text-warn" : "bg-accent-wash text-accent-ink"
                }`}
              >
                {result}
              </span>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

function decisionSentence(row: DecisionOut): string {
  const who = row.decided_by ? `${row.decided_by}님이` : "담당자가";
  const when = formatKoreanDateTime(row.created_at);
  const at = when ? ` ${when}에` : "";
  switch (row.decision) {
    case "APPROVE":
      return row.scenario_label
        ? `${who}${at} ‘${scenarioName(row.scenario_label)}’을 승인했습니다.`
        : `${who}${at} 안을 승인했습니다.`;
    case "REJECT_ALL":
      return `${who}${at} 모든 안을 반려했습니다.`;
    case "REQUEST_CHANGE":
      return `${who}${at} 조건을 바꿔 다시 만들어 달라고 요청했습니다.`;
    default:
      return `${who}${at} 결정을 남겼습니다.`;
  }
}

function Decisions({ rows, latestRunId }: { rows: DecisionOut[]; latestRunId: string | null }) {
  if (rows.length === 0)
    return (
      <p className="m-0 rounded-lg border border-line bg-sunk p-3 text-[17px] text-muted">
        아직 결정이 없습니다. <b className="text-ink">미결정</b>이며 거절된 것이 아닙니다.
      </p>
    );

  return (
    <section>
      <p className="mb-1.5 text-[16px] font-semibold text-muted">결정 기록 · {rows.length}건</p>
      <ol className="m-0 flex list-none flex-col gap-1.5 p-0">
        {rows.map((row) => (
          <li
            key={row.decision_id}
            className={`rounded-lg border p-3 ${
              row.is_current ? "border-accent bg-accent-wash" : "border-line bg-surface opacity-70"
            }`}
          >
            <div className="flex flex-wrap items-baseline gap-x-2.5 gap-y-1">
              <span className="text-[17.5px]">{decisionSentence(row)}</span>
              {row.is_current ? (
                <span className="rounded-full bg-accent px-1.5 py-px text-[14.5px] font-semibold text-white">
                  현재 결정
                </span>
              ) : (
                <span className="text-[15px] text-faint">이전 결정</span>
              )}
            </div>
            {row.condition_text && (
              <p className="m-0 mt-1 text-[16.5px] text-muted">요청한 조건: {row.condition_text}</p>
            )}
            {row.follow_up_request_id && (
              <p className="m-0 mt-1 text-[16px] text-muted">
                이 요청으로 매입안을 다시 만들었습니다
                {requestLabel(row.follow_up_request_id)
                  ? ` (${requestLabel(row.follow_up_request_id)})`
                  : ""}
                .
              </p>
            )}
            {row.history_run_id && latestRunId && row.history_run_id !== latestRunId && (
              <p className="m-0 mt-1 text-[16px] text-warn">
                이 결정은 위에 보이는 결과보다 앞선 결과를 보고 내렸습니다.
              </p>
            )}
          </li>
        ))}
      </ol>
      <p className="m-0 mt-2 text-[15.5px] text-faint">
        결정을 바꾸면 이전 결정은 지우지 않고 함께 남깁니다.
      </p>
    </section>
  );
}
