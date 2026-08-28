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
/**
 * 종료 코드를 사람 말로. **남의 실패를 우리 실패로 보이게 하지도, 감추지도 않는다.**
 *
 * `E3_REJECTED` 인데 시나리오가 실려 있는 상태가 실제로 나온다 — 매입 산출은 통과했고
 * 그 뒤 검증 단계에서 재무·물류 근거 대조가 걸린 경우다. 시나리오를 정상 표시하되
 * 어디서 반려됐는지는 화면에 남긴다.
 */
const END_CODE_KO: Record<string, { label: string; detail: string; tone: "ok" | "hold" }> = {
  E1_APPROVED: { label: "승인", detail: "선택지를 올릴 수 있다", tone: "ok" },
  E2_HELD: { label: "보류", detail: "실행 가능한 안이 없어 보류됐다", tone: "hold" },
  E3_REJECTED: {
    label: "검증 단계 반려",
    detail: "재무·물류 근거 대조에서 걸렸다 — 매입 산출은 통과했다",
    tone: "hold",
  },
  E4_NOT_STARTED: { label: "미시작", detail: "경계를 내지 못한 부서가 있다", tone: "hold" },
  E5_NO_FEASIBLE_PLAN: { label: "실행안 없음", detail: "확정 납품을 채울 수 없다", tone: "hold" },
};

export function EndCodeBanner({ vm }: { vm: ViewModel }) {
  if (vm.source !== "api" || !vm.endCode) return null;
  const meaning = END_CODE_KO[vm.endCode];
  if (!meaning || vm.endCode === "E1_APPROVED") return null;
  return (
    <div className="mb-[22px] flex flex-wrap items-center gap-3 rounded-[10px] border border-warn-line bg-warn-soft px-6 py-3.5">
      <Chip tone="hold">{meaning.label}</Chip>
      <span className="text-md2 text-warn">{meaning.detail}</span>
      <span className="ml-auto font-mono text-xs2 text-warn">{vm.endCode}</span>
      {vm.reasonText && (
        <span className="w-full text-sm2 text-warn">{vm.reasonText}</span>
      )}
    </div>
  );
}

export function SourceBadge({ vm }: { vm: ViewModel }) {
  if (vm.source === "api") {
    return (
      <div className="mb-[22px] flex flex-wrap items-center gap-3 rounded-[10px] border border-down bg-down-soft px-6 py-3.5">
        <Chip tone="ok">실행 결과</Chip>
        <span className="text-md2 text-down">
          <b className="font-mono">POST /master/request</b> 로 방금 돌린 응답을 그리고 있다 — 화면의
          수치는 이 실행이 낸 값이다
        </span>
        {!vm.gatesFromEvidence && (
          <span className="w-full text-sm2 text-down">
            판정 근거(임계 비교 문면)는 응답에 실리지 않는다 — 게이트에는 판정부가 말한 것만 있다
          </span>
        )}
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
