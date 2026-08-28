"use client";

import { useState } from "react";

import { SCENES } from "@/lib/fixtures";
import { AXIS_KO, n, pct } from "@/lib/format";
import type { ViewModel } from "@/lib/types";

import { ComparisonTable } from "./ComparisonTable";
import { ScenarioDetail } from "./ScenarioDetail";
import { Chip, EndCodeBanner, GradeBadge, Lbl, SourceBadge } from "./ui";

function VerdictBand({ vm }: { vm: ViewModel }) {
  const j = vm.judgment;
  return (
    <>
      <div className="mb-3.5 flex flex-wrap items-center gap-8 rounded-xl bg-accent px-[30px] py-[26px] text-on-accent">
        <div className="flex flex-col gap-0.5">
          <span className="lbl text-on-accent/70">품목 · 기준일</span>
          <span className="text-xl2 font-semibold tracking-tight">
            {vm.item}
            <span className="num ml-2 text-lg2 opacity-85">{vm.asOf}</span>
          </span>
        </div>

        {j ? (
          <>
            <span className="flex items-center gap-3 rounded-full bg-on-accent px-[22px] py-2.5 text-xl2 font-bold tracking-tight text-accent">
              <span className="size-3 rounded-full bg-current" />
              {j.situation === "stable" ? "안정" : "불확실"}
            </span>
            <span className="text-xl2 font-semibold tracking-tight">
              {vm.scenarios.length}안<small className="ml-2 text-sm2 font-medium opacity-75">제시</small>
            </span>
            <div className="ml-auto flex flex-wrap gap-2">
              {(["quantity", "timing", "mix"] as const).map((a) => {
                const on = j.allowedAxes.includes(a);
                return (
                  <span
                    key={a}
                    className={`rounded-md border-[1.5px] px-3.5 py-1.5 text-sm2 font-semibold ${
                      on
                        ? "border-on-accent bg-on-accent text-accent"
                        : "border-white/30 text-on-accent opacity-55 line-through"
                    }`}
                  >
                    {AXIS_KO[a]}
                  </span>
                );
              })}
            </div>
          </>
        ) : (
          /* 없는 판정을 지어내지 않는다 — 왜 없는지를 말한다 */
          <>
            <span className="text-xl2 font-semibold tracking-tight">
              {vm.scenarios.length}안<small className="ml-2 text-sm2 font-medium opacity-75">제시</small>
            </span>
            <div className="ml-auto max-w-[520px] rounded-lg border-[1.5px] border-white/35 px-4 py-2.5 text-sm2 leading-relaxed">
              <b>시장 상황·열린 축 미노출</b> — 마스터 응답이 시나리오 배열만 싣고 제안 최상위
              판정을 버린다 (<span className="font-mono">flow.py:_scenarios_of</span>). 화면이
              추정하지 않는다.
            </div>
          </>
        )}
      </div>

      <div className="mb-[26px] rounded-[10px] border border-rule bg-surface px-6 py-4 text-md2 text-ink-2">
        <b className="mr-2.5 font-semibold text-ink">판단 요약</b>
        {vm.reasoning}
      </div>
    </>
  );
}

function GateStrip({ vm }: { vm: ViewModel }) {
  if (!vm.gates || !vm.direction) return null;
  const d = vm.direction;
  return (
    <div className="mb-[26px] grid grid-cols-[repeat(auto-fit,minmax(240px,1fr))] overflow-hidden rounded-xl border border-rule bg-surface">
      <div className="border-r border-rule px-6 py-5 last:border-r-0">
        <div className="flex min-h-[26px] items-center justify-between gap-2.5">
          <Lbl>예측 방향 · D+14</Lbl>
        </div>
        {/* 적·청은 가격 방향에만 쓴다 — 한국 시장 관행(상승=적) */}
        <div
          className={`num my-2 block text-xl2 font-semibold whitespace-nowrap ${
            d.change < 0 ? "text-down" : "text-up"
          }`}
        >
          {d.change >= 0 ? "▲" : "▼"} {pct(Math.abs(d.change))}
          <span className="mt-[5px] block text-sm2 font-medium tracking-normal text-ink-3">
            {n(d.current)} → {n(d.predicted)}원/kg
          </span>
        </div>
        <div className="text-sm2 leading-relaxed text-ink-2">{d.say}</div>
        <div className="mt-2.5 font-mono text-xs2 text-ink-3">{d.ref}</div>
      </div>
      {vm.gates.map((g) => (
        <div key={g.name} className="border-r border-rule px-6 py-5 last:border-r-0">
          <div className="flex min-h-[26px] items-center justify-between gap-2.5">
            <Lbl>{g.name}</Lbl>
            {g.blocked && <Chip tone="hold">{g.chip}</Chip>}
          </div>
          <div
            className={`num my-2 text-xl2 font-semibold ${g.blocked ? "text-ink-3" : ""}`}
          >
            {g.value}
          </div>
          <div className="text-sm2 leading-relaxed text-ink-2">{g.say}</div>
          <div className="mt-2.5 font-mono text-xs2 text-ink-3">{g.ref}</div>
        </div>
      ))}
    </div>
  );
}

export function ResultScreen({
  vm,
  onFlip,
  busy,
}: {
  vm: ViewModel;
  onFlip: (asOf: string) => void;
  busy: boolean;
}) {
  const [pick, setPick] = useState(vm.scenarios.length - 1);
  const idx = Math.min(pick, vm.scenarios.length - 1);
  const j = vm.judgment;

  /* 만들지 않은 안의 자리에 적는 이유 — 게이트 판정에서 그대로 가져온다 */
  const ciGate = vm.gates?.find((g) => g.name.includes("CI"));
  const voidReason =
    j?.situation === "uncertain" && ciGate
      ? `예측 구간폭 ${ciGate.value} 가 임계 8.0% 이상이라 선매입 궤적이 차단됐다. 미리 사 두는 안은 만들지 않는다.`
      : null;

  return (
    <>
      {/* 시연에서 두 기준일을 오가는 자리 — as_of 를 바꿔 재호출한다 */}
      <div className="mb-[22px] flex gap-1 rounded-[9px] border border-rule bg-surface-2 p-[5px]">
        {Object.keys(SCENES).map((d) => (
          <button
            key={d}
            type="button"
            disabled={busy}
            aria-pressed={d === vm.asOf}
            onClick={() => onFlip(d)}
            className={`flex-1 rounded-md px-4 py-3 text-center text-md2 font-medium disabled:opacity-50 ${
              d === vm.asOf
                ? "bg-surface font-semibold text-ink shadow-sm"
                : "text-ink-2 hover:bg-surface/60"
            }`}
          >
            {d}
            <span className="mt-0.5 block text-xs2 font-medium text-ink-3">{SCENES[d].blurb}</span>
            {d === vm.asOf && (
              <span
                className={`mt-1 inline-block rounded px-2 py-0.5 text-xs2 font-semibold ${
                  vm.source === "api"
                    ? "bg-down-soft text-down"
                    : "bg-warn-soft text-warn"
                }`}
              >
                {vm.source === "api" ? "실행 결과" : "표본"}
              </span>
            )}
          </button>
        ))}
      </div>

      <SourceBadge vm={vm} />
      <EndCodeBanner vm={vm} />
      <VerdictBand vm={vm} />

      {j && j.contextDocs.length > 0 && (
        <div className="mb-[26px] flex flex-wrap items-center gap-3 rounded-[10px] border border-warn-line bg-warn-soft px-6 py-4.5">
          <Lbl>참조 문서</Lbl>
          {j.contextDocs.map((doc) => (
            <span
              key={doc}
              className="rounded-md border border-warn-line bg-surface px-3 py-[5px] font-mono text-sm2 font-semibold text-warn"
            >
              {doc}
            </span>
          ))}
          <span className="text-md2 text-warn">
            예측이 흔들려 외부 문서를 열어 봤다 — 각 안의 근거에 발췌가 함께 실린다.
          </span>
        </div>
      )}

      <GateStrip vm={vm} />

      <ComparisonTable
        scenarios={vm.scenarios}
        financeCap={vm.financeCap}
        voidReason={voidReason}
      />

      <div className="mt-[34px] mb-4 flex flex-wrap items-center gap-3.5">
        <h2 className="text-lg2 font-semibold">안별 상세</h2>
        <div className="ml-auto flex flex-wrap gap-2">
          {vm.scenarios.map((s, i) => (
            <button
              key={s.label}
              type="button"
              aria-pressed={i === idx}
              onClick={() => setPick(i)}
              className={`rounded-lg border px-[22px] py-2.5 text-md2 font-semibold ${
                i === idx
                  ? "border-accent bg-accent text-on-accent"
                  : "border-rule-2 bg-surface text-ink-2"
              }`}
            >
              {s.label}
            </button>
          ))}
        </div>
      </div>
      {vm.scenarios[idx] && <ScenarioDetail s={vm.scenarios[idx]} />}

      <div className="mt-[26px] overflow-hidden rounded-xl border border-rule bg-surface shadow-sm">
        <div className="flex flex-wrap items-center gap-4 border-b border-rule px-6 py-4.5">
          <h2 className="text-lg2 font-semibold">계약 검증</h2>
          <Lbl>마스터가 회신을 받고 돌리는 검사</Lbl>
        </div>
        <div className="grid grid-cols-[repeat(auto-fit,minmax(230px,1fr))] gap-px bg-rule">
          <div className="bg-surface px-6 py-4.5">
            <Lbl>근거 항목</Lbl>
            <div className="num mt-1.5 text-lg2 font-semibold">
              {vm.evidenceCount === null ? "미노출" : `${vm.evidenceCount}건`}
            </div>
          </div>
          <div className="bg-surface px-6 py-4.5">
            <Lbl>미수신 데이터</Lbl>
            <div className="num mt-1.5 text-lg2 font-semibold">{vm.missingData.length}건</div>
          </div>
          <div className="bg-surface px-6 py-4.5">
            <Lbl>사용 Tool</Lbl>
            <div className="num mt-1.5 text-lg2 font-semibold">{vm.usedTools.length}개</div>
          </div>
          <div className="bg-surface px-6 py-4.5">
            <Lbl>실행 식별자</Lbl>
            <div className="num mt-1.5 text-sm2 font-semibold break-all">{vm.runId}</div>
          </div>
        </div>
        {(vm.missingData.length > 0 || vm.concerns.length > 0 || vm.skippedChecks.length > 0) && (
          <div className="space-y-2 border-t border-rule bg-surface-2 px-6 py-4.5">
            {[...vm.missingData.map((t) => ["미수신", t] as const),
              ...vm.concerns.map((t) => ["사람 확인" as const, t]),
              ...vm.skippedChecks.map((t) => ["미검사" as const, t])].map(([k, t], i) => (
              <div key={i} className="flex items-start gap-3">
                <Chip tone="hold">{k}</Chip>
                <span className="text-sm2 leading-relaxed text-ink-2">{t}</span>
              </div>
            ))}
          </div>
        )}
        <div className="flex flex-wrap gap-6 border-t border-rule bg-surface-2 px-6 py-4.5">
          <span className="flex items-center gap-2.5 text-sm2 text-ink-2">
            <GradeBadge grade="ASSUMED" /> 다른 값에서 파생한 가정 — 그대로 믿지 않는다
          </span>
          <span className="flex items-center gap-2.5 text-sm2 text-ink-2">
            <Chip tone="hold">보류</Chip> 선행 데이터 미확정으로 검사를 미룬 항목
          </span>
          <span className="text-sm2 text-ink-2">배지가 없으면 시뮬레이션 고정값이다</span>
        </div>
      </div>
    </>
  );
}
