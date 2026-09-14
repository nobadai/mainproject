"use client";

/**
 * 개발용 정보를 담는 접기 영역. **기본은 닫혀 있다.**
 *
 * ★ 지우지 않고 **분리한다.** 실행 축·Runtime·원본 판정은 디버깅에 필요하고, 그것이
 *   필요한 사람은 열어서 본다 — 다만 첫 화면을 지배하지 않는다.
 */

import type { ReactNode } from "react";

export function TechDetails({
  summary = "기술 상세",
  children,
  open = false,
}: {
  summary?: string;
  children: ReactNode;
  open?: boolean;
}) {
  return (
    <details
      open={open}
      className="rounded-xl border bg-panel px-4 py-3"
      style={{ borderColor: "var(--color-hair)" }}
    >
      <summary className="cursor-pointer list-none text-[12px] text-ink2">
        <span className="select-none">▸ {summary}</span>
      </summary>
      <div className="mt-3">{children}</div>
    </details>
  );
}

/** 기준일과 데이터 출처 한 줄. **내부 식별자를 쓰지 않는다.** */
export function DataBasis({ asOf, note }: { asOf: string; note: string }) {
  return (
    <p className="m-0 flex flex-wrap items-baseline gap-x-5 gap-y-1 text-[12px] text-ink2">
      <span>
        기준일 <b className="text-ink tabular-nums">{asOf}</b>
      </span>
      <span>
        데이터 기준 <b className="text-ink">{note}</b>
      </span>
    </p>
  );
}
