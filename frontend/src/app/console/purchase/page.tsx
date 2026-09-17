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
import { Table as PagedTable, type Column as PagedColumn } from "@/components/console/ConsoleData";
import { useTab } from "@/components/console/useTab";
//  🔴 시연용 기준일 (`#431`). 시연이 끝나면 이 줄과 아래 `asOf` 를 지우고
//     `useTab` 의 `AS_OF` 로 되돌린다.
import { asOfSnapshot, serverAsOf, subscribeAsOf } from "@/lib/demo_as_of";
import {
  purchase,
  type Cell,
  type Plan,
  type PurchaseTab,
  type Table as ScreenTable,
  type Tone,
} from "@/lib/screen";

/**
 * 상태 낱말 → 색. **낱말은 서버가 정한다** (`app/api/plan_state.py` 의 넷).
 *
 * ★ 색만 여기서 고른다. 낱말을 화면이 만들면 대시보드와 매입 화면이 같은 안을 다른
 *   이름으로 부르게 된다 — 지금 둘이 같은 자리에서 낱말을 받는다.
 *
 * 🔴 **모르는 낱말은 감추지 않고 그대로 보인다** (`neutral`). 서버가 어휘를 늘리는 날
 *    화면이 조용히 빈 배지를 내면, 사람은 상태가 없는 줄로 읽는다.
 */
/*
 * ══ 안 그리는 것 — 화면에서만 · API 는 그대로 (2026-09-17) ══════════════════════════
 *
 * 화면을 줄이려고 다섯을 뺐다. 🔴 **API 칸은 하나도 안 지웠다** — `GET /api/purchase`
 * 응답에 그대로 있고, 되짚을 때는 거기서 읽는다.
 *
 *   ① 재고 근거의 로트 ID 괄호     「가용 29.0kg (로트 LOT-RCPT-SIM-CHAIN-…-1-1)」 에서
 *                                  괄호만 걷는다 · 「가용 29.0kg」 은 남긴다
 *      왜   로트 ID 가 50~80자 내부 식별자라 근거 한 줄을 세 줄로 민다
 *      어디 `plans[].reasons[].text` 원문 (근거 꼬리표 `reasons[].ref` 도 로트를 든다)
 *      ⚠️ 실측 — 세 실행에서 그 괄호는 **재고 근거에만** 있다 (REH 655 · FINAL 693 ·
 *         V13 220줄 · 다른 근거 0). 그래서 `source === "재고"` 일 때만 걷는다
 *
 *   ② 「걸리는 것」 섹션 통째로      `plan.risks`
 *      왜   안마다 5~8줄이라 카드가 길고, 에이전트 문장 안에 로트 ID · 내부 설명이 있다
 *      어디 `plans[].risks` (세 실행 합계 REH 3,456 · FINAL 3,136 · V13 1,027줄)
 *
 *   ③ 안 목록 안내 블록              `plans_note`
 *      왜   실행 수 · 뺀 건수 · 보는 걷기 같은 조회 설명이라 업무가 안 읽는다
 *      어디 `plans_note.text`
 *      🔴 **안이 0개인 날에는 그 글이 「왜 안이 없나」를 말하는 유일한 자리였다** —
 *         REH-0914 23일 · V13 8일 · FINAL 0일. 예) 02-27 「가용재고 72kg 이 커버 2일
 *         수요 29kg 을 이미 덮어 이날은 매입이 필요 없다」. 그날 화면은 빈 칸이고
 *         「오늘 제안 0 안」 통계만 남는다
 *
 *   ④ 확정 매입 안내 블록            `committed_note`
 *      왜   원장 줄 수 · 뺀 줄 수 같은 조회 설명이라 표 아래 소음이다
 *      어디 `committed_note.text`
 *      ⚠️ 도착일 「—」의 이유(「못 맞춘 줄 N개는 공란」)도 여기 있었다 — 세 실행 실데이터
 *         에는 그런 줄이 0 이다
 *
 *   ⑤ 「승인은 아래 서랍에서 합니다」  확정 매입 판 꼬리말
 *      왜   화면에만 있던 안내문이다 — 서랍은 모든 탭 아래에 늘 보인다
 *      어디 **API 에는 원래 없었다** (이 파일의 글자였다)
 */

/**
 * 근거 한 줄의 화면 글자. ① 재고 근거에서 **로트 ID 괄호만** 걷는다.
 *
 * ★ 괄호 모양이 정확히 맞을 때만 걷는다 — 「(로트 LOT-…)」. 다른 괄호 · 다른 근거는
 *   원문 그대로다. 에이전트 문장을 화면에서 고쳐 쓰는 자리는 이 한 곳뿐이다.
 */
function reasonText(r: { source: string; text: string }): string {
  return r.source === "재고" ? r.text.replace(/\s*\(로트 LOT-[^)]*\)/g, "") : r.text;
}

const STATE_TONE: Record<string, Tone> = {
  //  결정이 안 난 안. 🔴 ~~기다리는 것 — 사람이 아직 할 일이 남았다~~ 낡았다 (2026-09-17).
  //  같은 요청에서 다른 안이 결정되면 「후보」여도 **기다리지 않는다** — 기다리는지는
  //  `plan.pending` 이 말하고(초록 테두리), 이 색은 낱말만 따른다.
  후보: "warn",
  //  결정이 났다
  승인됨: "good",
  //  결정 + 실제로 산 값까지 적혔다. 승인됨과 **색으로도** 갈라 둔다
  "매입 기록됨": "info",
  //  안 사기로 한 것
  반려: "bad",
};

/**
 * 칸 값 → 화면 글자. 🔴 **`null` 은 「—」다 — 0 도 빈칸도 아니다** (규칙 3).
 *
 * ★ 공용 `DataTable`(Blocks)의 `cellText` 와 **같은 규칙**이다. 확정 매입 표를 검색 · 쪽
 *   나누기 부품(`ConsoleData.Table`)으로 바꾸면서 그 규칙을 여기로 옮겼다 — 부품을 바꾸다
 *   `String(null)` 이 「null」로 찍히거나 빈칸이 되면 «못 맞췄다» 가 화면에서 사라진다.
 */
function cellText(v: Cell | undefined): string {
  if (v === null || v === undefined) return "—";
  return typeof v === "number" ? v.toLocaleString("ko-KR") : String(v);
}

/**
 * 확정 매입 표. **검색 · 10줄씩** (2026-09-17).
 *
 * ★ 부품은 운영 콘솔 공용 `ConsoleData.Table` 을 **고치지 않고** 쓴다 — 재무 · 판매 표와
 *   조작법이 같아진다. 공용 `DataTable`(Blocks)은 대시보드 · 물류가 쓰므로 안 건드린다.
 * 🔴 줄 수는 API 가 준 그대로다 — 화면은 **쪽만** 나눈다. 이번 주 매입액 · 입고 예정은
 *    API 가 전체 줄로 셌다 (FINAL-0918 09-14 · 495줄 · 111KB 를 한 번에 받는다).
 * ⚠️ 줄이 0 이면 공용 부품 대신 `DataTable` 로 그린다 — `ConsoleData.Table` 은 빈 표를
 *    「검색 조건에 맞는 항목이 없습니다」로 적는데, 그건 API 가 준 빈 표 안내와 다른 말이다.
 */
function CommittedTable({ table }: { table: ScreenTable }) {
  //  🔴 매입 번호 칸(`approval`)은 **화면에서만** 가린다 · 근거는 아래 Panel 주석
  const shown = table.columns.filter((c) => c.key !== "approval");
  if (table.rows.length === 0) return <DataTable table={{ ...table, columns: shown }} />;
  const columns: PagedColumn<Record<string, Cell>>[] = shown.map((c) => ({
    key: c.key,
    label: c.label,
    //  ★ 공용 부품은 left · right 만 안다. 확정 매입 칸에 center 는 없다
    align: c.align === "right" ? "right" : "left",
    mono: c.mono,
    render: (row) => cellText(row[c.key]),
  }));
  return (
    <>
      <PagedTable columns={columns} rows={table.rows} pageSize={10} />
      <Note note={table.note} />
    </>
  );
}

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
        <strong className="text-[18px] font-semibold">{plan.key}</strong>
        <Pill text={plan.knob} tone="info" />
        <PlanState plan={plan} />
        <span className="ml-auto text-[15.5px]" style={{ color: "var(--color-mut)" }}>
          {plan.coverage}
        </span>
      </header>

      <div className="flex flex-col gap-3.5 p-4">
        <dl className="m-0 flex flex-col gap-1.5 text-[16.5px]">
          {[
            ["사는 양", `${plan.qty_kg.toLocaleString("ko-KR")} kg`, true],
            ["예상 금액", `${plan.amount_krw.toLocaleString("ko-KR")} 원`, false],
            ["등급 단가", `${plan.unit_price.toLocaleString("ko-KR")} 원/kg · ${plan.grade}`, false],
          ].map(([k, v, hero]) => (
            <div key={String(k)} className="flex items-baseline justify-between gap-3">
              <dt className="m-0" style={{ color: "var(--color-mut)" }}>
                {k}
              </dt>
              <dd className={`tabular m-0 font-mono ${hero ? "text-[23px]" : "text-[17px]"}`}>{v}</dd>
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
            <dd className="tabular m-0 font-mono text-[18px] font-semibold">
              {plan.cut_unit_price === null
                ? "이 실행에는 기준이 없습니다"
                : `${plan.cut_unit_price.toLocaleString("ko-KR")} 원/kg`}
            </dd>
          </div>
        </dl>

        <section className="flex flex-col gap-2">
          <h3 className="m-0 text-[15.5px] font-semibold" style={{ color: "var(--color-mut)" }}>
            회차
          </h3>
          <DataTable table={plan.legs} />
        </section>

        {/*
          🔴 **지급 섹션은 나눠 사는 안(회차 2+)에서만 그린다** (2026-09-17 · 화면에서만).
             한 번에 사는 안은 지급이 한 건이고 사는 날 · 금액이 위 회차 표에 이미 있다 —
             그 자리에 빈 표와 「따로 만들지 않습니다」를 안마다 세우면 소음이다. 대신
             **아무 줄도 안 남긴다** — 회차 표가 사실을 담는다.
          ⚠️ 나눠 사는데 계획이 없는 경우는 **그대로 빈 표로** 그린다 — API 가 「나눠 사는
             안인데 지급 계획이 실리지 않았습니다」로 적는 그 자리는 진짜 물을 자리다.
          ★ API 의 `payments` 칸은 그대로다. 회차 수는 `plan.legs` 줄 수로 본다 — API 가
            빈 표 문구를 가르는 기준(`split_plan` 길이)과 같은 목록이다.
          🟡 실측 — REH-0914 · FINAL-0918 · V13 세 실행의 안이 **전부 1회차**다 (684 · 707 ·
             231안). 이 섹션은 세 실행 화면에서 **아예 안 보인다.**
        */}
        {plan.legs.rows.length >= 2 && (
          <section className="flex flex-col gap-2">
            <h3 className="m-0 text-[15.5px] font-semibold" style={{ color: "var(--color-mut)" }}>
              지급
            </h3>
            <DataTable table={plan.payments} />
          </section>
        )}

        <section className="flex flex-col gap-2">
          <h3 className="m-0 text-[15.5px] font-semibold" style={{ color: "var(--color-mut)" }}>
            근거
          </h3>
          <ul className="m-0 flex list-none flex-col gap-1.5 p-0 text-[15.5px] leading-relaxed">
            {plan.reasons.map((r, i) => (
              <li key={i} className="flex flex-wrap items-baseline gap-1.5">
                <span
                  className="shrink-0 rounded px-1.5 py-0.5 text-[14px] font-semibold"
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
                <span className="min-w-0 flex-1">{reasonText(r)}</span>
              </li>
            ))}
          </ul>
        </section>

        {/* ② 「걸리는 것」(`plan.risks`)은 그리지 않는다 — 머리 주석 「안 그리는 것」 참조 */}
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
      {/* ③ 안 목록 안내(`plans_note`)는 그리지 않는다 — 머리 주석 「안 그리는 것」 참조 */}

      {/*
        🔴 subtitle 에 «사람이» 라고 쓰지 않는다 (2026-09-10 · 마스터 통보 「백필 승인은
        사람 승인이 아닙니다」). 걷기 구간을 decided_by="AUTO-BACKFILL" 로 채우기로
        정해졌고, 그날부터 이 표에는 사람이 누른 것과 자동으로 채운 것이 같이 실린다.

        ⚠️ «승인을 거친» 은 지금도 참이고 백필 뒤에도 참이다 — 주체를 단정한 쪽만 깨진다.

        🟡 «누가 승인했나» 를 표에 칸으로 더하는 것은 다음 판이다. 원장에 그 값이 없고,
        마스터가 purchases.decision_id(FK)를 세운 뒤 조인해 읽기로 했다.
      */}
      {/* ⑤ 「승인은 아래 서랍에서 합니다」 꼬리말은 걷었다 — 머리 주석 「안 그리는 것」 참조 */}
      <Panel title="확정된 매입" subtitle="승인을 거친 뒤에 생깁니다">
        {/*
          🔴 매입 번호 칸(`approval`)을 **화면에서만** 가린다 (2026-09-17). 값은 API 에 남는다.
             잰 것 — REH-0914 08-31 · FINAL-0918 09-14 · V13 01-26 세 실행에서
             ① 줄이 (품목 · 사는 날)만으로 **겹침 0** (291 · 495 · 21줄) — 번호 없이도 줄이 갈린다
             ② 번호 44~52자 · 끝이 전부 `D1-S1` — 다른 칸에 없는 정보가 없다
             ③ 재무 API 도 `purchase_id` 를 싣지만 **재무 화면은 안 그린다** — 맞대 볼 화면이 없다
          ⚠️ 이름을 고치는 쪽(「매입 번호」)은 API 가 했다 — 칸이 원래 「승인」으로 틀려 있었다.
        */}
        <CommittedTable table={data.committed} />
        {/* ④ 확정 매입 안내(`committed_note`)는 그리지 않는다 — 머리 주석 「안 그리는 것」 참조 */}
      </Panel>
    </>
  );
}
