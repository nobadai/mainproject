import type { ReactNode } from "react";

import { GRADE_KO, capPct } from "@/lib/format";
import type { ViewModel } from "@/lib/types";

export function Lbl({ children }: { children: ReactNode }) {
  return <span className="lbl">{children}</span>;
}

export function Chip({
  tone = "plain",
  children,
}: {
  tone?: "plain" | "hold" | "ok" | "up";
  children: ReactNode;
}) {
  const tones = {
    plain: "border-rule-2 bg-surface-2 text-ink-2",
    hold: "border-warn-line bg-warn-soft text-warn",
    ok: "border-down bg-down-soft text-down",
    up: "border-up bg-up-soft text-up",
  } as const;
  return (
    <span
      className={`inline-flex items-center gap-[7px] rounded-md border px-3 py-[5px] text-sm2 font-semibold ${tones[tone]}`}
    >
      {children}
    </span>
  );
}

/**
 * 색이 아니라 **형태**로 구분한다 — 출처의 성격이지 경고가 아니라서 상태색을 안 쓴다.
 * 기본값(SIM_FIXED)에는 아무것도 안 붙인다.
 */
export function GradeBadge({ grade }: { grade: string }) {
  if (grade === "SIM_FIXED") return null;
  const m = GRADE_KO[grade] ?? { ko: grade, why: grade, dashed: true };
  return (
    <span
      title={m.why}
      className="inline-flex flex-none items-center gap-1.5 rounded-[5px] border border-warn-line bg-warn-soft px-2 py-[2px] font-mono text-xs2 tracking-wide text-warn"
    >
      <i
        className={`block size-[9px] flex-none rounded-[2px] ${
          m.dashed ? "border-2 border-dashed border-warn" : "bg-warn"
        }`}
      />
      {m.ko}
    </span>
  );
}

/**
 * 화면에 떠 있는 것이 실측인지 표본인지 **항상** 밝힌다.
 * 둘이 섞이면 발표에서 사실이 아닌 것을 사실처럼 말하게 된다.
 */
export function SourceBadge({ vm }: { vm: ViewModel }) {
  if (vm.source === "api") {
    return (
      <div className="mb-[22px] flex flex-wrap items-center gap-3 rounded-[10px] border border-down bg-down-soft px-6 py-3.5">
        <Chip tone="ok">실측</Chip>
        <span className="text-md2 text-down">
          <b className="font-mono">POST /master/request</b> 응답을 그리고 있다
          {vm.endCode ? ` · ${vm.endCode}` : ""}
        </span>
      </div>
    );
  }
  return (
    <div className="mb-[22px] flex flex-wrap items-center gap-3 rounded-[10px] border border-warn-line bg-warn-soft px-6 py-3.5">
      <Chip tone="hold">내장 표본</Chip>
      <span className="text-md2 text-warn">
        API 응답을 쓰지 못해 고정 표본을 그리고 있다 — 화면의 수치는 실행 결과가 아니다.
      </span>
      {vm.fallbackReason && (
        <span className="w-full font-mono text-xs2 text-warn">사유 · {vm.fallbackReason}</span>
      )}
    </div>
  );
}

/** 단일 색상 크기 비교라 범례가 필요 없다 — 값이 곧 직접 라벨이다. */
export function Bar({ ratio }: { ratio: number }) {
  return (
    <div className="mt-2.5 h-2 overflow-hidden rounded-[5px] bg-surface-3">
      <i
        className="block h-full rounded-r-[5px] bg-accent"
        style={{ width: `${Math.min(ratio * 100, 100).toFixed(2)}%` }}
      />
    </div>
  );
}

export function CapMeter({ amount, cap }: { amount: number; cap: number }) {
  const r = amount / cap;
  return (
    <>
      <span className="num text-lg2 font-semibold text-accent">{capPct(r)}</span>
      <Bar ratio={r} />
    </>
  );
}
