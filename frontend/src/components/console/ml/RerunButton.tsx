"use client";

/**
 * 다시 돌리기 버튼 — 아침에 실패한 것을 지금 한 번 더.
 *
 * ★ **왜 있나.** 자동 작업과 AI 점검은 아침에 한 번만 돕니다. 실패하면
 *   **다음 날 아침까지 빈 채로 남습니다.** 2026-09-10 에 실제로 그랬습니다 —
 *   수집 검사가 헛경보로 배치를 세워, 매입 파트 전달표에 그날 것이 없었습니다.
 *   고친 뒤 사람이 터미널에서 손으로 돌려야 했습니다.
 *
 * ★ **누르면 바로 돌아오고, 끝나기를 여기서 기다립니다.** 자동 작업은
 *   10~15분 걸립니다. 요청을 붙들고 있으면 중간에 끊기고, 그러면 화면은
 *   «실패» 로 보이는데 뒤에서는 계속 돌고 있습니다 — 제일 나쁜 모양입니다.
 *   그래서 시작만 알리고 진행은 3초마다 따로 묻습니다.
 *
 * ★ **한 번에 하나만 돕니다.** 자동 작업이 학습표를 비우고 다시 채우므로,
 *   둘이 겹치면 한쪽이 비운 표를 다른 쪽이 읽습니다. 서버가 409 로 막고
 *   여기서는 그 말을 그대로 보입니다.
 *
 * ★ **끝나면 화면을 새로 읽습니다** (`onDone`). 안 그러면 새 결과와 낡은
 *   목록이 같은 화면에 같이 있게 됩니다.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { MlError, opsJob, opsRerun, type OpsJob } from "@/lib/mlConsole";

const say = (e: unknown) =>
  e instanceof MlError ? `[${e.status || "연결 안 됨"}] ${e.message}` : String(e);

/** 몇 분쯤 걸리는지 미리 말해 줍니다 — 안 그러면 멈춘 줄 압니다. */
const HOW_LONG: Record<string, string> = {
  batch: "10~15분",
  claude: "3~5분",
};

export function RerunButton({
  what,
  label,
  onDone,
}: {
  what: "batch" | "claude";
  /** 버튼에 쓸 말. 「다시 돌리기」·「갱신」 */
  label: string;
  onDone: () => void;
}) {
  const [job, setJob] = useState<OpsJob | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  //  ★ 이 창이 시작한 것만 끝났을 때 화면을 새로 읽습니다. 남이 돌린 것까지
  //    쫓아가면 보고 있던 화면이 제멋대로 바뀝니다.
  const mine = useRef(false);

  const poll = useCallback(async () => {
    try {
      const j = await opsJob();
      setJob(j);
      if (j.state !== "running" && mine.current) {
        mine.current = false;
        setBusy(false);
        onDone();
      }
      return j.state === "running";
    } catch (e) {
      setErr(say(e));
      setBusy(false);
      mine.current = false;
      return false;
    }
  }, [onDone]);

  //  열 때 한 번 봅니다 — 다른 창에서 돌리고 있을 수 있습니다.
  useEffect(() => {
    let alive = true;
    opsJob()
      .then((j) => alive && setJob(j))
      .catch(() => {
        /* 진행 상황을 못 읽어도 버튼은 눌러야 합니다 */
      });
    return () => {
      alive = false;
    };
  }, []);

  //  도는 동안만 3초마다 묻습니다. 안 돌 때는 아무것도 안 합니다.
  const running = job?.state === "running";
  useEffect(() => {
    if (!running) return;
    const t = setInterval(() => void poll(), 3000);
    return () => clearInterval(t);
  }, [running, poll]);

  const go = async () => {
    setErr(null);
    setBusy(true);
    try {
      await opsRerun(what);
      mine.current = true;
      await poll();
    } catch (e) {
      setErr(say(e));
      setBusy(false);
    }
  };

  //  다른 것이 돌고 있으면 이 버튼도 막습니다 — 서버가 어차피 409 입니다.
  const other = running && job?.what !== what;
  const disabled = busy || running;

  return (
    <div className="flex flex-col items-end gap-1">
      <button
        type="button"
        onClick={() => void go()}
        disabled={disabled}
        className="rounded-lg px-3 py-1.5 text-[12px] font-semibold transition disabled:cursor-not-allowed disabled:opacity-45"
        style={{ background: "var(--color-nav)", color: "#f4f3ee" }}
      >
        {running && job?.what === what ? "도는 중…" : label}
      </button>

      {running && (
        <span className="text-right text-[11px]" style={{ color: "var(--color-mut2)" }}>
          {other ? (
            <>{job?.label} 이(가) 먼저 돌고 있습니다 — 끝나면 누를 수 있습니다</>
          ) : (
            <>
              {job?.started} 에 시작 · {HOW_LONG[what]}쯤 걸립니다
            </>
          )}
        </span>
      )}

      {/*  ★ 끝난 뒤 «어떻게 끝났나» 를 말합니다. 조용히 사라지면 눌렀는지
             안 눌렀는지 알 수 없습니다. */}
      {!running && job?.what === what && job.state === "done" && (
        <span className="text-right text-[11px]" style={{ color: "var(--color-t-good)" }}>
          {job.ended} 에 잘 끝났습니다
        </span>
      )}
      {!running && job?.what === what && job.state === "failed" && (
        <span className="max-w-[380px] text-right text-[11px]" style={{ color: "var(--color-t-bad)" }}>
          또 실패했습니다 (종료코드 {job.code}) — 아래 기록을 보세요
          {job.log.length > 0 && (
            <>
              <br />
              <span className="font-mono">{job.log[job.log.length - 1]}</span>
            </>
          )}
        </span>
      )}

      {err && (
        <span className="max-w-[380px] text-right text-[11px]" style={{ color: "var(--color-t-bad)" }}>
          {err}
        </span>
      )}
    </div>
  );
}
