/**
 * 마스터 API 클라이언트.
 *
 * ★ **`/api` 프리픽스는 `next.config.ts` 의 개발 프록시가 백엔드로 넘긴다.** 같은
 *   출처라 CORS 가 없다. 배포(정적 export)에는 프록시가 없으므로
 *   `NEXT_PUBLIC_API_BASE` 로 절대 주소를 준다.
 *
 * ★ **서버가 낸 오류 문장을 그대로 올린다.** 화면이 *"오류가 발생했습니다"* 로 덮으면
 *   `422 '초공격' 은 이 실행이 내놓은 안이 아니다. 제시된 안: 보수, 기본, 공격` 처럼
 *   **무엇을 고쳐야 하는지 알려주는 문장**이 사라진다.
 */

import { DEFAULT_AS_OF, asOfSnapshot } from "./demo_as_of";
import type {
  AskResponse,
  BurnIn,
  ExecuteResponse,
  Intent,
  RunHistory,
  RunReport,
} from "./types";

const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "/api";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

/**
 * 읽기 넷(`runHistory` · `burnIn` · `runReport` · `health`)과 `ask` 의 상한.
 *
 * 2026-09-11 실측 (같은 LAN 의 DB · 걷기가 도는 중) — `/health` `0.12s` ·
 * `/master/burn-in` `0.09s` · `/master/runs/{id}` `0.07s` · `…/report` `0.04s`.
 * **20초는 그 최대의 160배**다.
 */
const READ_TIMEOUT_MS = 20_000;

/**
 * 실행은 상한이 다르다 — 하루치 판단을 통째로 돌린다.
 *
 * 처음에 `180초`(읽기 최대의 6배)로 잡았다가 **재고 물렀다.** `master_agent_runs.elapsed_ms`
 * 를 `request_id` 로 묶어(5분 넘게 벌어지면 다른 실행으로 자름) 실행 한 번의 소요를 세면 —
 *
 *     LLM 이 산 실행   377회   중앙 12.6s · p90 45.1s · p99 243.0s · 최대 **731.0s**
 *     LLM 이 꺼진 실행 5,102회 중앙  2.3s · p90  5.0s · p99  11.5s · 최대   67.2s
 *
 * 상한별로 **잘렸을 실행**이 이렇게 된다 (LLM 이 산 377회 기준) —
 *
 *      60s  32건 (8.5%)      180s  9건 (2.4%)      600s  1건 (0.3%)
 *     120s  13건 (3.4%)      300s  2건 (0.5%)      900s  0건
 *
 * ★ `900초` 를 고른다. 이 상한이 하려는 일은 *"느린 실행을 빨리 자르는 것"* 이 아니라
 *   *"영영 안 끝나는 실행을 끝내는 것"* 이다 — **관측된 정상 실행을 하나도 안 자르는**
 *   가장 낮은 칸이다. 잘린 실행은 화면에서 안이 통째로 사라지므로, 시연에서는 그쪽이
 *   더 나쁘다.
 *
 * ⚠️ 15분 스피너가 좋다는 뜻이 아니다. 그건 진행 표시로 풀 일이고 여기 상한과 다른 판이다.
 * 🟡 위 표는 `2026-09-11` 기록이다. LLM·모델이 바뀌면 다시 재고 이 칸을 조인다.
 */
const EXECUTE_TIMEOUT_MS = 900_000;

async function call<T>(path: string, init?: RequestInit, timeoutMs = READ_TIMEOUT_MS): Promise<T> {
  // 🔴 **본문까지 같은 상한 안에 둔다.** 헤더만 먼저 오고 본문이 안 끝나는 경우가 있다.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await send<T>(path, controller, timeoutMs, init);
  } finally {
    clearTimeout(timer);
  }
}

async function send<T>(
  path: string,
  controller: AbortController,
  timeoutMs: number,
  init?: RequestInit,
): Promise<T> {
  // ⚠️ **끊은 것과 못 닿은 것은 다른 사고다.** 한 문장으로 뭉치면 보는 사람이
  //    "서버를 켜라" 는 엉뚱한 조치를 한다 — 서버는 떠 있고 느린 것이다.
  const tooSlow = () =>
    new ApiError(0, `${timeoutMs / 1000}초 안에 응답이 오지 않아 끊었습니다 — 서버가 떠 있으나 느립니다.`);

  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
      // ``...init`` 뒤에 둔다 — 부르는 쪽이 signal 을 실어도 상한이 이긴다.
      signal: controller.signal,
    });
  } catch {
    if (controller.signal.aborted) throw tooSlow();
    throw new ApiError(0, "백엔드에 닿지 못했습니다 — 서버가 떠 있는지 확인해 주세요.");
  }

  let body: string;
  try {
    body = await response.text();
  } catch {
    if (controller.signal.aborted) throw tooSlow();
    throw new ApiError(0, "응답 본문을 읽지 못했습니다.");
  }
  if (!response.ok) {
    let detail = body;
    try {
      const parsed = JSON.parse(body) as { detail?: unknown };
      if (typeof parsed.detail === "string") detail = parsed.detail;
      else if (parsed.detail) detail = JSON.stringify(parsed.detail);
    } catch {
      /* 본문이 JSON 이 아니면 그대로 쓴다 */
    }
    throw new ApiError(response.status, detail);
  }
  return JSON.parse(body) as T;
}

/**
 * 기본 기준일.
 *
 * 🔴 값의 주인은 `lib/demo_as_of.ts` 하나다 (`#431`). 예전에는 이 파일과
 *    `components/console/useTab.ts` 가 **각자 다른 기본값을 들고 있었다** —
 *    마스터는 `2025-12-31` 로 판단하고 탭은 `2026-01-06` 을 보여 줬다.
 *
 * ★ 이 상수는 **기본값 자리로만 남는다.** 실제로 부를 때 쓰는 값은
 *   `asOfSnapshot()` 이 준다 — 시연 중에 화면에서 날짜를 바꾸면 그 값이 바뀐다.
 */
export const AS_OF = DEFAULT_AS_OF;
export const POLICY_VERSION = "v1.3";

/** ① 발화문을 분류한다. **확인이 필요하면 아무것도 실행하지 않는다.** */
export function ask(utterance: string): Promise<AskResponse> {
  return call<AskResponse>("/master/ask", {
    method: "POST",
    body: JSON.stringify({
      utterance,
      as_of: asOfSnapshot(),
      policy_version: POLICY_VERSION,
    }),
  });
}

/**
 * ② 확인한 의도를 실행한다.
 *
 * 🔴 **`intent` 를 그대로 되돌려보낸다.** 서버는 재분류하지 않는다 — 다시 분류하면
 *    사용자가 확인한 것과 다른 것이 돌 수 있고, 그 순간 확인의 뜻이 사라진다.
 */
export function execute(args: {
  intent: Intent;
  requestId?: string;
  targetRequestId?: string;
  /** 화면이 **보고 있던 실행**. 없으면 서버가 최신을 고르고 경합이 남는다. */
  targetHistoryRunId?: string;
  decidedBy?: string;
}): Promise<ExecuteResponse> {
  return call<ExecuteResponse>(
    "/master/ask/execute",
    {
      method: "POST",
      body: JSON.stringify({
        intent: args.intent,
        as_of: asOfSnapshot(),
        policy_version: POLICY_VERSION,
        request_id: args.requestId ?? null,
        // 발화문에 없어 화면이 실어야 하는 셋 (SELECT · RERUN 필수)
        target_request_id: args.targetRequestId ?? null,
        target_history_run_id: args.targetHistoryRunId ?? null,
        decided_by: args.decidedBy ?? null,
      }),
    },
    // 🔴 여기만 상한이 다르다 — 읽기가 아니라 **돌리는** 호출이다.
    EXECUTE_TIMEOUT_MS,
  );
}

export function runHistory(requestId: string): Promise<RunHistory> {
  return call<RunHistory>(`/master/runs/${encodeURIComponent(requestId)}`);
}

/** 번인 구간 — 에이전트가 판단하기 전 30일. **읽기 전용이다.** */
export function burnIn(): Promise<BurnIn> {
  return call<BurnIn>("/master/burn-in");
}

/** 매입안 보고서. **서버가 만든 Markdown 을 그대로 받는다.** */
export function runReport(requestId: string): Promise<RunReport> {
  return call<RunReport>(`/master/runs/${encodeURIComponent(requestId)}/report`);
}

export function health(): Promise<{ status: string }> {
  return call<{ status: string }>("/health");
}
