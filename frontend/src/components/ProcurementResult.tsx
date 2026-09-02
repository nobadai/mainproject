"use client";

import { AdvisorVerdicts } from "@/components/AdvisorVerdicts";
import { EvidencePanel } from "@/components/EvidencePanel";
import { Panel, SourceBadges } from "@/components/Badges";
import { AGENT_LABEL } from "@/components/LlmTrace";
import type { ProcurementRunResponse, Scenario } from "@/lib/types";
import { AXIS_LABEL, CONFIDENCE_LABEL, SITUATION_LABEL, vocab } from "@/lib/vocab";

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
  onRerun,
}: {
  run: ProcurementRunResponse;
  canApprove: boolean;
  onPick: (scenario: Scenario) => void;
  /** 조건을 붙여 다시 — 입력창에 문안을 채우고 커서를 준다. **보내는 것은 사람이다.** */
  onRerun?: () => void;
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

      {/*
        🔴 **안이 없으면 사유가 답이다.** 지금까지 화면은 "보류합니다" 만 적고
        왜 없는지는 안 적었다 — 사용자는 시스템이 고장 난 것으로 읽는다.

        ★ **둘을 다 적는다.** 마스터는 *"유효한 안이 없다"* 까지만 말하고,
          **왜 없는지는 매입이 안다.** 매입 문장이 더 유용하다:
          "매입단가 1,650원이 max_price 992원 초과" (실측 2026-08-31).
        ★ 매입 문장을 화면이 다시 쓰지 않는다 — 그대로 옮긴다.
      */}
      {run.scenarios.length === 0 && (
        <div className="rounded-lg border border-line bg-sunk px-3.5 py-3">
          {run.reason && (
            <p className="m-0 text-[13px] leading-relaxed">{run.reason}</p>
          )}
          {/*
            🔴 **막은 부서가 왜 막았는지** (2026-09-02). 전에는 `run.reason` 한 줄이
            전부라 *"경계를 내지 못한 에이전트: finance"* 만 보였다. 그것을 읽은
            사람이 할 수 있는 것은 *"다시 돌려 본다"* 뿐이고, 그건 조사가 아니라
            추측이다 (재현성 측정에서 6회 중 2회를 그렇게 놓쳤다).

            ★ **문장은 서버가 만든다** (`detail`). 화면이 다시 조립하면 결론 문장과
              여기가 갈린다 — 근거를 검증과 화면이 같은 객체로 보게 한 것과 같다.
          */}
          {run.blocked_failures.length > 0 && (
            <ul className="m-0 mt-2 list-none space-y-1 p-0">
              {run.blocked_failures.map((f) => (
                <li key={f.agent} className="text-[12.5px] text-warn">
                  <b className="font-semibold">{AGENT_LABEL[f.agent] ?? f.agent}</b>
                  <span className="ml-1 font-mono text-[11.5px] text-faint">
                    {f.runtime_status}
                  </span>
                  {/* 부서가 쓴 문장 그대로 — 화면이 다시 쓰지 않는다 */}
                  <span className="ml-1.5">{f.detail}</span>
                </li>
              ))}
            </ul>
          )}
          {run.judgment?.no_proposal_reason && (
            <p className="m-0 mt-2 text-[13px] leading-relaxed text-warn">
              매입: {run.judgment.no_proposal_reason}
            </p>
          )}
          {(run.judgment?.rejected_reasons ?? []).length > 0 && (
            <ul className="m-0 mt-2 list-none space-y-1 p-0">
              {run.judgment.rejected_reasons!.map((r, i) => (
                <li key={i} className="text-[12.5px] text-muted">
                  <b className="font-semibold text-ink">{r.label ?? "안"}</b> — {r.reason}
                </li>
              ))}
            </ul>
          )}
          {/*
            매입이 준 닫힌 집합을 한국어로 옮긴다. **모르는 값은 원문 + `미등록`** 이라
            매입이 어휘를 늘린 날 화면에서 바로 보인다 (매입 2026-08-31 회신 ④).
          */}
          {vocab(SITUATION_LABEL, run.judgment?.situation) && (
            <p className="m-0 mt-2 text-[11.5px] text-faint">
              매입 판단 · 상황 {vocab(SITUATION_LABEL, run.judgment?.situation)}
              {vocab(CONFIDENCE_LABEL, run.judgment?.confidence)
                ? ` · 확신 ${vocab(CONFIDENCE_LABEL, run.judgment?.confidence)}`
                : ""}
              {(run.judgment?.allowed_axes ?? []).length > 0
                ? ` · 열린 축 ${(run.judgment.allowed_axes ?? [])
                    .map((a) => vocab(AXIS_LABEL, a) ?? a)
                    .join("·")}`
                : ""}
            </p>
          )}
        </div>
      )}

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

      {onRerun && run.scenarios.length > 0 && (
        <button
          type="button"
          onClick={onRerun}
          className="self-start rounded-lg border border-line bg-sunk px-3 py-1.5 text-[12.5px] text-muted hover:border-accent hover:text-accent-ink"
        >
          ↻ 조건을 붙여 다시 만들기
        </button>
      )}

      <SourceBadges sources={run.input_sources} />

      {/*
        🔴 판정 라벨 **밑에** 둔다. "조건부" 한 단어가 *"안에 문제가 있다"* 와
        *"검사를 못 돌렸다"* 를 뭉개므로, 그 부서가 보낸 것을 열어 볼 수 있어야 한다.
      */}
      <AdvisorVerdicts verdicts={run.verdicts} />
      {/* 판정 바로 아래에 둔다 — "왜 그 판정인가" 를 물은 다음에 보는 것이다 */}
      <EvidencePanel evidences={run.evidences} />

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
