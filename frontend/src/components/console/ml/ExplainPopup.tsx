"use client";

/**
 * 「왜 이렇게 봤나」 — 그래프의 점을 누르면 뜨는 팝업.
 *
 * ★ 원래는 그래프 아래에 붙여 뒀는데, 설명이 붙을수록 화면이 길어져
 *   그래프가 밀려났습니다. 팝업이면 **그래프가 제자리에 있는 채로** 봅니다.
 *
 * ★ **화면이 계산하지 않습니다.** 출발점 분해도, 중요도도 전부 서버가 DB 와
 *   저장된 모델에서 읽어 온 것입니다. 화면은 그리기만 합니다.
 *
 * ★ 규칙만 돌아서 빠릅니다 — AI 를 안 부릅니다.
 */

import { useEffect, useState } from "react";

import { explain, MlError, type AgentReport, type TargetKind } from "@/lib/mlConsole";

import { en, ITEM } from "./labels";
import { FindingCard, Verdict } from "./Report";

export function ExplainPopup({
  baseDt,
  item,
  kind,
  lead,
  targetDate,
  showActual = true,
  onClose,
}: {
  baseDt: string;
  item: string;
  kind: TargetKind;
  lead: number;
  targetDate?: string;
  /** 끄면 서버가 실제값 줄을 아예 안 넣습니다 (운영에서 보이는 모습). */
  showActual?: boolean;
  onClose: () => void;
}) {
  const [rep, setRep] = useState<AgentReport | null>(null);
  const [err, setErr] = useState<string | null>(null);

  //  ★ 고른 점이 바뀌면 **이 컴포넌트를 통째로 다시 답니다** (부르는 쪽에서
  //    `key` 를 줍니다). 그래서 여기서 옛 답을 지울 일이 없습니다 —
  //    효과 안에서 곧바로 상태를 비우면 화면이 한 번 더 그려집니다.
  useEffect(() => {
    //  늦게 온 답이 새 답을 덮지 않게 막는다.
    let alive = true;
    explain(baseDt, item, kind, lead, showActual)
      .then((r) => {
        if (alive) setRep(r);
      })
      .catch((e: unknown) => {
        if (alive) setErr(e instanceof MlError ? `[${e.status || "연결 안 됨"}] ${e.message}` : String(e));
      });
    return () => {
      alive = false;
    };
  }, [baseDt, item, kind, lead, showActual]);

  //  Esc 로 닫는다. 팝업을 띄웠으면 키보드로도 빠져나갈 수 있어야 한다.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      //  뒷배경을 누르면 닫힌다. 팝업 안을 눌렀을 때는 안 닫히게 막는다.
      onClick={onClose}
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto p-4 sm:p-8"
      style={{ background: "rgb(21 26 22 / .45)" }}
      role="dialog"
      aria-modal="true"
      aria-label="왜 이렇게 예측했나"
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-[640px] rounded-xl border bg-panel shadow-2xl"
        style={{ borderColor: "var(--color-hair)" }}
      >
        <header
          className="flex flex-wrap items-center gap-2 border-b px-4 py-3"
          style={{ borderColor: "var(--color-hair-soft)" }}
        >
          <h4 className="m-0 text-[14px] font-semibold">
            왜 이렇게 예측했나 — {en(ITEM, item)}
            {lead === 0 ? " · 기준일 당일" : ` · ${lead}영업일 뒤`}
          </h4>
          {targetDate && (
            <span className="font-mono text-[11.5px]" style={{ color: "var(--color-mut2)" }}>
              {targetDate}
            </span>
          )}
          {rep && <Verdict level={rep.verdict} />}
          <button
            type="button"
            onClick={onClose}
            aria-label="닫기"
            className="ml-auto rounded px-2 py-1 text-[13px] transition hover:bg-sunk"
            style={{ color: "var(--color-mut)" }}
          >
            ✕
          </button>
        </header>

        <div className="p-4">
          {err && (
            <p
              className="m-0 rounded-lg px-3.5 py-3 text-[12.5px]"
              style={{ background: "var(--color-t-bad-bg)", color: "var(--color-t-bad)" }}
            >
              {err}
            </p>
          )}
          {!rep && !err && (
            <p className="m-0 text-[12.5px]" style={{ color: "var(--color-mut2)" }}>
              불러오는 중…
            </p>
          )}
          {rep && (
            <ul className="m-0 flex list-none flex-col gap-2 p-0">
              {rep.findings.map((f, i) => (
                <FindingCard key={i} f={f} compact />
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}
