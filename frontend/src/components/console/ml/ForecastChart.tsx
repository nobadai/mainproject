"use client";

/**
 * 예측 곡선 — 우리 ML 콘솔(`localhost:3100`)의 그래프를 그대로 옮겼습니다.
 *
 * 라이브러리를 안 쓰고 SVG 로 직접 그립니다. 선 하나 그리자고 차트
 * 라이브러리를 받으면 화면이 무거워지고, 무엇보다 **그 라이브러리가 값을
 * 어떻게 다루는지 우리가 모르게 됩니다.** 없는 값을 0 으로 이어 그리는
 * 라이브러리가 흔합니다.
 *
 * ★ **없는 값은 선을 끊습니다.** 채점 안 된 날은 실제값이 없습니다. 그걸
 *   0 으로 내리거나 앞뒤를 이어버리면 «가격이 떨어졌다» 로 읽힙니다.
 *
 * ★ **점을 누르면 «왜 이렇게 봤나» 가 뜹니다.** 숫자만 보이면 믿을지 말지를
 *   정할 수가 없습니다.
 */

export interface ChartRow {
  lead: number;
  target_dt: string;
  pred: number | null;
  lo: number | null;
  hi: number | null;
  actual: number | null;
  err_pct?: number | null;
  anchor: number | null;
  gated?: boolean;
}

const W = 760;
const H = 300;
const PAD = { top: 18, right: 18, bottom: 38, left: 56 };

/** 눈금을 사람이 읽기 좋은 자리에 놓는다 (10 · 20 · 25 · 50 의 배수). */
function niceTicks(lo: number, hi: number, n = 4): number[] {
  if (!isFinite(lo) || !isFinite(hi) || hi <= lo) return [lo];
  const raw = (hi - lo) / n;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? mag * 10;
  const out: number[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) out.push(v);
  return out;
}

export function ForecastChart({
  rows,
  unit,
  gateLead = 0,
  origin,
  picked,
  onPick,
  showActual = true,
}: {
  rows: ChartRow[];
  unit: string;
  /** 이 리드타임 미만은 모델을 안 쓴다 — 배경을 다르게 칠해 구분한다. 0 이면 없음. */
  gateLead?: number;
  /**
   * 기준일 앞에 놓는 출발점.
   *
   * ★ 리드 0 이 생긴 뒤로는 기준일 자리에 **진짜 예측**이 들어가므로 보통
   *   안 씁니다. 리드 1 부터 시작하는 옛 기준일을 볼 때만 쓰입니다.
   */
  origin?: { date: string; value: number } | null;
  /** 지금 설명이 열려 있는 리드타임. 점을 크게 그려 표시한다. */
  picked?: number | null;
  /** 점을 누르면 그 리드타임을 알린다. 화면이 설명을 띄운다. */
  onPick?: (row: ChartRow) => void;
  showActual?: boolean;
}) {
  if (rows.length === 0) return null;

  const hasOrigin = !!origin;
  const L0 = rows[0].lead - 1;

  const values = rows.flatMap((r) =>
    [r.lo, r.hi, r.pred, showActual ? r.actual : null, r.anchor].filter(
      (v): v is number => v !== null && v !== undefined,
    ),
  );
  if (origin) values.push(origin.value);
  if (values.length === 0) return null;

  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const pad = (hi - lo) * 0.08 || 1;
  const yLo = lo - pad;
  const yHi = hi + pad;

  const iw = W - PAD.left - PAD.right;
  const ih = H - PAD.top - PAD.bottom;
  const xLo = hasOrigin ? L0 : rows[0].lead;
  const span = Math.max(1, rows[rows.length - 1].lead - xLo);
  const x = (lead: number) => PAD.left + ((lead - xLo) / span) * iw;
  const y = (v: number) => PAD.top + ih - ((v - yLo) / (yHi - yLo)) * ih;

  /** 값이 없는 구간에서 선을 끊는다 — 이어 그리면 없는 값을 지어내는 셈이다. */
  const path = (pick: (r: ChartRow) => number | null | undefined) => {
    let d = "";
    let pen = false;
    for (const r of rows) {
      const v = pick(r);
      if (v === null || v === undefined) {
        pen = false;
        continue;
      }
      d += `${pen ? "L" : "M"}${x(r.lead).toFixed(1)} ${y(v).toFixed(1)} `;
      pen = true;
    }
    return d.trim();
  };

  const band = (() => {
    const up = rows.filter((r) => r.hi !== null);
    const dn = [...rows].reverse().filter((r) => r.lo !== null);
    if (up.length === 0) return "";
    return (
      up.map((r, i) => `${i ? "L" : "M"}${x(r.lead)} ${y(r.hi as number)}`).join(" ") +
      " " +
      dn.map((r) => `L${x(r.lead)} ${y(r.lo as number)}`).join(" ") +
      " Z"
    );
  })();

  const gateX = x(Math.min(gateLead, rows[rows.length - 1].lead));
  const hasActual = showActual && rows.some((r) => r.actual !== null);
  const anchor = rows[0].anchor;
  //  칸이 많으면 날짜를 걸러 적는다 — 다 적으면 글자가 겹친다.
  const every = rows.length > 12 ? 2 : 1;

  return (
    <figure className="m-0">
      <svg viewBox={`0 0 ${W} ${H}`} className="block h-auto w-full" role="img" aria-label="예측 그래프">
        {/* 게이트 구간 — 모델이 아니라 출발점이 그대로 나가는 자리 */}
        {gateLead > rows[0].lead && (
          <rect
            x={PAD.left}
            y={PAD.top}
            width={Math.max(0, gateX - PAD.left)}
            height={ih}
            fill="var(--color-shade)"
          />
        )}

        {niceTicks(yLo, yHi).map((t) => (
          <g key={t}>
            <line
              x1={PAD.left}
              x2={W - PAD.right}
              y1={y(t)}
              y2={y(t)}
              stroke="var(--color-grid)"
            />
            <text
              x={PAD.left - 8}
              y={y(t) + 3.5}
              textAnchor="end"
              className="tabular font-mono"
              fontSize={10}
              fill="var(--color-mut2)"
            >
              {Math.round(t).toLocaleString("ko-KR")}
            </text>
          </g>
        ))}

        {band && <path d={band} fill="var(--color-t-info)" opacity={0.13} />}

        {/* 출발점 — 여기서 얼마나 움직인다고 봤는지가 한눈에 보인다 */}
        {anchor !== null && (
          <>
            <line
              x1={PAD.left}
              x2={W - PAD.right}
              y1={y(anchor)}
              y2={y(anchor)}
              stroke="var(--color-mut2)"
              strokeWidth={1}
              strokeDasharray="2 3"
            />
            <text
              x={W - PAD.right}
              y={y(anchor) - 4}
              textAnchor="end"
              fontSize={9.5}
              fill="var(--color-mut2)"
            >
              출발점 {Math.round(anchor).toLocaleString("ko-KR")}
            </text>
          </>
        )}

        {/* 출발점에서 첫 예측까지 — 점선으로 그려 «예측 구간이 아님» 을 표시 */}
        {origin && rows[0].pred !== null && (
          <line
            x1={x(L0)}
            y1={y(origin.value)}
            x2={x(rows[0].lead)}
            y2={y(rows[0].pred)}
            stroke="var(--color-t-info)"
            strokeWidth={2}
            strokeDasharray="3 3"
            opacity={0.6}
          />
        )}

        <path d={path((r) => r.pred)} fill="none" stroke="var(--color-t-info)" strokeWidth={2} />
        {hasActual && (
          <path
            d={path((r) => r.actual)}
            fill="none"
            stroke="var(--color-t-warn)"
            strokeWidth={2}
            strokeDasharray="5 3"
          />
        )}

        {origin && (
          <circle
            cx={x(L0)}
            cy={y(origin.value)}
            r={4}
            fill="var(--color-panel)"
            stroke="var(--color-t-info)"
            strokeWidth={2}
          >
            <title>{`${origin.date} · 기준일\n출발점 ${Math.round(origin.value).toLocaleString("ko-KR")}${unit}\n(예측이 아니라 그날 이미 알고 있던 값)`}</title>
          </circle>
        )}

        {rows.map((r) => (
          <g key={r.lead}>
            {r.pred !== null && (
              <circle
                cx={x(r.lead)}
                cy={y(r.pred)}
                r={picked === r.lead ? 6 : 3}
                fill={r.gated ? "var(--color-mut2)" : "var(--color-t-info)"}
                stroke={picked === r.lead ? "var(--color-panel)" : "none"}
                strokeWidth={picked === r.lead ? 2 : 0}
                style={{ cursor: onPick ? "pointer" : "default" }}
                onClick={() => onPick?.(r)}
              >
                <title>
                  {`${r.lead === 0 ? "기준일 당일" : `${r.lead}영업일 뒤`} · ${r.target_dt}\n` +
                    `예측값: ${Math.round(r.pred).toLocaleString("ko-KR")}${unit}` +
                    (r.lo !== null && r.hi !== null
                      ? `\n예상 범위: ${Math.round(r.lo).toLocaleString("ko-KR")}~${Math.round(r.hi).toLocaleString("ko-KR")}`
                      : "") +
                    (showActual && r.actual !== null
                      ? `\n실제값: ${Math.round(r.actual).toLocaleString("ko-KR")}${unit}` +
                        (r.err_pct != null ? ` (${r.err_pct.toFixed(1)}% 틀림)` : "")
                      : "\n실제값 — 아직 안 온 날입니다") +
                    (r.gated ? "\n* 모델 안 씀 — 출발점 값을 그대로 가져왔습니다" : "") +
                    "\n\n눌러서 이유 보기"}
                </title>
              </circle>
            )}
            {showActual && r.actual !== null && (
              <circle cx={x(r.lead)} cy={y(r.actual)} r={2.8} fill="var(--color-t-warn)" />
            )}
          </g>
        ))}

        {origin && (
          <text
            x={x(L0)}
            y={H - 12}
            textAnchor="middle"
            className="tabular font-mono"
            fontSize={9.5}
            fontWeight={600}
            fill="var(--color-t-info)"
          >
            {origin.date.slice(5)}
          </text>
        )}
        {rows.map((r, i) =>
          i % every === 0 ? (
            <text
              key={r.lead}
              x={x(r.lead)}
              y={H - 12}
              textAnchor="middle"
              className="tabular font-mono"
              fontSize={9.5}
              fill={r.lead === 0 ? "var(--color-t-info)" : "var(--color-mut2)"}
              fontWeight={r.lead === 0 ? 600 : 400}
            >
              {r.target_dt.slice(5)}
            </text>
          ) : null,
        )}
      </svg>

      <figcaption
        className="mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px]"
        style={{ color: "var(--color-mut)" }}
      >
        <span className="flex items-center gap-1.5">
          <i aria-hidden className="inline-block h-0.5 w-4" style={{ background: "var(--color-t-info)" }} />
          예측 가격
        </span>
        <span className="flex items-center gap-1.5">
          <i
            aria-hidden
            className="inline-block h-2 w-4"
            style={{ background: "var(--color-t-info)", opacity: 0.13 }}
          />
          예측 범위
        </span>
        {hasActual && (
          <span className="flex items-center gap-1.5">
            <i
              aria-hidden
              className="inline-block h-0 w-4"
              style={{ borderTop: "2px dashed var(--color-t-warn)" }}
            />
            실제 가격
          </span>
        )}
        <span className="flex items-center gap-1.5">
          <i
            aria-hidden
            className="inline-block h-0 w-4"
            style={{ borderTop: "1px dashed var(--color-mut2)" }}
          />
          출발점 (어제 가격과 최근 7일 평균을 섞은 값)
        </span>
        {gateLead > 1 && (
          <span className="flex items-center gap-1.5">
            <i aria-hidden className="inline-block h-2 w-4" style={{ background: "var(--color-shade)" }} />
            모델 안 씀 ({gateLead}일 뒤 미만)
          </span>
        )}
        <span className="ml-auto" style={{ color: "var(--color-mut2)" }}>
          점을 누르면 이유가 나옵니다
        </span>
      </figcaption>
    </figure>
  );
}
