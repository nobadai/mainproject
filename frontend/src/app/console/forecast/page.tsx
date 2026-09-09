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
 *
 * ★ **위쪽 탭 넷이 우리 파트 화면 전부입니다.**
 *
 *       가격 예측        오늘 밤 경매부터 18일치 · 점을 누르면 이유
 *       배치 현황        매일 아침 9시에 도는 것이 잘 돌았나
 *       AI 보고서        데이터 이상 · 오늘 뉴스 · 지난 기록
 *       모델 재학습  다시 배워야 하나 · 바꿀지 말지
 *
 *   **셋은 열 때 부릅니다.** 데이터 이상 점검은 10초, 뉴스는 30초 걸립니다 —
 *   가격 예측을 보러 온 사람을 기다리게 하면 안 됩니다.
 */

import { useEffect, useState, useSyncExternalStore } from "react";

import { AgentsTab } from "@/components/console/ml/AgentsTab";
import { BatchTab } from "@/components/console/ml/BatchTab";
import { ExplainPopup } from "@/components/console/ml/ExplainPopup";
import { ForecastChart, type ChartRow } from "@/components/console/ml/ForecastChart";
import { RetrainTab } from "@/components/console/ml/RetrainTab";
import {
  DataTable,
  ErrorBox,
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
import { retrainPending } from "@/lib/mlConsole";
import { forecast, type ForecastTab } from "@/lib/screen";

function ForecastPane() {
  const [item, setItem] = useState("배추");
  const [kind, setKind] = useState("auc");
  const [baseDt, setBaseDt] = useState<string | undefined>(undefined);
  //  ★ **기본은 꺼 둡니다.** 예측 시점에는 정답이 없습니다 — 켜 두면
  //    «맞았네/틀렸네» 를 먼저 보게 되고, 그건 그날 알 수 있던 것이 아닙니다.
  //    되짚어 볼 때만 켭니다.
  const [showActual, setShowActual] = useState(false);
  //  ★ 점을 누르면 그 날짜의 «왜 이렇게 예측했나» 가 팝업으로 뜹니다.
  //    숫자만 보이면 믿을지 말지를 정할 수가 없습니다.
  const [picked, setPicked] = useState<ChartRow | null>(null);

  const asOf = useSyncExternalStore(subscribeAsOf, asOfSnapshot, serverAsOf);
  const { data, error } = useTab<ForecastTab>(
    `${asOf}|${kind}|${item}|${baseDt ?? ""}`,
    () => forecast(asOf, item, kind, baseDt),
  );

  if (error) return <ErrorBox message={error} />;
  if (!data) return <Loading what="가격 예측" />;

  const role = data.kinds.find((k) => k.kind === data.selected_kind)?.role;
  const latest = data.base_dates[0]?.base_dt;
  const unit = data.cards[0]?.unit ?? "원/kg";
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
            기준일 (이 예측을 만든 날)
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
                {d.scored > 0 ? ` · ${d.scored}/${d.total}건 확인됨` : " · 아직 결과 안 남"}
                {d.pre_fix && "  ⚠ 예전 방식"}
              </option>
            ))}
          </select>
          <span className="text-[10.5px]" style={{ color: "var(--color-mut2)" }}>
            지나간 날짜는 다시보기와 시연용입니다
            {latest && data.selected_base_dt !== latest && (
              <button
                type="button"
                onClick={() => setBaseDt(latest)}
                className="ml-1.5 rounded px-1.5 py-0.5 text-[10.5px]"
                style={{ background: "var(--color-t-good-bg)", color: "var(--color-t-good)" }}
              >
                최신 날짜로
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
            실제 가격
          </span>
          <button
            type="button"
            onClick={() => setShowActual((v) => !v)}
            title={
              showActual
                ? "끄면 실제 업무 화면처럼 보입니다 — 예측을 만든 날에는 진짜 정답을 알 수 없습니다"
                : "켜면 지난 날짜의 진짜 가격도 함께 그립니다 (시연할 때 씁니다)"
            }
            className="rounded-lg px-3 py-1.5 text-[12.5px] font-medium"
            style={
              showActual
                ? { background: "var(--color-t-warn-bg)", color: "var(--color-t-warn)" }
                : { background: "var(--color-sunk)", color: "var(--color-mut)" }
            }
          >
            {showActual ? "보이기 (시연용)" : "숨기기 (실무용)"}
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
            text: `기준일 목록이 ${data.base_dates.length}개까지만 나옵니다 — 더 오래된 날짜도 있습니다.`,
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
                {c.gated && <Pill text="어제 가격" tone="neutral" />}
                {!c.use_recommended && <Pill text="쓰지 마세요" tone="bad" />}
                {c.review && <Pill text="확인 필요" tone="warn" />}
              </span>
            </span>
            <span className="tabular font-mono text-[22px] leading-none">
              {c.predicted.toLocaleString("ko-KR")}
              <span className="ml-1 font-sans text-[10.5px]" style={{ color: "var(--color-mut)" }}>
                {c.unit} · {c.target_date}
              </span>
            </span>
            <span className="text-[10.5px] leading-snug" style={{ color: "var(--color-mut)" }}>
              예상 구간 {c.lower.toLocaleString("ko-KR")}~{c.upper.toLocaleString("ko-KR")} · 폭{" "}
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
            ? `기준일 ${data.selected_base_dt} · ${data.gate_lead}일 뒤 미만은 모델 예측을 쓰지 않습니다`
            : `기준일 ${data.selected_base_dt} · 가장 왼쪽 시점은 그날 밤 경매입니다`
        }
      >
        <ForecastChart
          rows={data.points}
          unit={unit}
          gateLead={data.gate_lead}
          showActual={showActual}
          picked={picked?.lead ?? null}
          onPick={(row) => setPicked((cur) => (cur?.lead === row.lead ? null : row))}
        />
        {data.quality_note && (
          <p
            className="m-0 border-t pt-2.5 text-[11.5px] leading-relaxed"
            style={{ borderColor: "var(--color-hair-soft)", color: "var(--color-mut)" }}
          >
            <b className="font-semibold">이렇게 진단한 이유 </b>
            {data.quality_note}
          </p>
        )}
      </Panel>

      {/* ── 리드타임별 표 ─────────────────────────────────────────── */}
      <Panel
        title="하루씩 나눠보기"
        subtitle="0일 뒤는 기준일 당일입니다 · 어떤 값이 모델 출력이고 어떤 값이 차단되었는지 보여줍니다"
      >
        <DataTable table={rows} />
      </Panel>

      {picked && (
        <ExplainPopup
          //  ★ 다른 점을 누르면 팝업을 **새로 답니다.** 그래야 옛 설명이
          //    잠깐 남아 있다가 바뀌는 일이 없습니다.
          key={`${data.selected_base_dt}|${data.selected}|${data.selected_kind}|${picked.lead}`}
          baseDt={data.selected_base_dt}
          item={data.selected}
          kind={data.selected_kind as "auc" | "whsl" | "rtl"}
          lead={picked.lead}
          targetDate={picked.target_dt}
          showActual={showActual}
          onClose={() => setPicked(null)}
        />
      )}
    </>
  );
}

/* ── 위쪽 탭 ─────────────────────────────────────────────────────────── */

const PANES = [
  { key: "forecast", label: "가격 예측" },
  { key: "batch", label: "배치 현황" },
  { key: "agents", label: "AI 보고서" },
  { key: "retrain", label: "모델 재학습" },
] as const;

type Pane = (typeof PANES)[number]["key"];

/**
 * 탭 하나. **오른쪽 위에 빨간 뱃지**를 달 수 있습니다.
 *
 * ★ 공용 `TabButtons` 를 안 쓰고 여기서 따로 그립니다. 뱃지를 붙이려면
 *   공용 부품을 고쳐야 하는데, 그건 다른 파트도 같이 쓰는 것입니다.
 */
function Tab({
  label,
  on,
  badge,
  onClick,
}: {
  label: string;
  on: boolean;
  badge?: number;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={on}
      className="relative rounded-lg border px-3 py-1.5 text-[12px] font-medium transition"
      style={{
        borderColor: on ? "var(--color-nav)" : "var(--color-hair)",
        background: on ? "var(--color-nav)" : "var(--color-panel)",
        color: on ? "#f4f3ee" : "var(--color-ink2)",
      }}
    >
      {label}
      {badge ? (
        //  ★ 눈에 띄어야 합니다. 이 탭은 **평소에 아예 없다가** 사람이
        //    결정할 것이 생겼을 때만 나타납니다. 나타난 것을 못 보면
        //    지금과 똑같아집니다.
        <span
          aria-label={`결정할 것 ${badge}건`}
          className="absolute -right-1.5 -top-1.5 flex h-4 min-w-4 items-center justify-center rounded-full px-1 text-[10px] font-bold leading-none"
          style={{ background: "var(--color-t-bad)", color: "#fff" }}
        >
          {badge}
        </span>
      ) : null}
    </button>
  );
}

export default function ForecastPage() {
  const [pane, setPane] = useState<Pane>("forecast");
  //  ★ 사람이 눌러야 할 재학습 결정 수. **탭에 빨간 뱃지로만** 씁니다.
  //    후보가 현행보다 나을 때만 셉니다 — 못하면 배치가 후보를 지우고
  //    아무것도 안 남깁니다.
  const [waiting, setWaiting] = useState(0);

  useEffect(() => {
    let alive = true;
    retrainPending()
      .then((r) => alive && setWaiting(r.pending.length))
      //  ★ 못 물어봤으면 뱃지를 안 답니다. 탭은 그대로 있으니 사람이
      //    들어가서 직접 볼 수 있습니다.
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, []);

  //  ★ 탭은 **늘 있습니다** (2026-09-09 다시 바꿈). 없다가 생기니 사람이
  //    「어디로 들어가야 하나」 를 몰랐습니다. 갈리는 것은 탭 안입니다 —
  //    바꿀 것이 있으면 비교표와 버튼, 없으면 «필요 없습니다» 한 줄.
  const here = pane;

  return (
    <>
      <div className="flex flex-wrap gap-1.5">
        {PANES.map((p) => (
          <Tab
            key={p.key}
            label={p.label}
            on={p.key === here}
            badge={p.key === "retrain" ? waiting : undefined}
            onClick={() => setPane(p.key)}
          />
        ))}
      </div>

      {/*  ★ 고른 판만 그립니다. 넷을 다 그려 두고 숨기면 배치·데이터 이상·뉴스를
             매번 같이 불러 가격 예측이 느려집니다. */}
      {here === "forecast" && <ForecastPane />}
      {here === "batch" && <BatchTab />}
      {here === "agents" && <AgentsTab />}
      {here === "retrain" && <RetrainTab />}
    </>
  );
}
