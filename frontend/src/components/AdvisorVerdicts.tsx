"use client";

import { useState } from "react";

import { AGENT_LABEL } from "@/components/LlmTrace";
import type { ProcurementRunResponse } from "@/lib/types";

/**
 * 조언자(재무·물류)가 낸 판정 — **그리고 그 부서가 보낸 것 그대로.**
 *
 * 🔴 **왜 이 화면이 필요한가 (실측 2026-08-31).** 화면은 이렇게만 말하고 있었다.
 *
 * ```text
 * 물류 판정이 조건부입니다 — 무조건 통과가 아닙니다
 * ```
 *
 *   읽는 사람은 *"물류가 이 안을 걸고넘어졌다"* 로 읽는다. 그런데 같은 실행의 물류
 *   payload 는 이랬다.
 *
 * ```text
 * scenario_results   보수 ok · 기본 ok · 공격 ok      ← 안에는 문제가 없다
 * hard_constraints   LOG-H02 UNRESOLVED (ZONE_CAPACITY_UNRESOLVED)
 * ```
 *
 *   **"안에 문제가 있다" 와 "검사를 못 돌렸다" 가 '조건부' 한 단어에 뭉쳐 있다.**
 *   완전히 다른 얘기인데 화면에서 구분이 안 된다.
 *
 * ★ **화면이 부서 payload 를 해석하지 않는다.** 모양이 부서마다 다르고, 화면이 뜻을
 *   붙이기 시작하면 **부서 스키마가 화면에 한 벌 더 생긴다** — 물류가 값을 바꾸는
 *   날 화면만 옛말을 한다. 그대로 편다.
 *
 * ★ **"실행 이력에서 보십시오" 의 목적지이기도 하다.** 서버 문구가 그렇게 안내하는데
 *   실행 이력 표에는 호출 단계만 있어 조정 제안이 개수조차 없었다.
 */

const STATUS_LABEL: Record<string, string> = {
  ok: "통과",
  conditional: "조건부",
  reject: "거절",
};

const STATUS_TONE: Record<string, string> = {
  ok: "border-line text-muted",
  conditional: "border-warn/35 text-warn",
  reject: "border-warn/45 text-warn",
};

export function AdvisorVerdicts({ verdicts }: { verdicts: ProcurementRunResponse["verdicts"] }) {
  const rows = Object.entries(verdicts ?? {});
  if (rows.length === 0) return null;

  return (
    <div className="rounded-lg border border-line bg-sunk p-3">
      <p className="mb-2 text-[11px] font-semibold uppercase tracking-[0.05em] text-muted">
        부서 판정
      </p>
      <div className="flex flex-col gap-2">
        {rows.map(([agent, v]) => (
          <AdvisorRow key={agent} agent={agent} verdict={v} />
        ))}
      </div>
    </div>
  );
}

function AdvisorRow({
  agent,
  verdict,
}: {
  agent: string;
  verdict: ProcurementRunResponse["verdicts"][string];
}) {
  const [open, setOpen] = useState(false);
  const label = STATUS_LABEL[verdict.business_status];
  const payload = verdict.payload ?? {};
  const hasPayload = Object.keys(payload).length > 0;
  const adjustments = verdict.suggested_adjustments ?? 0;

  return (
    <div className="rounded-lg border border-line-soft bg-surface px-3 py-2">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="text-[13px] font-semibold text-ink">{AGENT_LABEL[agent] ?? agent}</span>
        {/*
          🔴 라벨을 모르면 **추측해서 번역하지 않는다.** 판정을 못 낸 것과 모르는 값을
          낸 것은 다르다 — 앞엣것은 그 부서 문제, 뒤엣것은 화면 어휘가 낡은 것이다.
        */}
        {label ? (
          <span
            className={`rounded-md border px-1.5 py-0.5 text-[11.5px] ${
              STATUS_TONE[verdict.business_status] ?? "border-line text-muted"
            }`}
          >
            {label}
          </span>
        ) : (
          <span className="rounded-md border border-warn/35 px-1.5 py-0.5 text-[11.5px] text-warn">
            판정 없음 · {verdict.business_status || "—"}
          </span>
        )}
        {verdict.runtime_status !== "READY" && (
          <span className="font-mono text-[11px] text-warn">{verdict.runtime_status}</span>
        )}
        {adjustments > 0 && (
          <span className="text-[11.5px] text-muted">조정 제안 {adjustments}건</span>
        )}
        {verdict.needs_followup && <span className="text-[11.5px] text-warn">후속 확인 필요</span>}
        {hasPayload && (
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            className="ml-auto rounded-md border border-line px-2 py-0.5 text-[11.5px] text-muted hover:border-accent hover:text-accent-ink"
          >
            {open ? "접기" : `${AGENT_LABEL[agent] ?? agent}가 보낸 값`}
          </button>
        )}
      </div>

      {verdict.reasoning && (
        <p className="m-0 mt-1 text-[12.5px] leading-relaxed text-muted">{verdict.reasoning}</p>
      )}

      {open && hasPayload && (
        <div className="mt-2 flex flex-col gap-2 border-t border-line-soft pt-2">
          {Object.entries(payload).map(([key, value]) => (
            <Field key={key} name={key} value={value} />
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * 값 하나. **뜻을 붙이지 않고 모양만 보고 편다.**
 *
 * 🔴 `null` 과 `[]` 를 같게 그리지 않는다 — 부서들이 *"모른다"* 와 *"0건을 확인했다"*
 *   를 구분해 보내고 있다(물류 회신 §1-2). 화면이 뭉개면 그 구분이 사라진다.
 */
function Field({ name, value }: { name: string; value: unknown }) {
  return (
    <div>
      <p className="m-0 font-mono text-[11px] text-faint">{name}</p>
      <div className="mt-0.5 text-[12.5px]">{render(value)}</div>
    </div>
  );
}

function render(value: unknown): React.ReactNode {
  if (value === null || value === undefined) {
    return <span className="text-warn">모름 (null)</span>;
  }
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="text-muted">0건 (확인됨)</span>;
    if (value.every((v) => isRecord(v))) return <Rows rows={value as Record<string, unknown>[]} />;
    return <span className="font-mono text-muted">{value.map(scalar).join(" · ")}</span>;
  }
  if (isRecord(value)) {
    return (
      <div className="flex flex-col gap-0.5">
        {Object.entries(value).map(([k, v]) => (
          <div key={k} className="flex gap-2">
            <span className="font-mono text-[11.5px] text-faint">{k}</span>
            <span className="font-mono tabular-nums">{cell(v)}</span>
          </div>
        ))}
      </div>
    );
  }
  return <span className="font-mono tabular-nums">{scalar(value)}</span>;
}

/** 같은 키를 가진 객체 배열 → 표. 열 순서는 **첫 행이 보낸 순서** 그대로다. */
function Rows({ rows }: { rows: Record<string, unknown>[] }) {
  const columns = Array.from(new Set(rows.flatMap((r) => Object.keys(r))));

  return (
    <div className="overflow-x-auto rounded-md border border-line">
      <table className="w-full border-collapse text-[12px]">
        <thead>
          <tr className="bg-sunk text-[10.5px] uppercase tracking-wide text-faint">
            {columns.map((c) => (
              <th key={c} className="px-2 py-1 text-left font-mono font-normal">
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="tabular-nums">
          {rows.map((row, i) => (
            <tr key={i} className="border-t border-line-soft">
              {columns.map((c) => (
                <td key={c} className="px-2 py-1 font-mono">
                  {cell(row[c])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/**
 * 한 칸을 그린다. 🔴 **모름과 0 을 같은 색으로 두지 않는다.**
 *
 * 글자는 진작에 갈라 놨는데(`—` vs `0건`) **색이 같아 눈으로는 구분이 안 됐다**
 * (실측 2026-08-31, 화면 확인). 뜻이 다른 두 값이 같아 보이면 갈라 놓은 의미가 없다.
 *
 * ```text
 * —      값이 오지 않았다        흐리게 — 읽을 것이 없다는 신호
 * 0건    0건인 것을 확인했다      본문색 — 이건 **값이다**
 * ```
 *
 * ★ 위 `render()` 의 최상위와 색이 다른 것은 의도다. 최상위 `null` 은 부서가
 *   **키를 실어 놓고 "모른다" 고 답한 것**이라 눈에 띄어야 하고(`text-warn`),
 *   표 칸의 빈 자리는 그 행에 해당 사항이 없는 경우가 대부분이다 —
 *   `LOG-H01 PASS` 에 `skip_reason` 이 없는 것을 경고색으로 칠하면 **다섯 줄 중
 *   넷이 붉어져** 정작 봐야 할 `LOG-H02` 가 묻힌다.
 */
function cell(value: unknown): React.ReactNode {
  if (value === null || value === undefined) {
    return (
      <span className="text-faint" title="값이 오지 않았습니다 (모름)">
        —
      </span>
    );
  }
  if (Array.isArray(value) && value.length === 0) {
    return (
      <span className="text-muted" title="0건인 것을 확인했습니다">
        0건
      </span>
    );
  }
  return scalar(value);
}

/** 한 칸. **깊이가 더 있으면 JSON 그대로** — 화면이 지어내는 것보다 낫다. */
function scalar(v: unknown): string {
  // 🔴 표 안에서도 **0 과 모름을 가른다.** `—` 는 모름, `0건` 은 확인된 없음이다.
  //    한 칸이 좁다고 둘을 같은 기호로 뭉개면 밖에서 구분이 사라진다.
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") return v.toLocaleString("ko-KR");
  if (typeof v === "boolean") return v ? "예" : "아니오";
  if (typeof v === "string") return v;
  if (Array.isArray(v)) {
    if (v.length === 0) return "0건";
    return v.map(scalar).join(" · ");
  }
  return JSON.stringify(v);
}
