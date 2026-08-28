/**
 * 마스터 호출과 화면 모양으로의 변환.
 *
 * 규칙 하나: **없는 값을 지어내지 않는다.** API 가 안 주는 판정(`situation` 등)은
 * `null` 로 두고 화면이 "미노출"이라고 말한다. 그럴듯한 기본값을 채우면 화면은
 * 멀쩡해 보이는데 사실이 아니게 된다.
 */
import { SCENES } from "./fixtures";
import type {
  Direction,
  FallbackProposal,
  Gate,
  MasterResponse,
  ProcurementRunRequest,
  ViewModel,
} from "./types";

/**
 * 개발 모드에서는 `next.config.ts` 의 rewrite 가 `/api/*` 를 백엔드로 넘긴다(CORS 우회).
 * 정적 내보내기 빌드에는 서버가 없어 rewrite 가 없으므로, 그때는 오리진을 직접 준다.
 */
const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "/api";

export const POLICY_VERSION = "v2.3";

/** 응답이 늦어도 화면이 영원히 매달리지 않게 한다. */
const TIMEOUT_MS = 15_000;

export async function requestProcurement(asOf: string): Promise<MasterResponse> {
  const scene = SCENES[asOf];
  if (!scene) throw new Error(`표본에 없는 기준일이다: ${asOf}`);

  // 예측·확정주문·정책값은 마스터가 직접 조회하지 않는다 — 요청에 실어 보낸다.
  const body: ProcurementRunRequest = {
    as_of: asOf,
    policy_version: POLICY_VERSION,
    trigger: "USER_REQUEST",
    item: scene.input.item,
    forecast: scene.input.forecast,
    confirmed_orders: scene.input.confirmed_orders,
    policy_values: scene.input.policy_values,
  };

  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(`${BASE}/master/request`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: ctl.signal,
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => "");
      throw new Error(`HTTP ${res.status} ${res.statusText}${detail ? ` — ${detail.slice(0, 200)}` : ""}`);
    }
    return (await res.json()) as MasterResponse;
  } finally {
    clearTimeout(timer);
  }
}

// ─────────────────────────────────────────────────────────────
// 변환
// ─────────────────────────────────────────────────────────────

/** 응답의 `constraints.finance` 에서 매입 상한을 읽는다. 없으면 표본값으로 물러선다. */
function capOf(res: MasterResponse, asOf: string): number {
  const raw = res.constraints?.finance?.finance_cap_amount_krw;
  return typeof raw === "number" ? raw : SCENES[asOf].financeCap;
}

export function fromApi(res: MasterResponse, asOf: string): ViewModel {
  return {
    source: "api",
    asOf: res.as_of,
    item: SCENES[asOf]?.input.item ?? "",
    scenarios: res.scenarios,
    financeCap: capOf(res, asOf),
    // ⚠️ 마스터 응답이 아직 안 싣는다 (types.ts 의 MasterResponse 주석 참조)
    judgment: null,
    gates: null,
    direction: null,
    reasoning: res.reason,
    runId: res.plan.find((s) => s.agent === "purchase")?.run_id ?? res.request_id,
    usedTools: res.plan.flatMap((s) => (s.agent === "purchase" ? s.used_tools : [])),
    endCode: res.end_code,
    reasonText: res.reason,
    boundary: { finance: res.constraints?.finance ?? {}, inventory: res.constraints?.inventory ?? {} },
    evidenceCount: null,
    missingData: res.plan.flatMap((s) => s.missing_data),
    concerns: res.concerns,
    skippedChecks: res.skipped_checks,
  };
}

/** 게이트 넷은 판정 근거(`evidences`)에서 만든다 — 화면이 계산하지 않는다. */
function gatesOf(p: FallbackProposal): Gate[] {
  const by: Record<string, (typeof p.evidences)[number]> = {};
  for (const e of p.evidences) {
    if (e.claim === "allowed_axes") by[e.ref_ids[0].split("-")[1]] = e;
  }
  const uncertain = p.situation === "uncertain";
  return [
    {
      name: "예측 구간폭 · CI",
      value: `${(by.CI.value * 100).toFixed(1)}%`,
      say: by.CI.evidence_detail,
      ref: by.CI.ref_ids[0],
      blocked: uncertain,
      chip: "선매입 차단",
    },
    {
      name: "추정 총량 · VOL",
      value: `${by.VOL.value.toLocaleString("ko-KR")} kg`,
      say: by.VOL.evidence_detail,
      ref: by.VOL.ref_ids[0],
      blocked: false,
      chip: "",
    },
    {
      name: "품목 편중 · MIX",
      value: `${(by.MIX.value * 100).toFixed(1)}%`,
      say: by.MIX.evidence_detail,
      ref: by.MIX.ref_ids[0],
      blocked: true,
      chip: "축 제외",
    },
  ];
}

/** 예측 방향은 근거의 예측 항목에서 읽는다. 값은 요청에 실어 보낸 forecast 가 정본이다. */
function directionOf(asOf: string, p: FallbackProposal): Direction | null {
  const fc = SCENES[asOf].input.forecast as {
    current_price?: number;
    daily?: { predicted: number }[];
    model_version?: string;
  };
  const current = fc.current_price;
  // ci_judgment_day = D+14 → daily 는 D+1 부터라 index 13 (constraints.yaml)
  const predicted = fc.daily?.[13]?.predicted;
  if (typeof current !== "number" || typeof predicted !== "number") return null;
  return {
    current,
    predicted,
    change: (predicted - current) / current,
    say:
      p.situation === "uncertain"
        ? "구간이 넓어 방향보다 폭이 판단을 지배한다 — 이 상승은 근거로 못 쓴다."
        : "지속 상승 궤적 — 미리 사 두는 안(시점 축)이 열리는 근거다.",
    ref: `FC-${fc.model_version}-${asOf}`,
  };
}

export function fromFallback(asOf: string, reason: string): ViewModel {
  const scene = SCENES[asOf];
  const p = scene.fallback;
  return {
    source: "fallback",
    fallbackReason: reason,
    asOf,
    item: p.meta.item,
    scenarios: p.scenarios,
    financeCap: scene.financeCap,
    judgment: {
      situation: p.situation,
      allowedAxes: p.allowed_axes,
      confidence: p.confidence,
      contextDocs: p.context_docs_used,
    },
    gates: gatesOf(p),
    direction: directionOf(asOf, p),
    reasoning: p.reasoning,
    runId: p.run_id,
    usedTools: p.used_tools,
    endCode: null,
    reasonText: p.reasoning,
    boundary: scene.boundary,
    evidenceCount: p.evidences.length,
    missingData: p.missing_data,
    concerns: [],
    skippedChecks: [],
  };
}

/**
 * 실제 호출 → 실패하거나 **시나리오가 0건이면** 표본으로 물러선다.
 *
 * 0건도 물러서는 이유: HTTP 는 200 이지만 화면에 띄울 것이 없다. 마스터는 부서
 * 미가동·보류를 오류가 아니라 종료 코드로 돌려주므로(§5.3), 통신 성공만 보면
 * 빈 화면이 된다. 대신 **무슨 코드로 비었는지**를 표본 배지에 그대로 적는다.
 */
export async function loadScene(asOf: string): Promise<ViewModel> {
  try {
    const res = await requestProcurement(asOf);
    if (res.scenarios.length > 0) return fromApi(res, asOf);
    return fromFallback(asOf, `${res.end_code} · ${res.reason || "시나리오 0건"}`);
  } catch (err) {
    return fromFallback(asOf, err instanceof Error ? err.message : String(err));
  }
}
