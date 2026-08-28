"use client";

import { SCENES } from "@/lib/fixtures";
import { n } from "@/lib/format";
import type { ViewModel } from "@/lib/types";

export type StageState = "idle" | "run" | "done";

const STAGES = [
  { t: "재무 · 물류 경계 조회", who: "finance / inventory · PRE_PURCHASE" },
  { t: "ML 가격 예측", who: "ml · 일 1회 4품목" },
  { t: "오케스트레이터 조립", who: "master · AgentRequest" },
  { t: "매입 판단", who: "purchase · GENERATE_SCENARIOS" },
] as const;

const num = (v: unknown) => (typeof v === "number" ? n(v) : "—");

/**
 * 각 단계가 무엇을 받았는가.
 *
 * ★ 값이 없으면 **"—"** 로 둔다. 0 으로 채우면 "받았는데 0"과 "못 받았다"가
 *   구분되지 않는다 (정의서 §1.2-10 · 도메인 규칙 3).
 */
function stageRows(asOf: string, vm: ViewModel | null): [string, string][][] {
  const scene = SCENES[asOf];
  const fc = scene.input.forecast as {
    generated_at?: string;
    current_price?: number;
    horizon_days?: number;
    model_version?: string;
    daily?: { predicted: number; lower: number; upper: number }[];
  };
  const orders = scene.input.confirmed_orders as { total_kg?: number; orders?: unknown[] };
  const policy = scene.input.policy_values as { contract_price_krw?: number };
  const d14 = fc.daily?.[13];
  const ci = d14 ? (d14.upper - d14.lower) / d14.predicted : null;

  const fin = vm?.boundary.finance ?? {};
  const inv = vm?.boundary.inventory ?? {};

  return [
    [
      ["재무 · 매입 상한", fin.finance_cap_amount_krw != null ? `${num(fin.finance_cap_amount_krw)}원` : "—"],
      ["재무 · 지급조건 N5", fin.purchase_payment_days != null ? `${fin.purchase_payment_days}일 후 지급` : "—"],
      ["재무 · 현금 하한", fin.base_projected_cash_min != null ? `${num(fin.base_projected_cash_min)}원` : "—"],
      ["물류 · 창고 여유", inv.warehouse_free_kg != null ? `${num(inv.warehouse_free_kg)}kg` : "—"],
      ["물류 · 임차 상한", inv.rental_cap_kg != null ? `${num(inv.rental_cap_kg)}kg` : "—"],
      ["물류 · 보유 로트", inv.lot_count != null ? `${inv.lot_count}건` : Array.isArray(inv.lots) ? `${inv.lots.length}건` : "—"],
    ],
    [
      ["모델", `${fc.model_version} · 지평 ${fc.horizon_days}일`],
      ["생성 시각", fc.generated_at ?? "—"],
      ["당일 경락가", `${num(fc.current_price)}원/kg`],
      ["D+14 예측", d14 ? `${n(d14.predicted)}원/kg  (${n(d14.lower)} ~ ${n(d14.upper)})` : "—"],
      ["신뢰구간 폭", ci != null ? `${(ci * 100).toFixed(1)}%  · 임계 8.0%` : "—"],
    ],
    [
      ["봉투 키", "item · forecast · confirmed_orders · policy_values"],
      ["as_of 대조", `요청 ${asOf}`],
      ["확정주문", `${num(orders.total_kg)}kg / 14일 · ${orders.orders?.length ?? 0}건`],
      ["계약 단가", `${num(policy.contract_price_krw)}원/kg`],
    ],
    [
      ["모드", "GENERATE_SCENARIOS"],
      ["사용 Tool", vm && vm.usedTools.length > 0 ? vm.usedTools.join(" → ") : "—"],
      ["종료 코드", vm?.endCode ?? (vm ? "표본 (호출 결과 아님)" : "—")],
      ["산출", vm ? `${vm.scenarios.length}안 · 미수신 ${vm.missingData.length}건` : "—"],
    ],
  ];
}

export function RunScreen({
  asOf,
  onAsOf,
  onRun,
  onReset,
  states,
  vm,
  busy,
  status,
}: {
  asOf: string;
  onAsOf: (d: string) => void;
  onRun: () => void;
  onReset: () => void;
  states: StageState[];
  vm: ViewModel | null;
  busy: boolean;
  status: string;
}) {
  const rows = stageRows(asOf, vm);
  const done = states.every((s) => s === "done");

  return (
    <div className="grid items-start gap-7 lg:grid-cols-[minmax(0,380px)_minmax(0,1fr)]">
      <div className="rounded-card border border-rule bg-surface">
        <div className="border-b border-rule px-6 py-5">
          <h2 className="text-lg2 font-semibold">실행 조건</h2>
        </div>
        <div className="p-6">
          <div className="mb-6 flex flex-col gap-2">
            <label className="lbl" htmlFor="sel-date">
              기준일 · as_of
            </label>
            <select
              id="sel-date"
              value={asOf}
              disabled={busy}
              onChange={(e) => onAsOf(e.target.value)}
              className="w-full rounded-ctl border border-rule-2 bg-surface-2 px-4 py-3 font-mono text-md2 text-ink disabled:opacity-50"
            >
              {Object.keys(SCENES).map((d) => (
                <option key={d} value={d}>
                  {d} · {SCENES[d].blurb}
                </option>
              ))}
            </select>
            <span className="text-sm2 leading-relaxed text-ink-3">
              모든 조회가 이 시점으로 잘린다. 오늘 날짜를 쓰지 않고 항상 주입한다.
            </span>
          </div>

          <div className="mb-6 flex flex-col gap-2">
            <label className="lbl" htmlFor="sel-item">
              품목
            </label>
            <select
              id="sel-item"
              defaultValue="배추"
              className="w-full rounded-ctl border border-rule-2 bg-surface-2 px-4 py-3 font-mono text-md2 text-ink"
            >
              <option value="배추">배추</option>
              <option value="무" disabled>
                무 (예측 미적재)
              </option>
              <option value="양파" disabled>
                양파 (예측 미적재)
              </option>
              <option value="피마늘" disabled>
                피마늘 (예측 미적재)
              </option>
            </select>
          </div>

          <button
            type="button"
            onClick={onRun}
            disabled={busy}
            className="w-full rounded-ctl bg-accent px-4 py-4 text-lg2 font-semibold tracking-tight text-on-accent disabled:opacity-45"
          >
            {busy ? "실행 중…" : "매입 시나리오 생성"}
          </button>
          {done && !busy && (
            <button
              type="button"
              onClick={onReset}
              className="mt-3 w-full rounded-ctl border border-rule-2 bg-surface-2 px-4 py-4 text-md2 font-normal text-ink"
            >
              다시 실행
            </button>
          )}
        </div>
      </div>

      <div className="rounded-card border border-rule bg-surface">
        <div className="flex flex-wrap items-center gap-4 border-b border-rule px-6 py-5">
          <h2 className="text-lg2 font-semibold">실행 경로</h2>
          <span className="lbl">{status}</span>
        </div>
        <div className="p-6">
          <div className="relative pl-10">
            <span className="absolute top-4 bottom-5 left-[14px] w-px bg-rule" />
            {STAGES.map((st, i) => {
              const state = states[i];
              return (
                <div key={st.t} className="relative pb-7 last:pb-1">
                  <span
                    className={`absolute top-1 -left-10 grid size-7 place-items-center rounded-full border font-mono text-xs2 font-semibold ${
                      state === "done"
                        ? "border-accent bg-accent text-on-accent"
                        : state === "run"
                          ? "pulse-ring border-accent bg-surface text-accent"
                          : "border-rule bg-surface text-ink-3"
                    }`}
                  >
                    {state === "done" ? "✓" : i + 1}
                  </span>
                  <div className="flex flex-wrap items-baseline gap-3">
                    <h3
                      className={`text-lg2 font-semibold ${state === "idle" ? "text-ink-3" : ""}`}
                    >
                      {st.t}
                    </h3>
                    <span className="rounded-ctl border border-rule px-2 py-1 font-mono text-xs2 text-ink-3">
                      {st.who}
                    </span>
                    <span className="ml-auto font-mono text-xs2 text-ink-3">
                      {state === "done" ? "완료" : state === "run" ? "조회 중" : "대기"}
                    </span>
                  </div>
                  <div
                    className={`mt-3 overflow-hidden rounded-card border border-rule bg-surface-2 ${
                      state === "idle" ? "opacity-50" : ""
                    }`}
                  >
                    <div className="grid grid-cols-[auto_1fr]">
                      {rows[i].map(([k, v], ri) => (
                        <div key={k} className="contents">
                          <div
                            className={`border-r border-rule bg-surface px-4 py-3 text-sm2 whitespace-nowrap text-ink-2 ${
                              ri < rows[i].length - 1 ? "border-b" : ""
                            }`}
                          >
                            {k}
                          </div>
                          <div
                            className={`num px-4 py-3 text-sm2 ${
                              ri < rows[i].length - 1 ? "border-b border-rule" : ""
                            } ${state === "done" ? "" : "text-ink-3"}`}
                          >
                            {state === "run" ? (
                              <span
                                className="skeleton block h-3 rounded-ctl bg-surface-3"
                                style={{ width: `${45 + ((ri * 17) % 40)}%` }}
                              />
                            ) : state === "done" ? (
                              v
                            ) : (
                              "—"
                            )}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}
