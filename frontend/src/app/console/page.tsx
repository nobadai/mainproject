"use client";

/**
 * 대시보드 — 다섯 부서 값을 **날짜 축에 놓기만** 한다.
 *
 * 소유: 마스터. 여기서 숫자를 만들지 마세요 — 같은 값을 두 군데서 계산하면
 * 언젠가 갈라지고, 그러면 어느 쪽이 맞는지 아무도 모릅니다.
 */

import { useSyncExternalStore } from "react";

import {
  Badges,
  DataTable,
  ErrorBox,
  LineChart,
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
import { dashboard, type DashboardTab } from "@/lib/screen";

export default function DashboardPage() {
  const asOf = useSyncExternalStore(subscribeAsOf, asOfSnapshot, serverAsOf);
  const { data, error } = useTab<DashboardTab>(asOf, () => dashboard(asOf));

  if (error) return <ErrorBox message={error} />;
  if (!data) return <Loading what="대시보드" />;

  return (
    <>
      <div className="flex flex-wrap items-center gap-2.5">
        <Badges items={data.badges} />
        <span className="ml-auto font-mono text-[15.5px]" style={{ color: "var(--color-mut2)" }}>
          기준일 {data.axis.as_of}
        </span>
      </div>

      <SourceTag sources={data.sources} />
      {/* ★ **맨 위 배추 예측 칸만 뺍니다** (2026-09-16 · 화면에서만).
             같은 값이 바로 아래 「가격 예측」 판의 배추 카드에 또 있습니다 —
             한 화면에 같은 숫자가 둘이면 사람이 «둘이 다른 값인가» 를 먼저
             확인해야 합니다. 나머지 네 칸(운영 여유 · 현재고 · 승인 대기 ·
             판매금액)은 그대로 둡니다.
             **서버는 안 건드립니다** — 다시 보이려면 이 한 줄을 지우면 됩니다.
             라벨이 «배추 특급 · 08-31 예측» 처럼 날짜가 박혀 있어(실측
             2026-09-16) 앞뒤 두 군데를 같이 봅니다. */}
      <StatRow
        items={data.stats.filter((s) => !(s.label.startsWith("배추") && s.label.endsWith("예측")))}
      />

      <div className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(340px,1fr))]">
        <Panel title="가격 예측" subtitle="세 품목 · 경락가 특등급 · 모델이 낸 첫 날">
          <div className="grid gap-2.5 [grid-template-columns:repeat(auto-fit,minmax(150px,1fr))]">
            {data.forecast_cards.map((c) => {
              const at = ((c.predicted - c.lower) / Math.max(1, c.upper - c.lower)) * 100;
              return (
                <div
                  key={c.item}
                  className="flex min-w-0 flex-col gap-2 rounded-xl border px-3.5 py-3"
                  style={{ borderColor: "var(--color-hair)" }}
                >
                  <span className="flex items-center justify-between gap-2 text-[16.5px] font-semibold">
                    {c.item}
                    <Pill text={c.grade} tone="info" />
                  </span>
                  <span className="tabular font-mono text-[24px] leading-none">
                    {c.predicted.toLocaleString("ko-KR")}
                    <span className="ml-1 font-sans text-[14.5px]" style={{ color: "var(--color-mut)" }}>
                      {c.unit} · {c.target_date.slice(5)}
                    </span>
                  </span>
                  {/* 구간 안에서 가운데 값이 어디쯤인가 */}
                  <div className="relative h-[5px] rounded-full" style={{ background: "var(--color-band)" }}>
                    <i
                      aria-hidden
                      className="absolute -top-[3px] h-[11px] w-0.5"
                      style={{ left: `${Math.min(100, Math.max(0, at))}%`, background: "var(--color-t-info)" }}
                    />
                  </div>
                  <span className="text-[14.5px]" style={{ color: "var(--color-mut)" }}>
                    구간 {c.lower.toLocaleString("ko-KR")}–{c.upper.toLocaleString("ko-KR")} · 폭{" "}
                    {c.ci_width}
                    {c.review && " · 검토"}
                  </span>
                </div>
              );
            })}
          </div>
        </Panel>

        <Panel title="오늘의 매입 제안" subtitle="사람이 골라야 확정됩니다">
          <DataTable table={data.purchase} />
          <Note note={data.purchase_note} />
        </Panel>
      </div>

      <div className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(400px,1fr))]">
        <Panel title="현금이 어떻게 움직이나" subtitle="재무 일마감 현금 · 대출 제외/포함">
          <LineChart
            chart={data.cash_chart}
            days={data.axis.days}
            asOfIndex={data.axis.as_of_index}
            height={238}
          />
        </Panel>
        <Panel title="창고 재고" subtitle="보유 · 운송 중 · 추정">
          <LineChart
            chart={data.stock_chart}
            days={data.axis.days}
            asOfIndex={data.axis.as_of_index}
            height={238}
          />
        </Panel>
      </div>
    </>
  );
}
