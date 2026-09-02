"use client";

import type { ProcurementRunResponse } from "@/lib/types";
import { DEPT_AXIS_LABEL, DEPT_LABEL, UNIT_LABEL, vocab } from "@/lib/vocab";

/**
 * 부서가 낸 조정안 — **무엇을 얼마로 고치라는 것인가.**
 *
 * 🔴 **왜 이 화면이 필요한가 (2026-09-02).** 전에는 개수만 있었다.
 *
 * ```text
 * 물류 가 조정을 제안했습니다 (3건) — 실행 이력에서 보십시오
 * ```
 *
 *   **실행 이력에 없었다.** `ExecutionStep` 에도 `master_agent_runs` 의 계획 행에도
 *   조정안 칸이 없어서, 사람에게 **가서 봐도 없는 곳**을 알려 주고 있었다.
 *   서버 쪽에서도 `len()` 만 담고 객체를 버리고 있었다.
 *
 * ★ **`AdvisorVerdicts` 와 나눠 놓는다.** 저쪽은 *"이 안이 통과인가"* 이고 여기는
 *   *"그럼 무엇을 고치나"* 다. 그리고 조정안은 `dept` 를 스스로 들고 오므로 판정
 *   목록과 이어 붙일 필요가 없다 — **이어 붙이면 `AgentName` 과 `Dept` 를 같은
 *   어휘로 쓰게 된다.** 지금 글자가 같을 뿐 다른 어휘다.
 *
 * ★ **화면이 고르거나 정렬하지 않는다.** 순서는 부서가 낸 그대로다. 정렬하면 그것이
 *   우선순위로 읽힌다 — `EvidencePanel` 과 같은 이유다.
 *
 * ★ **모르는 어휘는 지어내지 않는다.** 특히 `unit` 은 **닫힌 집합이 아니라서**
 *   (봉투가 `str` 이고 검사가 없다) 모르는 값이 실제로 올 수 있다. `미등록` 이
 *   붙어 보이는 것이 목적이다.
 *
 * ★ **0건에 침묵한다.** 물류는 `reject` 안의 조정을 승격하지 않으므로(#121)
 *   **0건이 정답인 날이 있다.** 근거(`EvidencePanel`)가 0건을 드러내는 것과 다르다 —
 *   저쪽 0은 *"근거를 안 냈다"* 이고 이쪽 0은 *"고칠 것을 안 냈다"* 라 뜻이 다르다.
 */
export function AdjustmentPanel({
  adjustments,
}: {
  adjustments: ProcurementRunResponse["adjustments"];
}) {
  if (!adjustments || adjustments.length === 0) return null;

  return (
    <section className="rounded-lg border border-line bg-sunk p-3">
      <p className="mb-2 text-[11px] font-semibold uppercase tracking-[0.05em] text-muted">
        부서가 제안한 조정 {adjustments.length}건
      </p>
      <ul className="m-0 flex list-none flex-col gap-1.5 p-0">
        {/* 순서를 손대지 않는다 — 부서가 낸 순서가 그 부서의 설명 순서다 */}
        {adjustments.map((a, i) => (
          <li key={`${a.dept}-${a.axis}-${a.target_value}-${i}`} className="text-[12.5px]">
            <span className="font-semibold text-ink">{vocab(DEPT_LABEL, a.dept) ?? a.dept}</span>
            <span className="ml-1.5 text-muted">
              {vocab(DEPT_AXIS_LABEL, a.axis) ?? a.axis}
            </span>
            <span className="tabular ml-1.5 font-mono text-[12px] text-ink">
              {/* 반올림하지 않는다 — 화면이 원본과 다른 숫자를 말하면 안 된다 */}
              {a.target_value.toLocaleString("ko-KR", { maximumFractionDigits: 20 })}
              <span className="ml-0.5 text-[11px] text-muted">
                {vocab(UNIT_LABEL, a.unit) ?? a.unit}
              </span>
            </span>
            {/* 부서가 쓴 문장 그대로 — 화면이 다시 쓰지 않는다 */}
            <span className="ml-1.5 text-muted">{a.reason}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}
