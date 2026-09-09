/**
 * ML 운영 콘솔 클라이언트 — 재학습 · 배치 · 에이전트 보고서.
 *
 * ★ **이 셋은 ML 저장소가 있는 곳에서만 돕니다.** 학습 꾸러미와 모델 번들,
 *   에이전트 기록이 거기 있습니다. 그래서 이 서버가 대신 물어봅니다
 *   (`app/ml/console_proxy.py`).
 *
 *       화면 ──/api/ml/…──▶ 이 서버 ──▶ ML 백엔드(8102)
 *
 * ★ **화면용 API(`lib/screen.ts`)와 다릅니다.** 저쪽은 여섯 탭이 쓰는 값이고
 *   조회뿐입니다. 이쪽은 사람이 눌러 무언가를 **돌리는** 것이 섞여 있습니다.
 *
 * ★ **ML 백엔드가 안 떠 있으면 502 로 그 사실을 말합니다.** 빈 값으로
 *   떨어지지 않습니다 — 화면이 «자료가 없다» 로 보이면 안 됩니다.
 */

const BASE = process.env.NEXT_PUBLIC_ML_BASE ?? "/api/ml";

export class MlError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${BASE}/${path}`, {
      ...init,
      headers: { Accept: "application/json", ...(init?.headers ?? {}) },
    });
  } catch {
    throw new MlError(0, "서버에 연결할 수 없습니다 — 서버가 실행 중인지 확인하세요.");
  }
  const body = await res.text();
  if (!res.ok) {
    let detail = body;
    try {
      const parsed = JSON.parse(body) as { detail?: unknown };
      if (typeof parsed.detail === "string") detail = parsed.detail;
    } catch {
      /* JSON 이 아니면 원문 그대로 */
    }
    throw new MlError(res.status, detail);
  }
  return JSON.parse(body) as T;
}

const post = <T>(path: string, body?: unknown) =>
  call<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });

/* ── 타입 ─────────────────────────────────────────────────────────── */

export type TargetKind = "auc" | "whsl" | "rtl";

/** 보고서 한 줄. 우리 에이전트가 공통으로 쓰는 모양이다. */
export interface Finding {
  level: string;
  title: string;
  detail?: string;
  /** 근거 수치. `[이름, 값]` 목록 — **조건 없는 수치는 안 담는다** */
  numbers?: [string, string][];
  advice?: string;
}

/**
 * 보고서 하나. **이름 칸이 `title` 이 아니라 `name` 입니다** — 우리 백엔드가
 * 그렇게 냅니다. `title` 로 읽으면 **오류 없이 제목만 빈칸**이 됩니다.
 */
export interface AgentReport {
  name: string;
  /** OK · Caution · Problem */
  verdict: string;
  findings: Finding[];
  at?: string;
  days?: number;
  run_id?: number | null;
  /** 뉴스 에이전트만 */
  date?: string;
}

/** 저장된 보고서 한 개. 파일이 원본입니다. */
export interface HistoryItem {
  file: string;
  kind: string;
  /** "12:22:25" · Claude 일별 점검은 시각이 없어 null */
  time?: string | null;
  /** 규칙 에이전트만 판정을 갖습니다. Claude 보고서는 null */
  verdict?: string | null;
  is_claude?: boolean;
  bytes?: number;
}
export interface HistoryDay {
  date: string;
  reports: HistoryItem[];
}

export interface BatchRun {
  run_id: number;
  started_at: string;
  finished_at: string | null;
  status: string;
  host?: string | null;
  /** 어떤 단계를 돌기로 했나 — 쉼표로 이어 붙인 이름들 */
  plan?: string | null;
  n_ok?: number;
  n_fail?: number;
  note?: string | null;
}

/** 재학습 판정 — 다시 배워야 하나. */
export interface RetrainStatus extends AgentReport {
  kind: string;
  /** 재학습 후보로 올라온 조합 수 */
  candidates: number;
  job: { state: string; started: string | null; kind: string | null };
  /** 되돌릴 수 있는 백업 이름들 (최신 순) */
  backups: string[];
}

/** 검증 표 한 줄. **`null` 은 0 이 아니라 «못 쟀다» 이다.** */
export interface VerifyItem {
  item: string;
  n: number;
  verdict: string;
  anchor: number | null;
  cur: number | null;
  cand: number | null;
  diff: number | null;
  need: number | null;
}

export interface GraphAsk {
  ask: string;
  hint?: string;
  verdict?: string;
  candidate?: string;
  verify?: string;
  items?: VerifyItem[];
}

export interface GraphStatus {
  kind: string;
  next: string[];
  values: Record<string, unknown>;
  asking: GraphAsk | null;
  running: { busy: boolean; since: string | null; answer: string | null };
  judge_text?: string | null;
  build_tail?: string | null;
  verify_items?: VerifyItem[];
  last_verify?: {
    at: string;
    candidate: string;
    passed: boolean;
    verdict: string;
    eval_from: string;
    items: VerifyItem[];
  } | null;
}

/* ── 부르는 곳 ────────────────────────────────────────────────────── */

/** 예측 하나를 왜 그렇게 냈는지. 규칙만 돌아 빠르다 (AI 안 부름). */
export const explain = (
  baseDt: string,
  item: string,
  kind: TargetKind,
  lead: number,
  showActual = true,
) =>
  call<AgentReport>(
    `agent/explain?base_dt=${encodeURIComponent(baseDt)}` +
      `&item=${encodeURIComponent(item)}&kind=${kind}&lead=${lead}` +
      `&show_actual=${showActual}`,
  );

/** 배치 장애 조사 — 실패했을 때만 할 말이 있다. */
export const batchAgent = () => call<AgentReport>("agent/batch");
/** 최근 배치 실행 목록. **배열이 그대로 옵니다** — 감싼 껍데기가 없습니다. */
export const batchRecent = () => call<BatchRun[]>("batch/recent");
/**
 * **아침에 저장된** 점검 결과를 그대로 읽는다. 다시 안 돌린다 — 즉시 온다.
 *
 * ★ `/quality` 와 같은 모양이라 화면이 **같은 그림**으로 그린다.
 * ★ `found: false` 는 «아직 없다» 이지 «정상이다» 가 아니다.
 */
export const qualitySaved = () =>
  call<AgentReport & { found: boolean; file?: string }>("quality/saved");

/** 데이터 품질 — DB 를 훑어 10초쯤 걸린다 (서버가 10분 캐시). */
/**
 * 지금 다시 잰다. **기억해 둔 것을 안 씁니다** (`fresh`).
 *
 * ★ 사람이 버튼을 누른 것은 «지금 이 순간을 재 달라» 는 뜻입니다.
 *   10분 전 답을 주면 눌러도 시각이 안 바뀌어 «안 먹혔다» 로 보입니다.
 */
export const qualityAgent = (days = 180) =>
  call<AgentReport>(`quality?days=${days}&fresh=1`);
/** 오늘 기사에서 우리 품목 이야기를 골라 온다 (서버가 30분 캐시). */
export const newsAgent = (date?: string) =>
  call<AgentReport>("agent/news" + (date ? `?date=${encodeURIComponent(date)}` : ""));
/** 지난 보고서 목록 — 날짜별로 묶여 온다. */
export const agentHistory = () => call<{ dates: HistoryDay[] }>("agent/history");
/** 보고서 한 개의 내용. 파일 이름만 넘긴다 (경로는 서버가 막는다). */
export const agentReport = (file: string) =>
  call<{ file: string; text: string; is_claude: boolean }>(
    `agent/report?file=${encodeURIComponent(file)}`,
  );

/** 사람이 눌러야 할 재학습 결정 하나. */
export interface PendingRetrain {
  kind: string;
  state: string;
  candidate?: string;
  items?: VerifyItem[];
  verify?: string;
}

/**
 * **지금 사람이 눌러야 할 결정이 있나.**
 *
 * ★ 화면이 「모델 재학습」 탭을 띄울지 정하는 데 씁니다. 후보가 현행보다
 *   **나을 때만** 여기 뜹니다 — 못하면 배치가 후보를 지우고 아무것도
 *   안 남깁니다. 사람이 볼 것이 없기 때문입니다.
 *
 * ★ `ran` 은 «판정이 한 번이라도 돌았나» 입니다. **«아직 안 돌았다» 와
 *   «돌았는데 없다» 는 다릅니다** — 앞은 고장일 수 있고 뒤는 정상입니다.
 */
export const retrainPending = () =>
  call<{ at: string | null; pending: PendingRetrain[]; ran: boolean }>("retrain/pending");

/* 재학습 — 이제 사람은 **한 번만** 누른다. 「바꾸기」 하나다. */
export const retrainStatus = (kind: TargetKind = "auc") =>
  call<RetrainStatus>(`retrain/status?kind=${kind}`);
export const graphStatus = (kind: TargetKind = "auc") =>
  call<GraphStatus>(`retrain/graph/status?kind=${kind}`);
export const graphAct = (kind: TargetKind, answer?: "build" | "apply" | "stop") =>
  post<{ ok: boolean }>(
    `retrain/graph/act?kind=${kind}` + (answer ? `&answer=${answer}` : ""),
  );
export const graphReset = (kind: TargetKind) =>
  post<{ ok: boolean }>(`retrain/graph/reset?kind=${kind}`);
