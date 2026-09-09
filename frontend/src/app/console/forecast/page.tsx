"use client";

/**
 * 가격 예측 탭. 소유: **ML 파트 (우리)**.
 *
 * ★ **우리가 쓰던 화면(`localhost:3100`)의 예측 탭을 옮겨 왔습니다.**
 *   가격종류 셋 · 기준일 고르기 · 채점 상태 · 옛 기준 경고 · 18일 전체 ·
 *   실제값 겹치기 · 기준일 그날(리드 0)부터 · 리드타임별 표.
 *
 * ★ **오차를 같이 보입니다.** 숫자 하나만 크게 띄우면 틀린 줄 모르고 씁니다.
 *   1,000원짜리를 배추는 197원 틀립니다.
 */

import { useState, useSyncExternalStore } from "react";

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
import { useTab } from "@/components/console/useTab";
//  🔴 시연용 기준일 (`#431`). 시연이 끝나면 이 줄과 아래 `asOf` 를 지우고
//     `useTab` 의 `AS_OF` 로 되돌린다.
import { asOfSnapshot, serverAsOf, subscribeAsOf } from "@/lib/demo_as_of";
import { forecast, type ForecastTab } from "@/lib/screen";

export default function ForecastPage() {
  const [item, setItem] = useState("배추");
  const [kind, setKind] = useState("auc");
  const [baseDt, setBaseDt] = useState<string | undefined>(undefined);
  //  ★ **기본은 꺼 둡니다.** 예측 시점에는 정답이 없습니다 — 켜 두면
  //    «맞았네/틀렸네» 를 먼저 보게 되고, 그건 그날 알 수 있던 것이 아닙니다.
  //    되짚어 볼 때만 켭니다.
  const [showActual, setShowActual] = useState(false);

  const asOf = useSyncExternalStore(subscribeAsOf, asOfSnapshot, serverAsOf);
  const { data, error } = useTab<ForecastTab>(
    `${asOf}|${kind}|${item}|${baseDt ?? ""}`,
    () => forecast(asOf, item, kind, baseDt),
  );

  if (error) return <ErrorBox message={error} />;
  if (!data) return <Loading what="가격 예측" />;

  const role = data.kinds.find((k) => k.kind === data.selected_kind)?.role;
  const latest = data.base_dates[0]?.base_dt;
  const chart = showActual
    ? data.chart
    : { ...data.chart, series: data.chart.series.filter((s) => s.name !== "실제") };
  const rows = showActual
    ? data.rows
    : {
        ...data.rows,
        columns: data.rows.columns.filter((c) => c.key !== "actual" && c.key !== "err"),
      };

  return (
    <>
      {/* ── 고르는 줄 ─────────────────────────────────────────────── */}
      <div
        className="flex flex-wrap items-end gap-4 rounded-xl border bg-panel px-4 py-3.5"
        style={{ borderColor: "var(--color-hair)" }}
      >
        <label className="flex flex-col gap-1.5">
          <span className="text-[11px] font-semibold" style={{ color: "var(--color-mut)" }}>
            기준일 (예측을 만든 날)
          </span>
          <select
            value={data.selected_base_dt}
            onChange={(e) => setBaseDt(e.target.value)}
            className="tabular rounded-lg border px-2.5 py-1.5 font-mono text-[13px]"
            style={{ borderColor: "var(--color-hair)", background: "var(--color-panel)" }}
          >
            {data.base_dates.map((d) => (
              <option key={d.base_dt} value={d.base_dt}>
                {d.base_dt}
                {/*  「채점」 은 예측 몇 개가 실제 가격과 맞춰졌나다.
                    기준일이 오래될수록 대상일이 지나 늘어난다. */}
                {d.scored > 0 ? ` · 결과 확인 ${d.scored}/${d.total}건` : " · 아직 결과 안 나옴"}
                {d.pre_fix && "  ⚠ 옛 기준"}
              </option>
            ))}
          </select>
          <span className="text-[10.5px]" style={{ color: "var(--color-mut2)" }}>
            지난 날짜는 시연·되짚기용입니다
            {latest && data.selected_base_dt !== latest && (
              <button
                type="button"
                onClick={() => setBaseDt(latest)}
                className="ml-1.5 rounded px-1.5 py-0.5 text-[10.5px]"
                style={{ background: "var(--color-t-good-bg)", color: "var(--color-t-good)" }}
              >
                최신으로
              </button>
            )}
          </span>
        </label>

        <label className="flex flex-col gap-1.5">
          <span className="text-[11px] font-semibold" style={{ color: "var(--color-mut)" }}>
            가격 종류
          </span>
          <TabButtons
            items={data.kinds.map((k) => ({ key: k.kind, label: k.label }))}
            value={data.selected_kind}
            onChange={(k) => setKind(k)}
          />
        </label>

        <label className="flex flex-col gap-1.5">
          <span className="text-[11px] font-semibold" style={{ color: "var(--color-mut)" }}>
            품목
          </span>
          <TabButtons
            items={data.items.map((i) => ({ key: i, label: i }))}
            value={data.selected}
            onChange={setItem}
          />
        </label>

        <label className="flex flex-col gap-1.5">
          <span className="text-[11px] font-semibold" style={{ color: "var(--color-mut)" }}>
            실제값
          </span>
          <button
            type="button"
            onClick={() => setShowActual((v) => !v)}
            title={
              showActual
                ? "끄면 운영에서 보이는 모습이 됩니다 — 예측 시점에는 정답이 없습니다"
                : "켜면 지난 날짜의 실제 가격을 함께 그립니다 (시연용)"
            }
            className="rounded-lg px-3 py-1.5 text-[12.5px] font-medium"
            style={
              showActual
                ? { background: "var(--color-t-warn-bg)", color: "var(--color-t-warn)" }
                : { background: "var(--color-sunk)", color: "var(--color-mut)" }
            }
          >
            {showActual ? "보임 (시연)" : "숨김 (운영)"}
          </button>
        </label>

        {role && (
          <p
            className="m-0 ml-auto max-w-[280px] text-[11.5px] leading-snug"
            style={{ color: "var(--color-mut)" }}
          >
            {role}
          </p>
        )}
      </div>

      <SourceTag sources={[data.source]} />

      {/* ★ 옛 기준으로 만든 예측이면 왜 다른지 알려준다. 안 알려주면 다른
             날과 나란히 놓고 「예측이 들쭉날쭉하다」 로 읽는다. */}
      <Note note={data.notice} />

      {data.base_dates_truncated && (
        <Note
          note={{
            tone: "info",
            text: `기준일 목록이 ${data.base_dates.length}개에서 잘렸습니다 — 더 옛날 것도 있습니다.`,
          }}
        />
      )}

      {/* ── 세 품목 카드 ──────────────────────────────────────────── */}
      <div className="grid gap-2.5 [grid-template-columns:repeat(auto-fit,minmax(220px,1fr))]">
        {data.cards.map((c) => (
          <button
            key={c.item}
            type="button"
            onClick={() => setItem(c.item)}
            aria-pressed={c.item === data.selected}
            className="flex min-w-0 flex-col gap-2 rounded-xl border bg-panel px-4 py-3.5 text-left transition"
            style={{
              borderColor: c.item === data.selected ? "var(--color-t-info)" : "var(--color-hair)",
              boxShadow: c.item === data.selected ? "0 0 0 1px var(--color-t-info)" : undefined,
            }}
          >
            <span className="flex flex-wrap items-center gap-1.5 text-[13px] font-semibold">
              {c.item}
              <span className="ml-auto flex flex-wrap gap-1.5">
                <Pill text={c.grade} tone="info" />
                {c.gated && <Pill text="어제값" tone="neutral" />}
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
              구간 {c.lower.toLocaleString("ko-KR")}–{c.upper.toLocaleString("ko-KR")} · 폭{" "}
              {c.ci_width}
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

      {/* ── 18일 그래프 ───────────────────────────────────────────── */}
      <Panel
        title={data.chart.label}
        subtitle={
          //  ★ 리드타임 게이트는 2026-09-09 에 껐습니다. 그 전에는 여기에
          //    「리드 3 미만은 모델을 안 씁니다」 가 떴습니다.
          data.gate_lead > 0
            ? `기준일 ${data.selected_base_dt} · 리드타임 ${data.gate_lead} 미만은 모델을 안 씁니다`
            : `기준일 ${data.selected_base_dt} · 맨 왼쪽 칸이 그날 밤 경매입니다`
        }
      >
        <LineChart chart={chart} days={data.axis.days} asOfIndex={0} height={320} />
        {data.quality_note && (
          <p
            className="m-0 border-t pt-2.5 text-[11.5px] leading-relaxed"
            style={{ borderColor: "var(--color-hair-soft)", color: "var(--color-mut)" }}
          >
            <b className="font-semibold">판정 근거 </b>
            {data.quality_note}
          </p>
        )}
      </Panel>

      {/* ── 리드타임별 표 ─────────────────────────────────────────── */}
      <Panel
        title="하루씩 뜯어보기"
        subtitle="리드 0 이 기준일 그날 · 어느 값이 모델이고 어느 값이 차단된 값인가"
      >
        <DataTable table={rows} />
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
