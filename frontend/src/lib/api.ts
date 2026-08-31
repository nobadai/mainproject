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

import type { AskResponse, BurnIn, ExecuteResponse, Intent, RunHistory } from "./types";

const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "/api";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    });
  } catch {
    throw new ApiError(0, "백엔드에 닿지 못했습니다 — 서버가 떠 있는지 확인해 주세요.");
  }

  const body = await response.text();
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

/** 오늘 기준일. 재무·물류 실 DB 가 있는 날이라 시연은 이 날짜로 돈다. */
export const AS_OF = process.env.NEXT_PUBLIC_AS_OF ?? "2025-12-31";
export const POLICY_VERSION = "v1.3";

/** ① 발화문을 분류한다. **확인이 필요하면 아무것도 실행하지 않는다.** */
export function ask(utterance: string): Promise<AskResponse> {
  return call<AskResponse>("/master/ask", {
    method: "POST",
    body: JSON.stringify({ utterance, as_of: AS_OF, policy_version: POLICY_VERSION }),
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
  return call<ExecuteResponse>("/master/ask/execute", {
    method: "POST",
    body: JSON.stringify({
      intent: args.intent,
      as_of: AS_OF,
      policy_version: POLICY_VERSION,
      request_id: args.requestId ?? null,
      // 발화문에 없어 화면이 실어야 하는 셋 (SELECT · RERUN 필수)
      target_request_id: args.targetRequestId ?? null,
      target_history_run_id: args.targetHistoryRunId ?? null,
      decided_by: args.decidedBy ?? null,
    }),
  });
}

export function runHistory(requestId: string): Promise<RunHistory> {
  return call<RunHistory>(`/master/runs/${encodeURIComponent(requestId)}`);
}

/** 번인 구간 — 에이전트가 판단하기 전 30일. **읽기 전용이다.** */
export function burnIn(): Promise<BurnIn> {
  return call<BurnIn>("/master/burn-in");
}

export function health(): Promise<{ status: string }> {
  return call<{ status: string }>("/health");
}
