"use client";

/**
 * 가격 예측 탭. 소유: **ML 파트 (우리)**.
 *
 * ★ **오차를 같이 보입니다.** 예측선만 그리면 정답처럼 보입니다.
 *   1,000원짜리를 배추는 197원 틀린다는 것을 화면에 적어 둡니다.
 */

import { useState } from "react";

import {
  DataTable,
  ErrorBox,
  LineChart,
  Loading,
  Note,
  Panel,
  Pill,
  SourceTag,
  TabButtons,
} from "@/components/console/Blocks";
import { AS_OF, useTab } from "@/components/console/useTab";
import { forecast, type ForecastTab } from "@/lib/screen";

export default function ForecastPage() {
  const [item, setItem] = useState("배추");
  const { data, error } = useTab<ForecastTab>(item, () => forecast(AS_OF, item));

  if (error) return <ErrorBox message={error} />;
  if (!data) return <Loading what="가격 예측" />;

  return (
    <>
      <SourceTag sources={[data.source]} />
      <Note note={data.caveat} />

      <div className="grid gap-2.5 [grid-template-columns:repeat(auto-fit,minmax(210px,1fr))]">
        {data.cards.map((c) => (
          <button
            key={c.item}
            type="button"
            onClick={() => setItem(c.item)}
            aria-pressed={c.item === item}
            className="flex min-w-0 flex-col gap-2 rounded-xl border bg-panel px-4 py-3.5 text-left transition"
            style={{
              borderColor: c.item === item ? "var(--color-t-info)" : "var(--color-hair)",
              boxShadow: c.item === item ? "0 0 0 1px var(--color-t-info)" : undefined,
            }}
          >
            <span className="flex items-center justify-between gap-2 text-[13px] font-semibold">
              {c.item}
              <span className="flex gap-1.5">
                <Pill text={c.grade} tone="info" />
                {!c.use_recommended && <Pill text="쓰지 말 것" tone="bad" />}
                {c.review && <Pill text="검토" tone="warn" />}
              </span>
            </span>
            <span className="tabular font-mono text-[22px] leading-none">
              {c.predicted.toLocaleString("ko-KR")}
              <span className="ml-1 font-sans text-[10.5px]" style={{ color: "var(--color-mut)" }}>
                {c.unit} · {c.target_date}
              </span>
            </span>
            <span className="text-[10.5px] leading-snug" style={{ color: "var(--color-mut)" }}>
              구간 {c.lower.toLocaleString("ko-KR")}–{c.upper.toLocaleString("ko-KR")} · 폭 {c.ci_width}
              {c.spec && (
                <>
                  <br />
                  {c.spec}
                </>
              )}
            </span>
          </button>
        ))}
      </div>

      <Panel
        title={`${data.selected} 경락가`}
        subtitle="실측 · 전일 예측 · 내일 예측 구간"
        right={
          <TabButtons
            items={data.items.map((i) => ({ key: i, label: i }))}
            value={data.selected}
            onChange={setItem}
          />
        }
      >
        <LineChart
          chart={data.chart}
          days={data.axis.days}
          asOfIndex={data.axis.as_of_index}
          height={300}
        />
      </Panel>

      <div className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(380px,1fr))]">
        <Panel title="우리가 얼마나 틀리나" subtitle="봉인 개봉 2026-09-01 실측">
          <DataTable table={data.accuracy} />
        </Panel>
        <Panel title="어느 조합을 써도 되나" subtitle="모델이 «어제 가격 그대로» 를 이기는가">
          <DataTable table={data.quality} />
        </Panel>
      </div>
    </>
  );
}
