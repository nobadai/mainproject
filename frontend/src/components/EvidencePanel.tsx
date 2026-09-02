"use client";

import { useState } from "react";

import { AGENT_LABEL } from "@/components/LlmTrace";
import type { ProcurementRunResponse } from "@/lib/types";

/**
 * 부서가 낸 근거 — **시나리오 숫자가 어디서 왔는가.**
 *
 * 🔴 **왜 이 화면이 필요한가 (멘토 지적 2026-09-01).**
 *
 * ```text
 * "매입 시나리오 관련에서 근거의 내용이 보일 수 있게 화면 구성하자"
 * ```
 *
 *   화면은 *"재무 상한 2,000만원"* 은 보여주는데 **그 2,000만원이 어디서 나온
 *   숫자인지**는 못 보여주고 있었다. `AdvisorVerdicts` 의 `reasoning` 은 부서가 쓴
 *   **설명 문장**이지 출처가 아니다.
 *
 *   서버 쪽에서는 근거를 이미 모으고 있었다 — 검증 Tool 에만 넘기고 응답에서
 *   끊겼을 뿐이다 (2026-09-02 배선).
 *
 * ★ **화면이 근거를 고르지 않는다.** 순서도 서버가 준 그대로다. 화면이 정렬하거나
 *   중요한 것만 뽑기 시작하면 **"이게 더 중요하다" 는 판단이 화면에 생긴다** —
 *   `AdvisorVerdicts` 가 부서 payload 를 해석하지 않는 것과 같은 이유다.
 *
 * ★ **등급이 값만큼 중요하다.** 같은 숫자라도 `OFFICIAL` 과 `ASSUMED` 는 판단의
 *   무게가 다르다. 값만 보여주면 읽는 사람이 전부 실측으로 읽는다.
 *
 * ★ **기본은 접어 둔다.** 63건이 결론 위에 펼쳐지면 결론이 안 보인다. 근거는
 *   *"왜?"* 라고 물었을 때 답할 것이지 먼저 들이밀 것이 아니다.
 */

/** 모드 — 답하는 질문이 다르다. */
const MODE_LABEL: Record<string, string> = {
  PRE_PURCHASE: "경계",
  SCENARIO_VALIDATION: "판정",
};

const MODE_HINT: Record<string, string> = {
  PRE_PURCHASE: "상한이 왜 그 값인가",
  SCENARIO_VALIDATION: "이 안이 왜 그 판정인가",
};

/**
 * 등급 — **낮은 것을 눈에 띄게 한다.**
 *
 * 높은 등급을 강조하면 "근거가 튼튼하다" 는 인상만 남는다. 사람이 봐야 하는 것은
 * **약한 근거**다.
 */
const GRADE_LABEL: Record<string, string> = {
  OFFICIAL: "공식",
  VENDOR: "거래처",
  SIM_FIXED: "시뮬 고정",
  ASSUMED: "가정",
  INVALID_FOR_HARD: "하드 판정 불가",
};

const WEAK_GRADES = new Set(["ASSUMED", "INVALID_FOR_HARD"]);

function formatValue(value: number | string): string {
  if (typeof value === "string") return value;
  if (!Number.isFinite(value)) return String(value);
  // ★ **반올림하지 않는다.** 화면이 반올림하면 화면과 원본이 다른 숫자를 말한다.
  //   `maximumFractionDigits` 기본값은 3 이라 소수가 잘린다 - 넉넉히 열어 둔다.
  return value.toLocaleString("ko-KR", { maximumFractionDigits: 20 });
}

export function EvidencePanel({
  evidences,
}: {
  evidences: ProcurementRunResponse["evidences"];
}) {
  const [open, setOpen] = useState(false);

  // 🔴 **없는 것을 침묵으로 넘기지 않는다.** 빈 목록은 "근거가 완비됐다" 가 아니라
  //   "부서가 근거를 안 냈다" 이고, 그것도 사람이 알아야 할 사실이다.
  if (!evidences || evidences.length === 0) {
    return (
      <section className="mt-4 rounded-lg border border-line p-3">
        <p className="m-0 text-[12.5px] text-muted">
          근거 0건 — 부서가 근거를 내지 않았습니다. 숫자의 출처를 확인할 수 없습니다.
        </p>
      </section>
    );
  }

  const weak = evidences.filter((e) => WEAK_GRADES.has(e.evidence_grade)).length;

  return (
    <section className="mt-4 rounded-lg border border-line">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left"
      >
        <span className="text-[13px] font-semibold">
          숫자의 출처 · 근거 {evidences.length}건
          {weak > 0 && (
            <span className="ml-2 font-normal text-amber-700">
              약한 근거 {weak}건
            </span>
          )}
        </span>
        <span className="text-[12px] text-muted">{open ? "접기" : "펼치기"}</span>
      </button>

      {open && (
        <div className="overflow-x-auto border-t border-line">
          <table className="w-full min-w-[640px] border-collapse text-[12.5px]">
            <thead>
              <tr className="text-left text-muted">
                <th className="px-3 py-1.5 font-medium">부서</th>
                <th className="px-3 py-1.5 font-medium">무엇에 대한 근거</th>
                <th className="px-3 py-1.5 text-right font-medium">값</th>
                <th className="px-3 py-1.5 font-medium">출처</th>
                <th className="px-3 py-1.5 font-medium">등급</th>
              </tr>
            </thead>
            <tbody>
              {/* 순서를 손대지 않는다 — 부서가 낸 순서가 그 부서의 설명 순서다 */}
              {evidences.map((e, i) => (
                <tr key={`${e.agent}-${e.mode}-${e.claim}-${i}`} className="border-t border-line">
                  <td className="whitespace-nowrap px-3 py-1.5">
                    {AGENT_LABEL[e.agent] ?? e.agent}
                    <span className="ml-1.5 text-[11px] text-muted" title={MODE_HINT[e.mode] ?? e.mode}>
                      {MODE_LABEL[e.mode] ?? e.mode}
                    </span>
                  </td>
                  <td className="px-3 py-1.5">
                    <span className="font-mono text-[11.5px]">{e.claim}</span>
                    {e.evidence_detail && (
                      <span className="ml-1.5 text-[11px] text-muted">{e.evidence_detail}</span>
                    )}
                  </td>
                  <td className="whitespace-nowrap px-3 py-1.5 text-right tabular-nums">
                    {formatValue(e.value)}
                    <span className="ml-1 text-[11px] text-muted">{e.unit}</span>
                  </td>
                  <td className="whitespace-nowrap px-3 py-1.5 text-[11.5px] text-muted">
                    {e.source}
                    {e.ref_ids.length > 0 && (
                      <span className="ml-1" title={e.ref_ids.join(", ")}>
                        · {e.ref_ids.length}건
                      </span>
                    )}
                  </td>
                  <td
                    className={`whitespace-nowrap px-3 py-1.5 text-[11.5px] ${
                      WEAK_GRADES.has(e.evidence_grade) ? "text-amber-700" : "text-muted"
                    }`}
                  >
                    {GRADE_LABEL[e.evidence_grade] ?? e.evidence_grade}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
