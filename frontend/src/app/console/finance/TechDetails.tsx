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
      <summary className="cursor-pointer list-none text-[16px] text-ink2">
        <span className="select-none">▸ {summary}</span>
      </summary>
      <div className="mt-3">{children}</div>
    </details>
  );
}

/** 기준일과 데이터 출처 한 줄. **내부 식별자를 쓰지 않는다.** */
export function DataBasis({ asOf, note }: { asOf: string; note: string }) {
  return (
    <p className="m-0 flex flex-wrap items-baseline gap-x-5 gap-y-1 text-[16px] text-ink2">
      <span>
        기준일 <b className="text-ink tabular-nums">{asOf}</b>
      </span>
      <span>
        데이터 기준 <b className="text-ink">{note}</b>
      </span>
    </p>
  );
}

/**
 * 실행을 아직 안 골랐을 때.
 *
 * 🔴 **공용 `NoRunSelected` 를 쓰지 않는다.** 그쪽 본문에는 `sim_run_id` 라는 내부
 *    식별자 이름이 그대로 들어 있어, 실행을 고르기 전 첫 화면이 개발 용어로 시작한다.
 *    공용 컴포넌트는 이번 판의 수정 범위 밖이라 고치지 않고 여기서 같은 뜻을 사용자
 *    말로 적는다 — 동작은 같다. 아무것도 조회하지 않는다.
 */
export function NoRunChosen() {
  return (
    <section
      className="rounded-xl border border-dashed bg-panel px-5 py-10 text-center"
      style={{ borderColor: "var(--color-hair)" }}
    >
      <p className="m-0 text-[19px] font-semibold">먼저 볼 자료를 선택해 주세요</p>
      <p className="mb-0 mt-2 text-[16px] text-ink2">
        이 화면의 모든 숫자는 하나의 시뮬레이션 결과에 묶여 있습니다. 위의 «실행을 선택해
        주세요» 를 열어 자료를 고르면 그 자료에 저장된 사실만 보여 줍니다.
      </p>
    </section>
  );
}
