"use client";

import { Panel, SourceBadges } from "@/components/Badges";
import type { ProcurementRunResponse, Scenario } from "@/lib/types";

/**
 * 매입 제안 결과.
 *
 * ★ **이 화면의 절반은 "못 한 것"이다.** 입력이 어디서 왔는지 · 검증이 몇 개 돌았는지 ·
 *   무엇을 확인해야 하는지가 결론과 **같은 화면**에 있어야 한다. 수량만 크게 띄우면
 *   mock 으로 만든 안을 실측으로 읽는다.
 */

const END_TONE: Record<string, string> = {
  E1_APPROVED: "text-accent-ink",
  E2_HELD: "text-gold",
  E3_REJECTED: "text-warn",
  E4_NOT_STARTED: "text-warn",
  E5_NO_FEASIBLE_PLAN: "text-warn",
};

const won = (n: number) => n.toLocaleString("ko-KR");

export function ProcurementResult({
  run,
  canApprove,
  onPick,
}: {
  run: ProcurementRunResponse;
  canApprove: boolean;
  onPick: (scenario: Scenario) => void;
}) {
  // 결론 문장은 서버가 만든 리포트의 첫 줄이다 — 프론트가 다시 쓰지 않는다.
  const headline = run.report_text.split("\n")[0] || run.reason;

  return (
    <div className="flex flex-col gap-3.5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className={`m-0 text-base font-semibold ${END_TONE[run.end_code] ?? ""}`}>{headline}</h3>
        <span className="font-mono text-[11.5px] text-faint">
          {run.end_code} · 호출 {run.plan.length}단계 · {run.request_id}
        </span>
      </div>

      {run.scenarios.length > 0 && (
        <div className="grid gap-2.5 sm:grid-cols-3">
          {run.scenarios.map((s, i) => (
            <ScenarioCard
              key={String(s.label ?? i)}
              scenario={s}
              recommended={i === 1}
              canApprove={canApprove}
              onPick={() => onPick(s)}
            />
          ))}
        </div>
      )}

      <SourceBadges sources={run.input_sources} />

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
  );
}

function ScenarioCard({
  scenario,
  recommended,
  canApprove,
  onPick,
}: {
  scenario: Scenario;
  recommended: boolean;
  canApprove: boolean;
  onPick: () => void;
}) {
  const qty = scenario.total_qty_kg;
  const amount = scenario.total_amount_krw;
  const rounds = scenario.split_plan?.length ?? 0;

  return (
    <div
      className={`rounded-lg border p-3 ${
        recommended ? "border-accent bg-accent-wash" : "border-line bg-surface"
      }`}
    >
      <h4 className="m-0 text-sm font-semibold">{scenario.label ?? "이름 없음"}</h4>
      <p className="tabular m-0 mt-0.5 font-mono text-[22px] font-semibold tracking-tight">
        {/* 🔴 값이 없으면 0 으로 채우지 않는다 — 0 과 모름은 다르다 */}
        {qty == null ? "—" : won(qty)}
        <span className="ml-1 text-[13px] font-normal">kg</span>
      </p>
      <small className="tabular block text-xs text-muted">
        {amount == null ? "금액 미산출" : `${won(amount)}원`}
        {rounds > 0 && ` · ${rounds}회 분할`}
      </small>
      <button
        type="button"
        onClick={onPick}
        disabled={!canApprove}
        title={canApprove ? undefined : "승인권자만 기록할 수 있습니다"}
        className={`mt-2.5 w-full rounded-md border px-2 py-1.5 text-[12.5px] ${
          recommended
            ? "border-accent bg-accent font-semibold text-white"
            : "border-line bg-surface text-muted"
        } ${canApprove ? "" : "cursor-not-allowed opacity-45"}`}
      >
        이 안으로 진행
      </button>
    </div>
  );
}
