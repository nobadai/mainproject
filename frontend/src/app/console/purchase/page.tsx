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
import { purchase, type Plan, type PurchaseTab, type Tone } from "@/lib/screen";

/**
 * 상태 낱말 → 색. **낱말은 서버가 정한다** (`app/api/plan_state.py` 의 넷).
 *
 * ★ 색만 여기서 고른다. 낱말을 화면이 만들면 대시보드와 매입 화면이 같은 안을 다른
 *   이름으로 부르게 된다 — 지금 둘이 같은 자리에서 낱말을 받는다.
 *
 * 🔴 **모르는 낱말은 감추지 않고 그대로 보인다** (`neutral`). 서버가 어휘를 늘리는 날
 *    화면이 조용히 빈 배지를 내면, 사람은 상태가 없는 줄로 읽는다.
 */
const STATE_TONE: Record<string, Tone> = {
  //  기다리는 것 — 사람이 아직 할 일이 남았다
  후보: "warn",
  //  결정이 났다
  승인됨: "good",
  //  결정 + 실제로 산 값까지 적혔다. 승인됨과 **색으로도** 갈라 둔다
  "매입 기록됨": "info",
  //  안 사기로 한 것
  반려: "bad",
};

/**
 * 이 안이 지금 어느 상태인가.
 *
 * 🔴 **승인된 안에도 배지가 붙어야 한다.** 예전에는 `pending` 일 때만 배지를 달아서
 *    **승인된 안이 아무 표시 없이** 떴고, 그래서 미결정 안과 구분이 안 됐다
 *    (2026-09-16 실측 · 04-13 배추·무가 `approved:true` 인데 화면에 아무것도 없었다).
 *
 * 🔴 **`approved` 하나로는 못 가른다.** 승인만 된 안과 실매입까지 적은 안이 둘 다
 *    참이다. 이제 매입 API 가 `state` 를 실으므로 **받은 말을 그대로** 쓴다 —
 *    여기서 「매입 기록됨」을 지어내던 자리가 아니라, 서버가 말해 준 것을 옮긴다.
 */
function PlanState({ plan }: { plan: Plan }) {
  return <Pill text={plan.state} tone={STATE_TONE[plan.state] ?? "neutral"} />;
}

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
        <PlanState plan={plan} />
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
          {/*
            🔴 이 자리는 `cut_unit_price` 다 — `max_price` 가 아니다.
               `max_price` 는 재무 STRESS 로 나가는 수이고, 실제로 안을 죽이는 것은 컷이다.
               둘은 방향이 반대라(밴드가 좁아지면 컷은 엄격해지고 STRESS 는 느슨해진다)
               한 수가 둘을 대신할 수 없다. 지금은 값이 같아 안 틀리지만 컷 산식을 바꾸는
               날 갈라지고, 그때 이 라벨이 틀린 수 위에 붙는다.
            ⚠️ `null` 일 때 `max_price` 로 안 메운다 — 없는 값을 그럴듯한 값으로 채우면
               없었다는 사실이 지워진다.
          */}
          <div
            className="mt-1 flex items-baseline justify-between gap-3 rounded-lg px-3 py-2"
            style={{ background: "var(--color-t-warn-bg)", color: "var(--color-t-warn)" }}
          >
            <dt className="m-0 font-semibold">이보다 비싸면 안 산다</dt>
            <dd className="tabular m-0 font-mono text-[14px] font-semibold">
              {plan.cut_unit_price === null
                ? "이 실행에는 기준이 없습니다"
                : `${plan.cut_unit_price.toLocaleString("ko-KR")} 원/kg`}
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
                {/*
                  🔴 근거 꼬리표(`r.ref`)를 **화면에서만** 가린다 (2026-09-17).
                     `FC-…` · `INV-LOT-RCPT-SIM-CHAIN-…` 같은 내부 식별자라 업무가 안 읽는다.
                  ⚠️ API 에서는 빼지 않는다 — 모든 근거에 `ref_id` 가 있어야 한다 (규칙 4).
                     되짚을 때는 API 응답에서 읽는다.
                */}
                <span className="min-w-0 flex-1">{r.text}</span>
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
        {/*
          🔴 매입 번호 칸(`approval`)을 **화면에서만** 가린다 (2026-09-17). 값은 API 에 남는다.
             잰 것 — REH-0914 08-31 · FINAL-0918 09-14 · V13 01-26 세 실행에서
             ① 줄이 (품목 · 사는 날)만으로 **겹침 0** (291 · 495 · 21줄) — 번호 없이도 줄이 갈린다
             ② 번호 44~52자 · 끝이 전부 `D1-S1` — 다른 칸에 없는 정보가 없다
             ③ 재무 API 도 `purchase_id` 를 싣지만 **재무 화면은 안 그린다** — 맞대 볼 화면이 없다
          ⚠️ 이름을 고치는 쪽(「매입 번호」)은 API 가 했다 — 칸이 원래 「승인」으로 틀려 있었다.
        */}
        <DataTable
          table={{
            ...data.committed,
            columns: data.committed.columns.filter((c) => c.key !== "approval"),
          }}
        />
        <Note note={data.committed_note} />
      </Panel>
    </>
  );
}
