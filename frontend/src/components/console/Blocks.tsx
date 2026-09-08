"use client";

/**
 * 화면 부품 — 백엔드가 내려준 조각을 그린다.
 *
 * ★ **백엔드는 뜻만 주고 색은 여기서 정합니다.** 응답에 `tone: "warn"` 이
 *   들어오면 여기서 흙노랑으로 그립니다. 백엔드가 색을 내려보내면 화면을
 *   고칠 때마다 백엔드를 고쳐야 합니다.
 *
 * ★ **`null` 은 0 이 아니라 공란입니다.** 표도 그래프도 그렇게 그립니다.
 *   물류가 보고를 안 한 날을 0 으로 그리면 «재고가 없었다» 는 거짓말이 됩니다.
 */

import type {
  Badge as TBadge,
  Card as TCard,
  Cell,
  Chart as TChart,
  Column,
  Day,
  Note as TNote,
  Stat as TStat,
  Table as TTable,
  Tone,
} from "@/lib/screen";

/* ── 색 ───────────────────────────────────────────────────────────────── */

const FG: Record<Tone, string> = {
  neutral: "var(--color-ink)",
  good: "var(--color-t-good)",
  warn: "var(--color-t-warn)",
  bad: "var(--color-t-bad)",
  info: "var(--color-t-info)",
  sim: "var(--color-t-sim)",
};
const BG: Record<Tone, string> = {
  neutral: "var(--color-sunk)",
  good: "var(--color-t-good-bg)",
  warn: "var(--color-t-warn-bg)",
  bad: "var(--color-t-bad-bg)",
  info: "var(--color-t-info-bg)",
  sim: "var(--color-t-sim-bg)",
};

/** `**굵게**` 만 알아듣는 아주 작은 표시. 백엔드 글에서 강조를 살린다. */
function Rich({ text }: { text: string }) {
  return (
    <>
      {text.split(/(\*\*[^*]+\*\*)/g).map((piece, i) =>
        piece.startsWith("**") && piece.endsWith("**") ? (
          <b key={i} className="font-semibold">
            {piece.slice(2, -2)}
          </b>
        ) : (
          <span key={i}>{piece}</span>
        ),
      )}
    </>
  );
}

/* ── 작은 조각 ────────────────────────────────────────────────────────── */

export function Pill({ text, tone = "neutral" }: { text: string; tone?: Tone }) {
  return (
    <span
      className="inline-flex items-center rounded-full px-2.5 py-1 text-[11px] font-semibold"
      style={{ color: FG[tone], background: BG[tone] }}
    >
      {text}
    </span>
  );
}

export function Badges({ items }: { items: TBadge[] }) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {items.map((b) => (
        <Pill key={b.text} text={b.text} tone={b.tone} />
      ))}
    </div>
  );
}

export function Note({ note }: { note: TNote | null }) {
  if (!note) return null;
  return (
    <p
      className="m-0 rounded-lg border px-3.5 py-3 text-[12px] leading-relaxed"
      style={{
        color: note.tone === "neutral" ? "var(--color-ink2)" : FG[note.tone],
        background: note.tone === "neutral" ? "var(--color-sunk)" : BG[note.tone],
        borderColor: note.tone === "neutral" ? "var(--color-hair)" : "transparent",
      }}
    >
      <Rich text={note.text} />
    </p>
  );
}

export function StatRow({ items }: { items: TStat[] }) {
  if (items.length === 0) return null;
  return (
    <div className="grid gap-2.5 [grid-template-columns:repeat(auto-fit,minmax(190px,1fr))]">
      {items.map((s, i) => (
        <div
          key={`${s.label}-${i}`}
          className="flex min-w-0 flex-col gap-1 rounded-xl border bg-panel px-4 py-3.5"
          style={{ borderColor: "var(--color-hair)" }}
        >
          <span className="flex items-center gap-1.5 text-[11.5px]" style={{ color: "var(--color-mut)" }}>
            {s.tone !== "neutral" && (
              <i
                aria-hidden
                className="inline-block size-[7px] shrink-0 rounded-full"
                style={{ background: FG[s.tone] }}
              />
            )}
            <span className="truncate">{s.label}</span>
          </span>
          {/*  ★ 값이 수가 아니면 큰 폭고정 글꼴로 두지 않는다 —
                  «15건 모두 출고 완료» 가 숫자처럼 커 보이면 읽기 나쁘다 */}
          <span
            className={
              /^[-+]?[\d,.]+$/.test(s.value)
                ? "tabular font-mono text-[21px] leading-tight"
                : "text-[15px] font-semibold leading-snug"
            }
            style={{ color: s.tone === "neutral" ? undefined : FG[s.tone] }}
          >
            {s.value}
            {s.unit && (
              <span className="ml-1 font-sans text-[10.5px]" style={{ color: "var(--color-mut)" }}>
                {s.unit}
              </span>
            )}
          </span>
          {s.detail && (
            <span className="text-[10.5px] leading-snug" style={{ color: "var(--color-mut2)" }}>
              {s.detail}
            </span>
          )}
        </div>
      ))}
    </div>
  );
}

/* ── 표 ───────────────────────────────────────────────────────────────── */

function cellText(v: Cell): { text: string; blank: boolean } {
  //  ★ null 은 공란이다. 0 과 다르다.
  if (v === null || v === undefined) return { text: "—", blank: true };
  return { text: typeof v === "number" ? v.toLocaleString("ko-KR") : String(v), blank: false };
}

function align(c: Column) {
  return c.align === "right" ? "text-right" : c.align === "center" ? "text-center" : "text-left";
}

export function DataTable({ table }: { table: TTable }) {
  return (
    <div className="flex flex-col gap-2.5">
      <div className="thin-scroll -mx-1 overflow-x-auto px-1">
        {table.rows.length === 0 ? (
          <p
            className="m-0 rounded-lg border border-dashed px-4 py-6 text-center text-[12px]"
            style={{ borderColor: "var(--color-hair)", color: "var(--color-mut2)" }}
          >
            {table.empty_text}
          </p>
        ) : (
          <table className="w-full border-collapse text-[12px]">
            <thead>
              <tr>
                {table.columns.map((c) => (
                  <th
                    key={c.key}
                    scope="col"
                    className={`whitespace-nowrap border-b px-2.5 py-2 font-medium ${align(c)}`}
                    style={{ borderColor: "var(--color-hair)", color: "var(--color-mut)" }}
                  >
                    {c.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {table.rows.map((row, ri) => (
                <tr key={ri}>
                  {table.columns.map((c) => {
                    const { text, blank } = cellText(row[c.key] ?? null);
                    return (
                      <td
                        key={c.key}
                        className={`whitespace-nowrap border-b px-2.5 py-2 ${align(c)} ${
                          c.mono ? "tabular font-mono" : ""
                        }`}
                        style={{
                          borderColor: "var(--color-hair-soft)",
                          color: blank ? "var(--color-mut2)" : undefined,
                        }}
                      >
                        {text}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <Note note={table.note} />
    </div>
  );
}

/* ── 그래프 ───────────────────────────────────────────────────────────── */

const W = 720;
const PAD = { l: 54, r: 22, t: 20, b: 40 };

/**
 * 날짜축 그래프.
 *
 * ★ **끊긴 선을 이어 붙이지 않습니다.** 값이 없는 날에서 선을 끊습니다.
 *   이으면 «그날도 값이 있었다» 로 보입니다.
 *
 * ★ **`x_labels` · `y_labels` 는 없을 수도 있다고 보고 씁니다.** 백엔드를
 *   고치고 서버를 안 올리면 그 칸이 통째로 안 옵니다. 그때 `[0]` 을 바로
 *   읽으면 **화면이 죽습니다** — 실제로 죽였습니다. 눈금 하나 때문에
 *   대시보드 전체가 사라지면 안 됩니다.
 */
export function LineChart({ chart, days, asOfIndex = -1, height = 250 }: {
  chart: TChart;
  /** 공용 날짜축. 없으면 `chart.x_labels` 를 쓴다 (재무 12월 · 판매 수금). */
  days?: Day[];
  asOfIndex?: number;
  height?: number;
}) {
  //  ★ 축이 둘이다. 날짜축을 쓰는 탭은 휴장일을 눕히고 «오늘» 선을 긋고,
  //    자기 눈금을 가진 그래프(재무 12월 30칸)는 글자만 적는다.
  const ticks: { text: string; sub: string; dim: boolean }[] =
    days && days.length > 0
      ? days.map((d) => ({ text: d.date.slice(5), sub: d.dow, dim: !d.market_open }))
      : (chart.x_labels ?? []).map((t) => ({ text: t, sub: "", dim: false }));
  const n = Math.max(ticks.length, 2);
  const H = height;
  const iw = W - PAD.l - PAD.r;
  const ih = H - PAD.t - PAD.b;
  const step = iw / (n - 1);
  const x = (i: number) => PAD.l + i * step;
  const y = (v: number) =>
    PAD.t + ih - ((v - chart.y_min) / (chart.y_max - chart.y_min || 1)) * ih;

  const path = (data: (number | null)[]) => {
    let d = "";
    let open = false;
    data.slice(0, n).forEach((v, i) => {
      if (v === null || v === undefined) {
        open = false;
        return;
      }
      d += `${open ? "L" : "M"}${x(i).toFixed(1)} ${y(v).toFixed(1)} `;
      open = true;
    });
    return d.trim();
  };

  const lastPoint = (data: (number | null)[]) => {
    for (let i = Math.min(n, data.length) - 1; i >= 0; i -= 1) {
      const v = data[i];
      if (v !== null && v !== undefined) return { i, v };
    }
    return null;
  };

  return (
    <figure className="m-0 flex flex-col gap-2.5">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="block h-auto w-full"
        role="img"
        aria-label={chart.label}
      >
        {/* 휴장일 — 뒤에 눕혀 둔다 */}
        {ticks.map((d, i) =>
          !d.dim ? null : (
            <rect
              key={`h${i}`}
              x={x(i) - step / 2}
              y={PAD.t}
              width={step}
              height={ih}
              fill="var(--color-shade)"
              opacity={0.55}
            />
          ),
        )}

        {chart.y_ticks.map((t, ti) => (
          <g key={t}>
            <line x1={PAD.l} x2={W - PAD.r} y1={y(t)} y2={y(t)} stroke="var(--color-grid)" />
            <text
              x={PAD.l - 8}
              y={y(t) + 3}
              textAnchor="end"
              className="tabular font-mono"
              fontSize="10"
              fill="var(--color-mut2)"
            >
              {/* ★ 백엔드가 준 글자가 있으면 그걸 쓴다. 값의 단위와 보이는
                     단위가 다를 수 있다 — 재고는 kg 로 그리고 톤으로 적는다 */}
              {chart.y_labels?.[ti] || `${t.toLocaleString("ko-KR")}${chart.y_unit}`}
            </text>
          </g>
        ))}

        {chart.bands.map((b) => {
          const hi = b.hi.slice(0, n).map((v, i) => (v == null ? null : [x(i), y(v)] as const));
          const lo = b.lo.slice(0, n).map((v, i) => (v == null ? null : [x(i), y(v)] as const));
          const hiPts = hi.filter(Boolean) as (readonly [number, number])[];
          const loPts = lo.filter(Boolean) as (readonly [number, number])[];
          if (hiPts.length === 0) return null;
          if (hiPts.length === 1) {
            const [px, py] = hiPts[0];
            const bottom = loPts[0]?.[1] ?? py;
            return (
              <rect
                key={b.name}
                x={px - 11}
                y={py}
                width={22}
                height={Math.max(1, bottom - py)}
                rx={3}
                fill="var(--color-band)"
              />
            );
          }
          const d =
            `M${hiPts.map((p) => p.join(" ")).join("L")}` +
            `L${[...loPts].reverse().map((p) => p.join(" ")).join("L")}Z`;
          return <path key={b.name} d={d} fill="var(--color-band)" />;
        })}

        {/* as_of 선 — 여기까지가 일어난 일이다. 날짜축이 없으면 안 긋는다 */}
        {asOfIndex >= 0 && asOfIndex < n && (
        <>
        <line
          x1={x(asOfIndex)}
          x2={x(asOfIndex)}
          y1={PAD.t}
          y2={PAD.t + ih}
          stroke="var(--color-mut2)"
          strokeDasharray="2 3"
        />
        <text x={x(asOfIndex) + 5} y={PAD.t + 9} fontSize="10.5" fill="var(--color-ink2)">
          오늘
        </text>
        </>
        )}

        {chart.series.map((s) => {
          if (s.width === 0) return null;
          const end = s.end_dot ? lastPoint(s.data) : null;
          return (
            <g key={s.name}>
              <path
                d={path(s.data)}
                fill="none"
                stroke={FG[s.tone]}
                strokeWidth={s.width}
                strokeLinejoin="round"
                strokeLinecap="round"
                strokeDasharray={s.dashed ? "5 4" : undefined}
                opacity={s.opacity}
              />
              {end && (
                <circle cx={x(end.i)} cy={y(end.v)} r={3.6} fill={FG[s.tone]} stroke="#fff" strokeWidth={2} />
              )}
            </g>
          );
        })}

        {/* 값이 한 점뿐인 계열은 선이 안 보이므로 점으로 찍는다 */}
        {chart.series
          .filter((s) => s.width === 0)
          .flatMap((s) =>
            s.data.slice(0, n).map((v, i) =>
              v == null ? null : (
                <circle key={`${s.name}${i}`} cx={x(i)} cy={y(v)} r={4.5} fill={FG[s.tone]} />
              ),
            ),
          )}

        {chart.markers
          .filter((m) => m.index >= 0 && m.index < n)
          .map((m) => (
            <g key={`${m.index}-${m.label}`}>
              <circle cx={x(m.index)} cy={y(m.value)} r={4} fill={FG[m.tone]} stroke="#fff" strokeWidth={1.6} />
              <text
                x={x(m.index)}
                y={y(m.value) + 19}
                textAnchor="middle"
                fontSize="10.5"
                fill="var(--color-ink2)"
              >
                {m.label}
              </text>
            </g>
          ))}

        {ticks.map((d, i) =>
          d.text === "" ? null : (
            <g key={`x${i}`} opacity={d.dim ? 0.5 : 1}>
              <text
                x={x(i)}
                y={H - 16}
                textAnchor="middle"
                className="tabular font-mono"
                fontSize="10"
                fill="var(--color-mut2)"
              >
                {d.text}
              </text>
              {d.sub && (
                <text
                  x={x(i)}
                  y={H - 5}
                  textAnchor="middle"
                  className="font-mono"
                  fontSize="9.5"
                  fill="var(--color-mut2)"
                  opacity={0.8}
                >
                  {d.sub}
                </text>
              )}
            </g>
          ),
        )}
      </svg>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[11px]" style={{ color: "var(--color-mut)" }}>
        {chart.series.map((s) => (
          <span key={s.name} className="inline-flex items-center gap-1.5">
            <i
              aria-hidden
              className="inline-block h-0 w-4"
              style={{
                borderTop: `${Math.max(1, s.width)}px ${s.dashed ? "dashed" : "solid"} ${FG[s.tone]}`,
                opacity: s.opacity,
              }}
            />
            {s.name}
          </span>
        ))}
        {ticks.some((d) => d.dim) && (
          <span className="inline-flex items-center gap-1.5">
            <i aria-hidden className="inline-block size-3 rounded-sm" style={{ background: "var(--color-shade)" }} />
            {chart.shade_label || "휴장 · 경매 없음"}
          </span>
        )}
      </div>

      {chart.note && (
        <figcaption className="m-0">
          <Note note={chart.note} />
        </figcaption>
      )}
    </figure>
  );
}

/* ── 카드 ─────────────────────────────────────────────────────────────── */

export function Panel({
  title,
  subtitle,
  right,
  children,
  footer,
}: {
  title?: string;
  subtitle?: string | null;
  right?: React.ReactNode;
  children: React.ReactNode;
  footer?: string | null;
}) {
  return (
    <section
      className="flex flex-col overflow-hidden rounded-xl border bg-panel"
      style={{ borderColor: "var(--color-hair)" }}
    >
      {title && (
        <header
          className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b px-4 py-3"
          style={{ borderColor: "var(--color-hair-soft)" }}
        >
          <h2 className="m-0 text-[13.5px] font-semibold">{title}</h2>
          {subtitle && (
            <span className="text-[11.5px]" style={{ color: "var(--color-mut)" }}>
              {subtitle}
            </span>
          )}
          {right && <div className="ml-auto flex items-center gap-2">{right}</div>}
        </header>
      )}
      <div className="flex flex-col gap-3.5 p-4">{children}</div>
      {footer && (
        <p
          className="m-0 border-t px-4 py-2.5 text-[11px]"
          style={{ borderColor: "var(--color-hair-soft)", color: "var(--color-mut)" }}
        >
          {footer}
        </p>
      )}
    </section>
  );
}

export function Flow({ steps }: { steps: string[] }) {
  if (steps.length === 0) return null;
  return (
    <div className="thin-scroll flex items-center gap-1.5 overflow-x-auto pb-1 text-[11.5px]">
      {steps.map((s, i) => (
        <span key={s} className="flex shrink-0 items-center gap-1.5">
          {i > 0 && <i aria-hidden style={{ color: "var(--color-mut2)" }}>→</i>}
          <span
            className="whitespace-nowrap rounded-md px-2.5 py-1.5"
            style={{ background: "var(--color-sunk)", color: "var(--color-ink2)" }}
          >
            {s}
          </span>
        </span>
      ))}
    </div>
  );
}

/** 백엔드가 목록으로 준 카드 하나. 채워진 것만 그린다. */
export function CardBlock({ card }: { card: TCard }) {
  return (
    <Panel
      title={card.title}
      subtitle={card.subtitle}
      footer={card.footer}
      right={
        card.source_ref ? (
          <span className="font-mono text-[10.5px]" style={{ color: "var(--color-mut2)" }}>
            {card.source_ref}
          </span>
        ) : undefined
      }
    >
      <Note note={card.lead} />
      <Flow steps={card.flow} />
      <StatRow items={card.stats} />
      {/* 카드 안 그래프는 자기 눈금(`x_labels`)을 쓴다 — 공용 날짜축이 아니다 */}
      {card.chart && <LineChart chart={card.chart} />}
      {card.table && <DataTable table={card.table} />}
      {card.bullets.length > 0 && (
        <ul className="m-0 flex list-none flex-col gap-1.5 p-0 text-[12px] leading-relaxed">
          {card.bullets.map((b) => (
            <li key={b} className="flex gap-2">
              <i aria-hidden style={{ color: "var(--color-mut2)" }}>·</i>
              <span>
                <Rich text={b} />
              </span>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

/** 값이 아직 예시일 때 붙이는 딱지. **없으면 데모 숫자를 실적으로 읽습니다.** */
export function SourceTag({ sources }: { sources: { filled: boolean; owner: string; note: string | null }[] }) {
  const unfilled = sources.filter((s) => !s.filled);
  if (unfilled.length === 0) return null;
  return (
    <div
      className="flex flex-wrap items-center gap-x-2.5 gap-y-1 rounded-lg border px-3.5 py-2.5 text-[11.5px]"
      style={{ borderColor: "var(--color-t-warn)", background: "var(--color-t-warn-bg)", color: "var(--color-t-warn)" }}
    >
      <b className="font-semibold">예시값</b>
      <span>
        {unfilled.map((s) => s.owner).join(" · ")} 파트가 아직 실제 값에 붙이지 않았습니다 — 이 화면의
        숫자는 화면 구성을 보이기 위한 것입니다.
      </span>
    </div>
  );
}

export function ErrorBox({ message }: { message: string }) {
  return (
    <p
      className="m-0 rounded-lg px-4 py-3.5 text-[12.5px]"
      style={{ background: "var(--color-t-bad-bg)", color: "var(--color-t-bad)" }}
    >
      {message}
    </p>
  );
}

export function Loading({ what }: { what: string }) {
  return (
    <p className="m-0 py-10 text-center text-[12.5px]" style={{ color: "var(--color-mut2)" }}>
      {what}을(를) 읽는 중…
    </p>
  );
}

export function TabButtons<T extends string>({
  items,
  value,
  onChange,
}: {
  items: { key: T; label: string }[];
  value: T;
  onChange: (v: T) => void;
}) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {items.map((it) => {
        const on = it.key === value;
        return (
          <button
            key={it.key}
            type="button"
            onClick={() => onChange(it.key)}
            aria-pressed={on}
            className="rounded-lg border px-3 py-1.5 text-[12px] font-medium transition"
            style={{
              borderColor: on ? "var(--color-nav)" : "var(--color-hair)",
              background: on ? "var(--color-nav)" : "var(--color-panel)",
              color: on ? "#f4f3ee" : "var(--color-ink2)",
            }}
          >
            {it.label}
          </button>
        );
      })}
    </div>
  );
}
