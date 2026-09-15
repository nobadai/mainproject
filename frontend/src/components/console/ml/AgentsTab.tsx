"use client";

/**
 * AI 보고서 — 데이터 이상 · 오늘 뉴스 · 지난 기록.
 *
 * ★ **판단은 규칙이 하고, 설명만 AI 가 합니다.** 이상을 찾는 것은 코드이고,
 *   사람 말로 옮기는 데만 AI 를 씁니다. AI 가 조용히 죽으면 «이상 없음»
 *   처럼 보이는데, 그게 제일 위험합니다.
 *
 * ★ 품질 검사는 DB 를 훑어 10초쯤, 뉴스는 30초쯤 걸립니다. 서버가 캐시하지만
 *   처음 열 때는 기다립니다 — 그래서 **누를 때만** 부릅니다.
 *
 * ★ **금일 Claude 점검만 맨 위에 펼쳐 둡니다.** 이건 매일 아침 배치가 끝난 뒤
 *   한 번 도는 사후 점검이고, 하루에 하나뿐이며, 그날 무슨 일이 있었는지가
 *   전부 여기 적힙니다. 지난 기록 목록에 섞어 두면 **찾아서 눌러야** 보입니다 —
 *   사흘 동안 실패 알림을 아무도 안 열어본 일이 그래서 생겼습니다.
 */

import { useCallback, useEffect, useState } from "react";

import {
  agentHistory,
  agentReport,
  MlError,
  qualityAgent,
  qualitySaved,
  type AgentReport,
  type HistoryDay,
  type HistoryItem,
} from "@/lib/mlConsole";

import { en, REPORT_KIND } from "./labels";
import { Markdownish } from "./Markdownish";
import { ReportBody, Verdict } from "./Report";
import { RerunButton } from "./RerunButton";

//  ★ 보고서 맨 위 «기계 번역» 안내(`>` 인용 칸)는 화면에서 뺍니다 (2026-09-11).
//    **파일에는 그대로 남습니다** — 번역 숫자 대조도 계속 돕니다.
//    맨 위 인용만 떼고, 본문 중간의 인용은 그대로 그립니다.
function dropNotice(text: string): string {
  return text
    .replace(/^(?:[ \t]*>.*(?:\r?\n|$))+\s*/, "")
    //  안내 칸 바로 밑의 가로줄(---)도 같이 뗍니다. 남으면 맨 위에 선만 하나 뜹니다.
    .replace(/^---[ \t]*(?:\r?\n|$)\s*/, "");
}

const say = (e: unknown) =>
  e instanceof MlError ? `[${e.status || "연결 안 됨"}] ${e.message}` : String(e);

function Card({
  title,
  subtitle,
  children,
  right,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
  right?: React.ReactNode;
}) {
  return (
    <section
      className="flex flex-col gap-3.5 rounded-xl border bg-panel p-4"
      style={{ borderColor: "var(--color-hair)" }}
    >
      <header className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <h2 className="m-0 text-[13.5px] font-semibold">{title}</h2>
        {subtitle && (
          <span className="text-[11.5px]" style={{ color: "var(--color-mut)" }}>
            {subtitle}
          </span>
        )}
        {right && <div className="ml-auto">{right}</div>}
      </header>
      {children}
    </section>
  );
}

function RunButton({
  onClick,
  busy,
  children,
}: {
  onClick: () => void;
  busy: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={busy}
      className="rounded-lg px-3 py-1.5 text-[12px] font-semibold transition disabled:cursor-not-allowed disabled:opacity-45"
      style={{ background: "var(--color-nav)", color: "#f4f3ee" }}
    >
      {busy ? "검사 중…" : children}
    </button>
  );
}

/**
 * 데이터 이상 점검 — **오늘 아침 결과를 바로 보입니다.**
 *
 * ★ 전에는 누를 때만 돌았습니다. 그런데 이 검사는 **매일 아침 자동으로
 *   돕니다.** 결과가 있는데 사람에게 또 누르라고 하면, 안 누른 날은
 *   못 본 것이 됩니다.
 *
 * ★ **저장된 것과 방금 돌린 것을 같은 그림으로 그립니다.** 같은 내용인데
 *   두 가지 모양으로 보이면 사람이 헷갈립니다. 그래서 서버가 둘을 같은
 *   모양으로 냅니다 (`/quality/saved` · `/quality`).
 *
 * ★ 버튼은 남깁니다 — **지금 이 순간을 다시 재고 싶을 때**가 있습니다.
 *   DB 를 훑어 10초쯤 걸립니다.
 */
function QualityCard({ onDone }: { onDone: () => void }) {
  const [rep, setRep] = useState<AgentReport | null>(null);
  const [when, setWhen] = useState<"저장" | "방금" | "없음" | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    qualitySaved()
      .then((r) => {
        if (!alive) return;
        //  ★ «없다» 와 «정상이다» 를 가릅니다. 아침 점검이 실패한 날에
        //    «정상» 으로 보이면 안 됩니다.
        if (r.found) {
          setRep(r);
          setWhen("저장");
        } else {
          setWhen("없음");
        }
      })
      .catch((e: unknown) => {
        if (alive) setErr(say(e));
      });
    return () => {
      alive = false;
    };
  }, []);

  const go = () => {
    setBusy(true);
    setErr(null);
    qualityAgent(180)
      .then((r) => {
        setRep(r);
        setWhen("방금");
        //  ★ 다시 잰 결과도 파일로 남습니다. **아래 「지난 진단 보고서」
        //    목록도 새로 읽어야** 방금 것이 거기 보입니다. 안 그러면
        //    같은 화면에 새 결과와 낡은 목록이 같이 있게 됩니다.
        onDone();
      })
      .catch((e: unknown) => setErr(say(e)))
      .finally(() => setBusy(false));
  };

  return (
    <Card
      title="데이터 이상 점검"
      subtitle="자동 작업한 데이터의 품질 검사 항목입니다."
      right={
        <RunButton onClick={go} busy={busy}>
          새로고침
        </RunButton>
      }
    >
      {err && (
        <p
          className="m-0 rounded-lg px-3.5 py-2.5 text-[12px]"
          style={{ background: "var(--color-t-bad-bg)", color: "var(--color-t-bad)" }}
        >
          {err}
        </p>
      )}
      {when && when !== "없음" && (
        <p className="m-0 text-[11.5px]" style={{ color: "var(--color-mut2)" }}>
          {when === "방금"
            ? "방금 다시 잰 결과입니다"
            : "매일 아침 자동으로 점검한 결과입니다"}
        </p>
      )}
      {when === "없음" && !err && (
        <p className="m-0 text-[12px]" style={{ color: "var(--color-mut2)" }}>
          점검 결과가 아직 없습니다 — 아침 자동 점검 뒤에 채워집니다.
        </p>
      )}
      {!when && !err && (
        <p className="m-0 text-[12px]" style={{ color: "var(--color-mut2)" }}>
          읽는 중…
        </p>
      )}
      {rep && <ReportBody report={rep} />}
    </Card>
  );
}

/**
 * 금일 Claude 점검 — 하루에 하나, 펼쳐서 보입니다.
 *
 * ★ **판정 배지가 없습니다.** 규칙 에이전트만 정상/주의/이상을 냅니다.
 *   Claude 보고서에 화면이 임의로 배지를 달면, 안 읽고 색만 보게 됩니다.
 */
function TodayClaude({
  day,
  pick,
  onDone,
}: {
  day: HistoryDay | null;
  pick: HistoryItem | null;
  onDone: () => void;
}) {
  const [text, setText] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const file = pick?.file ?? null;

  useEffect(() => {
    if (!file) return;
    let alive = true;
    agentReport(file)
      .then((r) => alive && setText(r.text))
      .catch((e: unknown) => alive && setErr(say(e)));
    return () => {
      alive = false;
    };
  }, [file]);

  if (!day || !pick)
    return (
      <Card
        title="금일 AI 진단"
        subtitle="자동 작업에 대한 AI 보고서입니다."
        //  ★ **없을 때야말로 버튼이 필요합니다.** 아침에 이게 실패하는 일이
        //    실제로 있었습니다 (로그인 만료 · 인코딩 사고). 그러면 그날은
        //    진단이 통째로 빕니다.
        right={<RerunButton what="claude" label="지금 만들기" onDone={onDone} />}
      >
        <p className="m-0 text-[12px]" style={{ color: "var(--color-mut2)" }}>
          오늘 진단 결과가 아직 없습니다 — 아침 작업이 끝난 뒤 실행됩니다.
        </p>
      </Card>
    );

  return (
    <Card
      title="금일 AI 진단"
      subtitle={`${day.date} · 자동 작업에 대한 AI 보고서입니다.`}
      right={
        <div className="flex items-start gap-2.5">
          {/*  ★ 배치를 다시 돌린 뒤에는 진단도 다시 받아야 합니다 — 아침 것은
                 실패한 배치를 보고 쓴 글이라 이미 틀린 이야기입니다. */}
          <RerunButton what="claude" label="갱신" onDone={onDone} />
        </div>
      }
    >
      {err && (
        <p
          className="m-0 rounded-lg px-3.5 py-2.5 text-[12px]"
          style={{ background: "var(--color-t-bad-bg)", color: "var(--color-t-bad)" }}
        >
          {err}
        </p>
      )}
      {!text && !err && (
        <p className="m-0 text-[12px]" style={{ color: "var(--color-mut2)" }}>
          불러오는 중…
        </p>
      )}
      {text && (
        <div
          className="thin-scroll max-h-[640px] overflow-auto rounded-lg border px-4 py-3"
          style={{ borderColor: "var(--color-hair)" }}
        >
          <Markdownish text={dropNotice(text)} />
        </div>
      )}
    </Card>
  );
}

/** 지난 보고서 — 날짜별로 묶여 온다. */
function History({ days, err, skip }: { days: HistoryDay[] | null; err: string | null; skip: string | null }) {
  const [open, setOpen] = useState<string | null>(null);
  const [text, setText] = useState<string>("");

  const show = (file: string) => {
    if (open === file) {
      setOpen(null);
      return;
    }
    setOpen(file);
    setText("불러오는 중…");
    agentReport(file)
      .then((r) => setText(r.text))
      .catch((e: unknown) => setText(say(e)));
  };

  if (err)
    return (
      <Card title="지난 진단 보고서" subtitle="날짜별">
        <p className="m-0 text-[12px]" style={{ color: "var(--color-t-bad)" }}>
          {err}
        </p>
      </Card>
    );

  return (
    <Card title="지난 진단 보고서" subtitle="AI가 남긴 점검 기록 — 날짜별 보기">
      {!days && (
        <p className="m-0 text-[12px]" style={{ color: "var(--color-mut2)" }}>
          불러오는 중…
        </p>
      )}
      {days?.length === 0 && (
        <p className="m-0 text-[12px]" style={{ color: "var(--color-mut2)" }}>
          남겨진 보고서가 없습니다.
        </p>
      )}
      <div className="flex flex-col gap-2.5">
        {days?.slice(0, 14).map((d) => {
          //  ★ 맨 위에 펼쳐 둔 것은 여기서 뺍니다 — 같은 것이 두 번 보이면
          //    어느 쪽이 최신인지 헷갈립니다.
          const reports = d.reports.filter((f) => f.file !== skip);
          if (reports.length === 0) return null;
          return (
            <div key={d.date}>
              <p className="m-0 mb-1 font-mono text-[11.5px]" style={{ color: "var(--color-mut)" }}>
                {d.date}
              </p>
              <ul className="m-0 flex list-none flex-wrap gap-1.5 p-0">
                {reports.map((f) => (
                  <li key={f.file}>
                    <button
                      type="button"
                      onClick={() => show(f.file)}
                      className="flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-[11.5px] transition hover:bg-sunk"
                      style={{
                        borderColor: open === f.file ? "var(--color-t-info)" : "var(--color-hair)",
                      }}
                    >
                      <span>{en(REPORT_KIND, f.kind)}</span>
                      {/*  ★ 같은 종류가 하루에 여러 번 남습니다 (재학습 검증이
                             다섯 번 도는 날도 있습니다). 시각이 없으면 어느
                             것이 어느 것인지 못 고릅니다. */}
                      {f.time && (
                        <span className="font-mono text-[10.5px]" style={{ color: "var(--color-mut2)" }}>
                          {f.time.slice(0, 5)}
                        </span>
                      )}
                      {f.verdict && <Verdict level={f.verdict} />}
                      {f.is_claude && (
                        <span className="text-[10px]" style={{ color: "var(--color-t-sim)" }}>
                          AI
                        </span>
                      )}
                    </button>
                  </li>
                ))}
              </ul>
              {open && reports.some((f) => f.file === open) && (
                <div
                  className="thin-scroll mt-2 max-h-80 overflow-auto rounded-lg border p-3"
                  style={{ borderColor: "var(--color-hair)", background: "var(--color-sunk)" }}
                >
                  {reports.find((f) => f.file === open)?.is_claude ? (
                    <Markdownish text={dropNotice(text)} />
                  ) : (
                    //  ★ `.txt` 는 수치가 세로로 줄 맞춰져 있습니다.
                    //    문서로 그리면 줄 맞춤이 깨집니다.
                    <pre className="tabular m-0 whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed">
                      {text}
                    </pre>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </Card>
  );
}

export function AgentsTab() {
  //  ★ 기록은 **한 번만** 받습니다. 맨 위 카드와 아래 목록이 따로 받으면
  //    같은 것을 두 번 묻게 됩니다.
  const [days, setDays] = useState<HistoryDay[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const reload = useCallback(() => {
    agentHistory()
      .then((r) => setDays(r.dates))
      .catch((e: unknown) => setErr(say(e)));
  }, []);

  useEffect(() => {
    let alive = true;
    agentHistory()
      .then((r) => alive && setDays(r.dates))
      .catch((e: unknown) => alive && setErr(say(e)));
    return () => {
      alive = false;
    };
  }, []);

  //  가장 최근 날짜 중 Claude 가 남긴 것. 없으면 카드가 그렇게 말합니다.
  const today = days?.[0] ?? null;
  const claude = today?.reports.find((r) => r.is_claude) ?? null;

  return (
    <div className="flex flex-col gap-4">
      <TodayClaude day={today} pick={claude} onDone={reload} />
      <QualityCard onDone={reload} />
      <History days={days} err={err} skip={claude?.file ?? null} />
    </div>
  );
}
