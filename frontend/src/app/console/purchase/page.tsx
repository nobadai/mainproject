"use client";

/**
 * 매입 탭. 소유: **매입 파트.**
 *
 * ★ **승인 버튼은 여기에 두지 않습니다.** 승인은 서랍(마스터)에서
 *   `POST /master/runs/{request_id}/decision` 으로 합니다. 두 군데서
 *   승인할 수 있으면 «어느 쪽으로 승인했나» 가 기록에서 갈립니다.
 *   이 화면은 **무엇을 고를지 판단할 근거**를 보이는 자리입니다.
 */

import { useSyncExternalStore } from "react";

import {
  DataTable,
  ErrorBox,
  Loading,
  Note,
  Panel,
  Pill,
  SourceTag,
  StatRow,
} from "@/components/console/Blocks";
import { useTab } from "@/components/console/useTab";
//  🔴 시연용 기준일 (`#431`). 시연이 끝나면 이 줄과 아래 `asOf` 를 지우고
//     `useTab` 의 `AS_OF` 로 되돌린다.
import { asOfSnapshot, serverAsOf, subscribeAsOf } from "@/lib/demo_as_of";
import { purchase, type Plan, type PurchaseTab } from "@/lib/screen";

function PlanCard({ plan }: { plan: Plan }) {
  return (
    <article
      className="flex min-w-0 flex-col overflow-hidden rounded-xl border bg-panel"
      style={{
        borderColor: plan.pending ? "var(--color-t-good)" : "var(--color-hair)",
        boxShadow: plan.pending ? "0 0 0 1px var(--color-t-good)" : undefined,
      }}
    >
      <header
        className="flex flex-wrap items-center gap-2 border-b px-4 py-3"
        style={{ borderColor: "var(--color-hair-soft)" }}
      >
        <strong className="text-[14px] font-semibold">{plan.key}</strong>
        <Pill text={plan.knob} tone="info" />
        {plan.pending && <Pill text="승인 대기" tone="good" />}
        <span className="ml-auto text-[11.5px]" style={{ color: "var(--color-mut)" }}>
          {plan.coverage}
        </span>
      </header>

      <div className="flex flex-col gap-3.5 p-4">
        <dl className="m-0 flex flex-col gap-1.5 text-[12.5px]">
          {[
            ["사는 양", `${plan.qty_kg.toLocaleString("ko-KR")} kg`, true],
            ["예상 금액", `${plan.amount_krw.toLocaleString("ko-KR")} 원`, false],
            ["등급 단가", `${plan.unit_price.toLocaleString("ko-KR")} 원/kg · ${plan.grade}`, false],
          ].map(([k, v, hero]) => (
            <div key={String(k)} className="flex items-baseline justify-between gap-3">
              <dt className="m-0" style={{ color: "var(--color-mut)" }}>
                {k}
              </dt>
              <dd className={`tabular m-0 font-mono ${hero ? "text-[19px]" : "text-[13px]"}`}>{v}</dd>
            </div>
          ))}
          <div
            className="mt-1 flex items-baseline justify-between gap-3 rounded-lg px-3 py-2"
            style={{ background: "var(--color-t-warn-bg)", color: "var(--color-t-warn)" }}
          >
            <dt className="m-0 font-semibold">이보다 비싸면 안 산다</dt>
            <dd className="tabular m-0 font-mono text-[14px] font-semibold">
              {plan.max_price.toLocaleString("ko-KR")} 원/kg
            </dd>
          </div>
        </dl>

        <section className="flex flex-col gap-2">
          <h3 className="m-0 text-[11.5px] font-semibold" style={{ color: "var(--color-mut)" }}>
            회차
          </h3>
          <DataTable table={plan.legs} />
        </section>

        <section className="flex flex-col gap-2">
          <h3 className="m-0 text-[11.5px] font-semibold" style={{ color: "var(--color-mut)" }}>
            지급
          </h3>
          <DataTable table={plan.payments} />
        </section>

        <section className="flex flex-col gap-2">
          <h3 className="m-0 text-[11.5px] font-semibold" style={{ color: "var(--color-mut)" }}>
            근거
          </h3>
          <ul className="m-0 flex list-none flex-col gap-1.5 p-0 text-[11.5px] leading-relaxed">
            {plan.reasons.map((r, i) => (
              <li key={i} className="flex flex-wrap items-baseline gap-1.5">
                <span
                  className="shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold"
                  style={{ background: "var(--color-sunk)", color: "var(--color-ink2)" }}
                >
                  {r.source}
                </span>
                {r.carried && <Pill text="어제 기억" tone="sim" />}
                <span className="min-w-0 flex-1">{r.text}</span>
                {r.ref && (
                  <span className="font-mono text-[10px]" style={{ color: "var(--color-mut2)" }}>
                    {r.ref}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </section>

        {plan.risks.length > 0 && (
          <section className="flex flex-col gap-2">
            <h3 className="m-0 text-[11.5px] font-semibold" style={{ color: "var(--color-t-warn)" }}>
              걸리는 것
            </h3>
            <ul className="m-0 flex list-none flex-col gap-1.5 p-0 text-[11.5px] leading-relaxed">
              {plan.risks.map((r) => (
                <li key={r} className="flex gap-2">
                  <i aria-hidden style={{ color: "var(--color-t-warn)" }}>·</i>
                  <span>{r}</span>
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>
    </article>
  );
}

export default function PurchasePage() {
  const asOf = useSyncExternalStore(subscribeAsOf, asOfSnapshot, serverAsOf);
  const { data, error } = useTab<PurchaseTab>(asOf, () => purchase(asOf));

  if (error) return <ErrorBox message={error} />;
  if (!data) return <Loading what="매입" />;

  return (
    <>
      <SourceTag sources={[data.source]} />
      <StatRow items={data.stats} />

      <div className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(340px,1fr))]">
        {data.plans.map((p) => (
          <PlanCard key={p.key} plan={p} />
        ))}
      </div>
      <Note note={data.plans_note} />

      {/*
        🔴 subtitle 에 «사람이» 라고 쓰지 않는다 (2026-09-10 · 마스터 통보 「백필 승인은
        사람 승인이 아닙니다」). 걷기 구간을 decided_by="AUTO-BACKFILL" 로 채우기로
        정해졌고, 그날부터 이 표에는 사람이 누른 것과 자동으로 채운 것이 같이 실린다.

        ⚠️ «승인을 거친» 은 지금도 참이고 백필 뒤에도 참이다 — 주체를 단정한 쪽만 깨진다.

        🟡 «누가 승인했나» 를 표에 칸으로 더하는 것은 다음 판이다. 원장에 그 값이 없고,
        마스터가 purchases.decision_id(FK)를 세운 뒤 조인해 읽기로 했다.
      */}
      <Panel title="확정된 매입" subtitle="승인을 거친 뒤에 생깁니다" footer="승인은 아래 서랍에서 합니다.">
        <DataTable table={data.committed} />
        <Note note={data.committed_note} />
      </Panel>
    </>
  );
}
