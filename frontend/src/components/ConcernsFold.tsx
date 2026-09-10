"use client";

import { useState } from "react";

import { Panel } from "@/components/Badges";
import type { ProcurementRunResponse } from "@/lib/types";

/**
 * 「확인해 주세요」와 「검증」을 **배지 한 줄로 접는다.**
 *
 * 🔴 **감추는 것이 아니라 접는 것이다.** 이 화면의 절반은 "못 한 것"이고, 그 절반이
 *   결론과 같은 화면에 있어야 한다 (`ProcurementResult` 머리말). 그래서 접힌
 *   상태에서도 **배지가 남는다** — 무엇이 몇 건 접혀 있는지를 그 한 줄이 말한다.
 *
 * ★ 매입이 청한 것이 이것이다 (2026-09-09).
 *
 * ```text
 * 넷 다 "접어 달라" 이지 "빼 달라" 가 아닙니다
 * 🔴 mocked_inputs · evidence_grade · 「확인해 주세요」 건수
 *    → 접어도 첫 화면에 남아야 한다
 * ```
 *
 * ★ **왜 접는가.** 실측(2026-01-30 기준일)으로 두 판이 펼쳐진 채 안 하나에 글
 *   600자를 먹는다. 결론 카드가 화면 밖으로 밀려나면 **결론을 못 보고 스크롤한다.**
 *
 * ★ **내용은 한 줄도 안 지운다.** 펼치면 지금까지 그리던 두 `Panel` 이 그대로 온다 —
 *   여기서 문장을 다시 쓰거나 고르지 않는다.
 *
 * ★ **mock 이 하나라도 있으면 눈에 띈다.** `tone="attn"` 과 같은 결(경고 바탕)로
 *   접힌 줄을 칠한다. mock 으로 만든 안을 실측으로 읽는 것이 이 화면이 막는 것이다.
 *
 * ★ **접기 상태는 기억하지 않는다.** 실행마다 새 결론이고, 앞 실행에서 접어 뒀다고
 *   다음 실행의 mock 경고까지 접혀 있으면 그것이 곧 "감추는 것"이 된다.
 */
export function ConcernsFold({ run }: { run: ProcurementRunResponse }) {
  const [open, setOpen] = useState(false);

  const mocked = run.mocked_inputs.length;
  const findings = run.findings.length;
  const concerns = run.concerns.length;
  const skipped = run.skipped_checks.length;

  // 🔴 **다 0이면 그리지 않는다** — 그때는 접을 것도 감출 것도 없다.
  //    `concerns` 까지 세는 것은 그것도 「확인해 주세요」 안에 있는 **내용**이기
  //    때문이다. 셋만 보고 0이라 판단하면 concerns 만 있는 실행에서 글이 사라진다.
  if (mocked + findings + concerns + skipped === 0) return null;

  // 0인 갈래는 안 적는다 — `지적 0건` 을 늘 보여 줄 이유가 없다.
  const counts = [
    mocked > 0 ? `mock ${mocked}건` : "",
    findings > 0 ? `지적 ${findings}건` : "",
    concerns > 0 ? `확인 ${concerns}건` : "",
    skipped > 0 ? `미판정 ${skipped}건` : "",
  ].filter(Boolean);

  const attn = mocked > 0;

  return (
    <section
      className={`overflow-hidden rounded-lg border ${
        attn ? "border-warn/25" : "border-line"
      }`}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className={`flex w-full flex-wrap items-center justify-between gap-x-2 gap-y-1
          px-3 py-2 text-left focus-visible:outline-2 focus-visible:-outline-offset-2
          focus-visible:outline-accent ${attn ? "bg-warn-wash" : "bg-sunk"}`}
      >
        <span
          className={`text-[12.5px] font-semibold ${attn ? "text-warn" : "text-muted"}`}
        >
          {attn && (
            <span aria-hidden="true" className="mr-1">
              ⚠️
            </span>
          )}
          {counts.join(" · ")}
        </span>
        <span className="ml-auto text-[12px] font-normal text-muted">
          {open ? "접기" : "펼치기"}
        </span>
      </button>

      {/*
        펼친 자리는 지금까지 화면이 그리던 것 그대로다. `Panel` 을 다시 만들지 않는 이유는
        문구·모양의 주인이 하나여야 해서다 — 여기서 베끼면 다음에 한쪽만 바뀐다.
      */}
      {open && (
        <div className="flex flex-col gap-2.5 border-t border-line bg-surface px-3 py-3">
          <Panel
            tone="attn"
            title="확인해 주세요"
            items={[
              ...run.mocked_inputs.map(
                (k) => `🔴 ${k} 는 mock 에서 왔습니다 — 이 결론을 실측으로 읽지 마십시오`,
              ),
              ...run.findings.map((f) => `지적: ${f}`),
              ...run.concerns,
            ]}
          />

          <Panel
            title="검증"
            items={[
              `지적 ${run.findings.length}건 · 판정하지 못한 검사 ${run.skipped_checks.length}건`,
              ...run.skipped_checks,
            ]}
          />
        </div>
      )}
    </section>
  );
}
