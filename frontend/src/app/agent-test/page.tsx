"use client";

/**
 * 오케스트레이터 · Critic 기능 테스트 페이지 (브랜치 한정 · 시연용).
 *
 * ★ 화면은 Backend 값을 **그대로** 보여준다. 여기서 다시 계산하지 않는다.
 *   Core(결정론) 결과와 AI(LLM) 산출물을 시각적으로 분리하는 것이 이 화면의 목적이다.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { ENDPOINTS, PRESETS, type EndpointKey } from "./presets";

type Json = Record<string, unknown>;

const LLM_TONE: Record<string, string> = {
  SUCCESS: "bg-emerald-100 text-emerald-800 border-emerald-300",
  SKIPPED_TEMPLATE: "bg-slate-100 text-slate-700 border-slate-300",
  FALLBACK: "bg-amber-100 text-amber-900 border-amber-300",
  DISABLED: "bg-slate-100 text-slate-500 border-slate-300",
};

const STATUS_TONE: Record<string, string> = {
  PASS: "bg-emerald-100 text-emerald-800 border-emerald-300",
  CONCERN: "bg-amber-100 text-amber-900 border-amber-300",
  FAIL: "bg-rose-100 text-rose-800 border-rose-300",
};

function Badge({ text, tone }: { text: string; tone?: string }) {
  return (
    <span
      className={`inline-flex items-center rounded border px-2 py-0.5 text-xs font-medium ${
        tone ?? "bg-slate-100 text-slate-700 border-slate-300"
      }`}
    >
      {text}
    </span>
  );
}

function Section({
  title,
  accent,
  children,
}: {
  title: string;
  accent?: "core" | "ai";
  children: React.ReactNode;
}) {
  const border =
    accent === "ai" ? "border-l-4 border-l-violet-400" : "border-l-4 border-l-slate-400";
  return (
    <section className={`rounded-r border border-slate-200 bg-white p-4 ${border}`}>
      <h3 className="mb-3 text-sm font-semibold tracking-wide text-slate-500">{title}</h3>
      {children}
    </section>
  );
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex gap-3 py-1 text-sm">
      <span className="w-40 shrink-0 text-slate-500">{label}</span>
      <span className="min-w-0 flex-1 break-words text-slate-900">{value}</span>
    </div>
  );
}

const num = (v: unknown) =>
  typeof v === "number" ? v.toLocaleString("ko-KR", { maximumFractionDigits: 3 }) : "—";

/** null 은 "상한 없음"이다 (INF 를 null 로 직렬화한다). */
const cap = (v: unknown) => (v === null || v === undefined ? "상한 없음" : num(v));

export default function AgentTestPage() {
  const [endpoint, setEndpoint] = useState<EndpointKey>("orchestrator/procurement");
  const [presetIndex, setPresetIndex] = useState(0);
  const [body, setBody] = useState(() =>
    JSON.stringify(PRESETS["orchestrator/procurement"][0].body, null, 2),
  );
  const [running, setRunning] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [result, setResult] = useState<Json | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showRaw, setShowRaw] = useState(false);
  const [lastRationale, setLastRationale] = useState<string | null>(null);
  const [runs, setRuns] = useState<Json[]>([]);

  const presets = PRESETS[endpoint];
  const preset = presets[Math.min(presetIndex, presets.length - 1)];

  /** 프리셋 선택 = 요청 본문 교체. effect 로 파생시키면 편집 중 값이 되돌아간다. */
  const choose = useCallback((next: EndpointKey, index: number) => {
    setEndpoint(next);
    setPresetIndex(index);
    setBody(JSON.stringify(PRESETS[next][index].body, null, 2));
    setResult(null);
    setError(null);
  }, []);

  // 실행 중 경과 시간 — LLM 호출이 20~35초라 진행 중임을 보여줘야 한다.
  useEffect(() => {
    if (!running) return;
    const started = Date.now();
    const id = setInterval(() => setElapsed((Date.now() - started) / 1000), 100);
    return () => clearInterval(id);
  }, [running]);

  const agent = endpoint.startsWith("critic") ? "critic" : "orchestrator";

  const fetchRuns = useCallback(async (): Promise<Json[]> => {
    try {
      const res = await fetch(`/api/${agent}/runs?limit=10`);
      return res.ok ? await res.json() : [];
    } catch {
      return []; // DB 미연결이어도 화면은 살아 있어야 한다
    }
  }, [agent]);

  useEffect(() => {
    let cancelled = false;
    void fetchRuns().then((rows) => {
      if (!cancelled) setRuns(rows);
    });
    return () => {
      cancelled = true;
    };
  }, [fetchRuns]);

  async function run() {
    setRunning(true);
    setError(null);
    setResult(null);
    setElapsed(0);
    const started = Date.now();
    try {
      const parsed = JSON.parse(body);
      const res = await fetch(`/api/${endpoint}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(parsed),
      });
      const data = await res.json();
      if (!res.ok) {
        setError(`HTTP ${res.status} — ${JSON.stringify(data.detail ?? data, null, 2)}`);
      } else {
        setResult(data);
        const rp = (data.interpretation as Json | undefined)?.rationale_per_id as
          | Record<string, string>
          | undefined;
        const rec = data.recommended_id as string | undefined;
        if (rp && rec && rp[rec]) setLastRationale(rp[rec]);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setElapsed((Date.now() - started) / 1000);
      setRunning(false);
      void fetchRuns().then(setRuns);
    }
  }

  /** 오케가 쓴 결정 근거를 Critic 요청의 rationale 로 넣는다 — 이 연계가 시연의 핵심이다. */
  function handOffRationale() {
    if (!lastRationale) return;
    try {
      const parsed = JSON.parse(body);
      parsed.rationale = lastRationale;
      setBody(JSON.stringify(parsed, null, 2));
    } catch {
      /* 편집 중 JSON 이 깨져 있으면 무시한다 */
    }
  }

  const interpretation = (result?.interpretation ?? null) as Json | null;
  const isCritic = endpoint.startsWith("critic");
  const coverage = (result?.coverage ?? null) as Record<string, [number, number]> | null;

  const clipResults = useMemo(
    () => (result?.clip_results as Json[] | undefined) ?? [],
    [result],
  );

  return (
    <main className="min-h-screen bg-slate-50 p-6 text-slate-900">
      <div className="mx-auto max-w-6xl space-y-4">
        <header>
          <h1 className="text-2xl font-semibold">오케스트레이터 · Critic 기능 테스트</h1>
          <p className="mt-1 text-sm text-slate-600">
            Backend 값을 그대로 표시합니다. 이 화면은 계산하지 않습니다.
            <span className="ml-2 text-slate-400">브랜치 한정 · 시연용</span>
          </p>
        </header>

        {/* 실행 패널 */}
        <section className="rounded border border-slate-200 bg-white p-4">
          <div className="flex flex-wrap gap-2">
            {ENDPOINTS.map((e) => (
              <button
                key={e.key}
                onClick={() => choose(e.key, 0)}
                className={`rounded border px-3 py-1.5 text-sm ${
                  endpoint === e.key
                    ? "border-slate-900 bg-slate-900 text-white"
                    : "border-slate-300 bg-white hover:bg-slate-50"
                }`}
              >
                <span className="mr-1.5 text-xs opacity-70">{e.agent}</span>
                {e.label}
              </button>
            ))}
          </div>

          <div className="mt-3 flex flex-wrap items-center gap-2">
            {presets.map((p, i) => (
              <button
                key={p.label}
                onClick={() => choose(endpoint, i)}
                className={`rounded-full border px-3 py-1 text-xs ${
                  presetIndex === i
                    ? "border-violet-500 bg-violet-50 text-violet-800"
                    : "border-slate-300 text-slate-600 hover:bg-slate-50"
                }`}
              >
                {p.label}
              </button>
            ))}
          </div>
          <p className="mt-2 text-xs text-slate-500">{preset.note}</p>

          <textarea
            value={body}
            onChange={(e) => setBody(e.target.value)}
            spellCheck={false}
            className="mt-3 h-64 w-full rounded border border-slate-300 bg-slate-50 p-3 font-mono text-xs"
          />

          <div className="mt-3 flex flex-wrap items-center gap-3">
            <button
              onClick={run}
              disabled={running}
              className="rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
            >
              {running ? `실행 중… ${elapsed.toFixed(1)}s` : "실행"}
            </button>

            {isCritic && lastRationale && (
              <button
                onClick={handOffRationale}
                className="rounded border border-violet-400 bg-violet-50 px-3 py-2 text-sm text-violet-800"
              >
                ← 오케 선정 근거를 rationale 로 넣기
              </button>
            )}

            {running && (
              <span className="text-xs text-amber-700">
                LLM 호출은 보통 20~35초 걸립니다. 모델 교체가 겹치면 더 걸릴 수 있습니다.
              </span>
            )}
            {!running && elapsed > 0 && (
              <span className="text-xs text-slate-500">응답 {elapsed.toFixed(1)}초</span>
            )}
          </div>

          {isCritic && lastRationale && (
            <p className="mt-2 rounded bg-violet-50 p-2 text-xs text-violet-900">
              <span className="font-semibold">L5 가 검사할 문장: </span>
              {lastRationale}
            </p>
          )}
        </section>

        {error && (
          <section className="rounded border border-rose-300 bg-rose-50 p-4">
            <h3 className="text-sm font-semibold text-rose-800">요청 실패</h3>
            <pre className="mt-2 overflow-x-auto whitespace-pre-wrap text-xs text-rose-900">
              {error}
            </pre>
          </section>
        )}

        {result && (
          <div className="grid gap-4 lg:grid-cols-2">
            {/* ── Core ── */}
            <div className="space-y-4">
              <Section title="[결정론 Core 결과]">
                <Row
                  label="runtime_status"
                  value={<Badge text={String(result.runtime_status ?? "—")} />}
                />
                {isCritic ? (
                  <>
                    <Row
                      label="판정"
                      value={
                        <Badge
                          text={String(result.status)}
                          tone={STATUS_TONE[String(result.status)]}
                        />
                      }
                    />
                    <Row label="커버리지 배지" value={<b>{String(result.badge)}</b>} />
                  </>
                ) : (
                  <>
                    <Row label="순위" value={(result.ranked_ids as string[])?.join(" > ") || "—"} />
                    <Row label="추천" value={String(result.recommended_id ?? "—")} />
                    {result.end_code ? (
                      <Row label="end_code" value={<Badge text={String(result.end_code)} />} />
                    ) : null}
                  </>
                )}
              </Section>

              {/* 밴드 */}
              {result.band ? (
                <Section title="밴드 (그날의 제약)">
                  {(() => {
                    const b = result.band as Json;
                    return (
                      <>
                        <Row label="floor_kg" value={JSON.stringify(b.floor_kg)} />
                        <Row label="cap_kg" value={JSON.stringify(b.cap_kg)} />
                        <Row label="cap_total_kg" value={cap(b.cap_total_kg)} />
                        <Row label="cap_amount_krw" value={cap(b.cap_amount_krw)} />
                        <Row label="기여 부서" value={JSON.stringify(b.contributors)} />
                        {(b.not_ready as string[])?.length ? (
                          <Row
                            label="미가동"
                            value={<Badge text={(b.not_ready as string[]).join(", ")} />}
                          />
                        ) : null}
                      </>
                    );
                  })()}
                </Section>
              ) : null}

              {/* 교착 */}
              {result.deadlock ? (
                <Section title="교착 (deadlock)">
                  {(() => {
                    const d = result.deadlock as Json;
                    return (
                      <>
                        <Row label="코드" value={<Badge text={String(d.code)} tone={STATUS_TONE.FAIL} />} />
                        <Row label="내용" value={String(d.detail)} />
                        <Row label="부족" value={`${num(d.shortfall)} ${d.unit}`} />
                        <Row label="책임 검사" value={(d.responsible_checks as string[])?.join(", ")} />
                      </>
                    );
                  })()}
                </Section>
              ) : null}

              {/* 클리핑 */}
              {clipResults.length > 0 && (
                <Section title="클리핑 (숫자는 코드가 확정)">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b text-left text-xs text-slate-500">
                        <th className="py-1">후보</th>
                        <th>원본</th>
                        <th>클리핑</th>
                        <th>binding</th>
                      </tr>
                    </thead>
                    <tbody>
                      {clipResults.map((c) => (
                        <tr key={String(c.scenario_id)} className="border-b last:border-0">
                          <td className="py-1 font-medium">{String(c.scenario_id)}</td>
                          <td>{num(c.original_total_kg)}</td>
                          <td className={c.clipped ? "font-semibold text-amber-700" : ""}>
                            {num(c.total_kg)}
                          </td>
                          <td className="text-xs text-slate-600">
                            {(c.binding_constraints as string[])?.join(", ") || "—"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </Section>
              )}

              {/* Critic 상세 */}
              {isCritic && coverage && (
                <Section title="레이어 커버리지 (감추지 않는다 · §8)">
                  <table className="w-full text-sm">
                    <tbody>
                      {Object.entries(coverage).map(([layer, [ran, total]]) => (
                        <tr key={layer} className="border-b last:border-0">
                          <td className="py-1 w-24 text-slate-500">{layer}</td>
                          <td className={ran === 0 ? "text-slate-400" : ""}>
                            {ran} / {total}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </Section>
              )}

              {isCritic && (result.findings as Json[])?.length > 0 && (
                <Section title="발견 (findings — FAIL 사유)">
                  {(result.findings as Json[]).map((f, i) => (
                    <div key={i} className="border-b py-1 text-sm last:border-0">
                      <Badge text={String(f.layer)} tone={STATUS_TONE.FAIL} />{" "}
                      <span className="font-mono text-xs">{String(f.check_id)}</span>
                      <p className="text-slate-700">{String(f.detail)}</p>
                    </div>
                  ))}
                </Section>
              )}

              {isCritic && (result.concerns as Json[])?.length > 0 && (
                <Section title="우려 (concerns — 결정을 죽이지 않는다)">
                  {(result.concerns as Json[]).map((c, i) => (
                    <div key={i} className="border-b py-1 text-sm last:border-0">
                      <Badge text={String(c.code)} tone={STATUS_TONE.CONCERN} />
                      <p className="text-slate-700">{String(c.detail)}</p>
                    </div>
                  ))}
                </Section>
              )}

              {isCritic && (result.skipped as string[])?.length > 0 && (
                <Section title="미검사 (skipped — 통과가 아니다)">
                  <ul className="list-disc pl-5 text-sm text-slate-600">
                    {(result.skipped as string[]).map((s, i) => (
                      <li key={i}>{s}</li>
                    ))}
                  </ul>
                </Section>
              )}

              {(result.soft_warnings as string[])?.length > 0 && (
                <Section title="Soft Warning (밴드를 움직이지 않는다)">
                  <ul className="list-disc pl-5 text-sm text-slate-600">
                    {(result.soft_warnings as string[]).map((s, i) => (
                      <li key={i}>{s}</li>
                    ))}
                  </ul>
                </Section>
              )}
            </div>

            {/* ── AI ── */}
            <div className="space-y-4">
              <Section title="[AI 산출물] — 숫자를 만들지 않는다" accent="ai">
                <div className="mb-3 flex flex-wrap gap-2">
                  <Badge
                    text={String(result.llm_status ?? "—")}
                    tone={LLM_TONE[String(result.llm_status)]}
                  />
                  <Badge text={String(result.llm_model ?? "—")} />
                  <Badge text={`시도 ${result.llm_attempts ?? 0}회`} />
                  {result.llm_fallback_used ? (
                    <Badge text="FALLBACK 사용" tone={LLM_TONE.FALLBACK} />
                  ) : null}
                </div>

                {result.llm_status === "FALLBACK" && (
                  <p className="mb-3 rounded bg-amber-50 p-2 text-xs text-amber-900">
                    LLM 이 실패했지만 왼쪽 Core 결과는 그대로입니다. 순위는 결정론 정렬을 씁니다.
                  </p>
                )}
                {result.llm_status === "SKIPPED_TEMPLATE" && (
                  <p className="mb-3 rounded bg-slate-50 p-2 text-xs text-slate-600">
                    호출이 불필요해 기본값을 썼습니다 (후보 1개 · 미가동 · 검사할 문장 없음).
                    {isCritic && " 커버리지의 skipped 와는 다른 개념입니다."}
                  </p>
                )}

                {interpretation ? (
                  <>
                    <Row label="summary" value={String(interpretation.summary ?? "—")} />
                    {isCritic ? (
                      <>
                        <Row
                          label="L5 판정"
                          value={
                            <Badge
                              text={String(interpretation.verdict)}
                              tone={
                                interpretation.verdict === "PASS"
                                  ? STATUS_TONE.PASS
                                  : STATUS_TONE.CONCERN
                              }
                            />
                          }
                        />
                        <Row label="근거" value={String(interpretation.note ?? "—")} />
                      </>
                    ) : (
                      <>
                        <Row
                          label="LLM 순위"
                          value={
                            (interpretation.ranked_scenario_ids as string[])?.join(" > ") || "—"
                          }
                        />
                        {interpretation.conflict_note ? (
                          <Row
                            label="부서 충돌"
                            value={
                              <span className="text-amber-800">
                                {String(interpretation.conflict_note)}
                              </span>
                            }
                          />
                        ) : null}
                        <div className="mt-3 space-y-2">
                          {Object.entries(
                            (interpretation.rationale_per_id as Record<string, string>) ?? {},
                          ).map(([id, text]) => (
                            <div key={id} className="rounded bg-violet-50 p-2 text-sm">
                              <span className="font-semibold">{id}</span>
                              <p className="text-slate-700">{text}</p>
                            </div>
                          ))}
                        </div>
                      </>
                    )}
                  </>
                ) : (
                  <p className="text-sm text-slate-500">AI 산출물이 없습니다.</p>
                )}
              </Section>

              {/* /day 는 하위 응답이 각자 LLM 상태를 갖는다 */}
              {result.procurement ? (
                <Section title="하루 전체 — 하위 응답" accent="ai">
                  <Row
                    label="매입 LLM"
                    value={
                      <Badge
                        text={String((result.procurement as Json).llm_status)}
                        tone={LLM_TONE[String((result.procurement as Json).llm_status)]}
                      />
                    }
                  />
                  <Row
                    label="판매 LLM"
                    value={
                      result.sales ? (
                        <Badge
                          text={String((result.sales as Json).llm_status)}
                          tone={LLM_TONE[String((result.sales as Json).llm_status)]}
                        />
                      ) : (
                        "—"
                      )
                    }
                  />
                </Section>
              ) : null}

              <Section title="원문 JSON">
                <button
                  onClick={() => setShowRaw((v) => !v)}
                  className="rounded border border-slate-300 px-2 py-1 text-xs"
                >
                  {showRaw ? "접기" : "펼치기"}
                </button>
                {showRaw && (
                  <pre className="mt-2 max-h-96 overflow-auto rounded bg-slate-900 p-3 text-xs text-slate-100">
                    {JSON.stringify(result, null, 2)}
                  </pre>
                )}
              </Section>
            </div>
          </div>
        )}

        {/* 실행이력 (DB) */}
        <Section title="실행이력 (DB 적재 · 최신 10건)">
          {runs.length === 0 ? (
            <p className="text-sm text-slate-500">
              이력이 없습니다. DB 미연결이어도 계산은 정상 동작합니다.
            </p>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b text-left text-xs text-slate-500">
                  <th className="py-1">시각</th>
                  <th>cycle</th>
                  <th>상태</th>
                  <th>LLM</th>
                  <th>모델</th>
                  <th>소요</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <tr key={String(r.run_id)} className="border-b last:border-0">
                    <td className="py-1 text-xs">
                      {new Date(String(r.created_at)).toLocaleTimeString("ko-KR")}
                    </td>
                    <td className="text-xs">{String(r.cycle)}</td>
                    <td className="text-xs">
                      {r.critic_status ? (
                        <Badge
                          text={String(r.critic_status)}
                          tone={STATUS_TONE[String(r.critic_status)]}
                        />
                      ) : (
                        String(r.runtime_status)
                      )}
                    </td>
                    <td className="text-xs">
                      {r.llm_status ? (
                        <Badge
                          text={String(r.llm_status)}
                          tone={LLM_TONE[String(r.llm_status)]}
                        />
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="text-xs text-slate-600">{String(r.llm_model ?? "—")}</td>
                    <td className="text-xs">
                      {r.elapsed_ms ? `${(Number(r.elapsed_ms) / 1000).toFixed(1)}s` : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Section>
      </div>
    </main>
  );
}
