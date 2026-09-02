"use client";

import { useState } from "react";

import { ApiError, runHistory } from "@/lib/api";
import type { DecisionOut, RunHistory as RunHistoryData } from "@/lib/types";

/**
 * 실행 이력 — *"그 요청이 어떻게 됐나"* 에 **한 번의 호출로** 답한다.
 *
 * ★ **번복을 지우지 않는다.** 결정은 회차를 올려 쌓이고 최신 하나만 `is_current` 다.
 *   지난 결정을 감추면 *"무엇을 골랐다가 무엇으로 바꿨나"* 를 나중에 답할 수 없다.
 *
 * ★ 이번 세션에서 만든 업무 키를 목록으로 두되, **직접 입력도 받는다** — 어제 실행도
 *   봐야 하기 때문이다.
 */

const DECISION_LABEL: Record<string, string> = {
  APPROVE: "승인",
  REJECT_ALL: "전체 반려",
  REQUEST_CHANGE: "조건부 재요청",
};

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
      // 🔴 서버 문장을 덮지 않는다 — "실행이력을 찾을 수 없습니다: …" 가 답이다
      setError(e instanceof ApiError ? `[${e.status}] ${e.message}` : String(e));
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
        <input
          value={id}
          onChange={(e) => setId(e.target.value)}
          /**
           * 🔴 칸이 **실제 값**으로 차 있다 — 마운트 때 마지막 업무 키가 들어가고,
           * 아래 세션 칩을 누르면 `load()` 가 `setId(key)` 로 다시 채운다. 그 상태로
           * 칸을 클릭해 다른 키를 치면 **지워지지 않고 뒤에 붙는다.**
           *
           *   REQ-20251231-0001 + REQ-NOPE-9999 → REQ-20251231-0001REQ-NOPE-9999
           *
           * 그래서 포커스를 받을 때 전체를 고른다. 로그인 칸은 아예 안 채우는 것
           * (placeholder) 으로 풀었지만, 여기서는 채워 두는 것이 맞다 — 방금 본
           * 업무 키를 다시 타이핑하게 만들 이유가 없다.
           *
           * 이미 포커스된 칸을 다시 클릭하면 focus 가 안 나서 select 도 안 걸리는데,
           * 그건 커서를 옮기려는 것이므로 **그대로 두는 것이 맞다.**
           */
          onFocus={(e) => e.currentTarget.select()}
          placeholder="REQ-20251231-0001"
          className="min-w-[220px] flex-1 rounded-lg border border-line bg-surface px-3 py-2 font-mono text-[13px] outline-none focus:border-accent"
        />
        <button
          type="submit"
          disabled={busy || !id.trim()}
          className="rounded-lg bg-accent px-4 py-2 text-[13.5px] font-semibold text-white disabled:opacity-45"
        >
          {busy ? "조회 중…" : "조회"}
        </button>
      </form>

      {known.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[11px] text-faint">이번 세션</span>
          {known.map((key) => (
            <button
              key={key}
              type="button"
              onClick={() => void load(key)}
              className={`rounded-full border px-2.5 py-0.5 font-mono text-[11.5px] ${
                key === data?.request_id
                  ? "border-accent bg-accent-wash text-accent-ink"
                  : "border-line bg-sunk text-muted hover:border-accent"
              }`}
            >
              {key}
            </button>
          ))}
        </div>
      )}

      {error && (
        <div className="rounded-lg border border-warn/25 bg-warn-wash p-3 text-[13px] text-warn">
          {error}
        </div>
      )}

      {data && (
        <>
          <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 border-b border-line pb-3">
            <h3 className="m-0 font-mono text-[15px] font-semibold">{data.request_id}</h3>
            <span className="font-mono text-[11.5px] text-faint">
              {data.cycle} · {data.runtime_status} · 기준일 {data.as_of}
            </span>
            {/* 🔴 같은 업무 키로 다시 돌리면 실행 행이 쌓인다 — 실측에서 한 키에 74행이
                나왔다 (2026-08-30). 서버는 **마지막 하나**를 돌려주는데, 화면이 그 말을
                안 하면 "이 키에 실행이 하나뿐"으로 읽힌다. 어느 실행인지 시각으로 못박는다. */}
            <span className="w-full font-mono text-[11px] text-muted">
              아래 호출 순서는 이 업무 키의 <b className="text-ink">마지막 실행</b>입니다 —{" "}
              {data.created_at.slice(0, 16).replace("T", " ")}
              {data.elapsed_ms != null && ` · ${data.elapsed_ms}ms`}
              . 같은 키로 여러 번 돌렸다면 그 전 실행은 여기 안 나옵니다.
            </span>
          </div>

          <PlanTable rows={data.plan} />
          <Decisions rows={data.decisions} latestRunId={data.run_id ?? null} />
        </>
      )}
    </div>
  );
}

function PlanTable({ rows }: { rows: Record<string, unknown>[] }) {
  if (rows.length === 0)
    return (
      <p className="m-0 text-[13px] text-muted">
        실행 계획이 비어 있습니다 — 부서를 부르지 못한 실행입니다.
      </p>
    );

  return (
    <section>
      <p className="mb-1.5 text-[10.5px] font-semibold uppercase tracking-[0.12em] text-faint">
        호출 순서 · {rows.length}단계
      </p>
      <div className="overflow-x-auto rounded-lg border border-line">
        <table className="w-full min-w-[560px] border-collapse text-[13px]">
          <thead>
            <tr className="bg-sunk text-[11px] uppercase tracking-wide text-muted">
              <th className="px-3 py-2 text-left font-semibold">#</th>
              <th className="px-3 py-2 text-left font-semibold">에이전트</th>
              <th className="px-3 py-2 text-left font-semibold">모드</th>
              <th className="px-3 py-2 text-left font-semibold">결과</th>
              <th className="px-3 py-2 text-left font-semibold">쓴 도구</th>
              {/* 🔴 그 부서가 규칙으로 답했나 모델로 답했나 — 없으면 둘이 같아 보인다 */}
              <th className="px-3 py-2 text-left font-semibold">LLM</th>
              {/*
                🔴 **부서가 스스로 남긴 관측** (2026-09-02 · #166).
                재무가 provider 대체(gemini → ollama · HTTP_429)를 여기 싣는데,
                `llm_fallback_used` 는 계약상 *"규칙이 대신 답함"* 만 뜻해서 False 다.
                모델이 바뀐 사실은 이 칸에만 있다.
              */}
              <th className="px-3 py-2 text-left font-semibold">관측</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => {
              const tools = Array.isArray(row.used_tools) ? (row.used_tools as string[]) : [];
              const missing = Array.isArray(row.missing_data) ? (row.missing_data as string[]) : [];
              const observations = Array.isArray(row.observations)
                ? (row.observations as string[])
                : [];
              const ready = row.runtime_status === "READY";
              return (
                <tr key={i} className="border-t border-line-soft align-top">
                  <td className="px-3 py-2 font-mono text-muted">{String(row.seq ?? i + 1)}</td>
                  <td className="px-3 py-2">{String(row.agent ?? "—")}</td>
                  <td className="px-3 py-2 font-mono text-[12px]">{String(row.mode ?? "—")}</td>
                  <td className={`px-3 py-2 ${ready ? "" : "text-warn"}`}>
                    {String(row.business_status ?? "—")}
                    {!ready && (
                      <span className="ml-1 font-mono text-[11px]">
                        ({String(row.runtime_status)})
                      </span>
                    )}
                    {/* 🔴 못 받은 것을 감추지 않는다 */}
                    {missing.length > 0 && (
                      <span className="mt-0.5 block font-mono text-[11px] text-warn">
                        없음: {missing.join(", ")}
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2 font-mono text-[11.5px] text-muted">
                    {tools.length > 0 ? tools.join(" → ") : "—"}
                  </td>
                  <td className="px-3 py-2 font-mono text-[11.5px]">
                    <span className={row.llm_fallback_used ? "text-warn" : "text-muted"}>
                      {String(row.llm_status ?? "—")}
                    </span>
                    {/* 모델이 죽어 규칙이 대신 답한 것과 애초에 안 쓴 것은 다르다 */}
                    {row.llm_fallback_used ? (
                      <span className="mt-0.5 block text-[11px] text-warn">
                        규칙이 대신 답함
                      </span>
                    ) : null}
                    {row.llm_model ? (
                      <span className="mt-0.5 block text-[11px] text-faint">
                        {String(row.llm_model)}
                      </span>
                    ) : null}
                  </td>
                  {/*
                    ★ **파싱하지 않는다.** 부서마다 모양이 다른 JSON 이라, 화면이 뜻을
                      붙이면 부서 스키마가 여기 한 벌 더 생기고 부서가 필드를 바꾸는 날
                      화면만 옛말을 한다 (`AdvisorVerdicts` 와 같은 이유).
                      건수만 세고 원문 그대로 편다.
                  */}
                  <td className="px-3 py-2 text-[11.5px]">
                    {observations.length === 0 ? (
                      <span className="text-faint">—</span>
                    ) : (
                      <details>
                        <summary className="cursor-pointer text-muted">
                          {observations.length}건
                        </summary>
                        <ul className="m-0 mt-1 list-none space-y-1 p-0">
                          {observations.map((o, j) => (
                            <li
                              key={j}
                              className="break-all font-mono text-[10.5px] text-faint"
                            >
                              {o}
                            </li>
                          ))}
                        </ul>
                      </details>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Decisions({ rows, latestRunId }: { rows: DecisionOut[]; latestRunId: string | null }) {
  if (rows.length === 0)
    return (
      <p className="m-0 rounded-lg border border-line bg-sunk p-3 text-[13px] text-muted">
        아직 결정이 없습니다 — <b className="text-ink">미결정</b>이지 거절이 아닙니다.
      </p>
    );

  return (
    <section>
      <p className="mb-1.5 text-[10.5px] font-semibold uppercase tracking-[0.12em] text-faint">
        결정 · {rows.length}회차
      </p>
      <ol className="m-0 flex list-none flex-col gap-1.5 p-0">
        {rows.map((row) => (
          <li
            key={row.decision_id}
            className={`rounded-lg border p-3 ${
              row.is_current ? "border-accent bg-accent-wash" : "border-line bg-surface opacity-70"
            }`}
          >
            <div className="flex flex-wrap items-baseline gap-x-2.5 gap-y-1">
              <span className="font-mono text-[12px] text-muted">{row.decision_seq}회차</span>
              <b className="text-[14px] font-semibold">
                {DECISION_LABEL[row.decision] ?? row.decision}
                {row.scenario_label && ` · ${row.scenario_label}`}
              </b>
              {row.is_current && (
                <span className="rounded-full bg-accent px-1.5 py-px text-[10.5px] font-semibold text-white">
                  현재
                </span>
              )}
              <span className="ml-auto font-mono text-[11px] text-faint">
                {row.decided_by} · {row.created_at.slice(0, 16).replace("T", " ")}
              </span>
            </div>
            {row.condition_text && (
              <p className="m-0 mt-1 text-[12.5px] text-muted">조건: {row.condition_text}</p>
            )}
            {row.follow_up_request_id && (
              <p className="m-0 mt-1 font-mono text-[11.5px] text-muted">
                → 후속 실행 {row.follow_up_request_id}
              </p>
            )}
            {/* 🔴 **무엇을 보고 결정했나.** 한 업무 키에 실행이 여러 행이라, 이게
                없으면 "그 안" 이 어느 실행의 안인지 나중에 답할 수 없다. */}
            <p className="m-0 mt-1 font-mono text-[11px] text-faint">
              {row.history_run_id ? (
                <>
                  본 실행 {row.history_run_id.slice(0, 8)}…
                  {row.history_run_id !== latestRunId && (
                    <span className="ml-1.5 text-warn">
                      (이 화면 위의 마지막 실행과 다릅니다)
                    </span>
                  )}
                </>
              ) : (
                <span className="text-warn">
                  어느 실행인지 기록되지 않았습니다 — 2026-08-30 이전 결정입니다
                </span>
              )}
            </p>
          </li>
        ))}
      </ol>
      <p className="m-0 mt-2 text-[11.5px] text-faint">
        번복은 지우지 않고 <b className="text-muted">회차를 올려</b> 쌓입니다 — 무엇을 골랐다가
        무엇으로 바꿨는지가 남아야 합니다.
      </p>
    </section>
  );
}
