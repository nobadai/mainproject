"use client";

/**
 * 마스터 서랍 — 어느 탭에서나 화면 아래에 붙어 있다.
 *
 * ★ **데모가 비워 둔 자리입니다** (`dock` · `dockPanel` · `dockBar`).
 *   껍데기만 있고 안이 비어 있어서, 우리가 쓰던 대화 화면을 그대로 넣습니다.
 *
 * ★ **접혀 있을 때도 살아 있습니다.** 대화 내용은 `MasterConsole` 안에
 *   있는데, 접었다고 지우면 되묻는 중이던 확인이 사라집니다. 그래서
 *   **없애지 않고 숨깁니다** — `hidden` 이 아니라 높이 0 으로.
 */

import { useState } from "react";

import { MasterConsole } from "@/components/console/MasterConsole";
import type { Session } from "@/lib/session";

export function MasterDock({ session }: { session: Session }) {
  const [open, setOpen] = useState(false);

  return (
    <aside
      className="fixed inset-x-0 bottom-0 z-40 flex flex-col md:left-[238px]"
      aria-label="마스터 에이전트"
    >
      <div className="mx-auto flex w-full max-w-[820px] flex-col px-3">
        {/* 펼친 판 — 접혀도 지우지 않는다 (되묻던 확인이 사라지지 않게) */}
        <div
          className="overflow-hidden rounded-t-2xl border border-b-0 shadow-[0_-12px_40px_-24px_rgba(21,26,22,.5)] transition-[height]"
          style={{
            borderColor: "var(--color-hair)",
            background: "rgba(255,255,255,.97)",
            backdropFilter: "blur(14px)",
            height: open ? "min(58vh, 520px)" : 0,
          }}
          aria-hidden={!open}
        >
          <div className="h-full" style={{ display: open ? "block" : "none" }}>
            <MasterConsole session={session} />
          </div>
        </div>

        {/* 늘 보이는 막대 */}
        <div
          className="flex items-center gap-2.5 rounded-t-2xl border border-b-0 px-4 py-2.5"
          style={{
            borderColor: "var(--color-hair)",
            background: "rgba(255,255,255,.97)",
            backdropFilter: "blur(14px)",
            borderTopLeftRadius: open ? 0 : undefined,
            borderTopRightRadius: open ? 0 : undefined,
          }}
        >
          <span
            aria-hidden
            className="size-[7px] shrink-0 rounded-full"
            style={{ background: "var(--color-t-good)", boxShadow: "0 0 0 3px var(--color-t-good-bg)" }}
          />
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
            className="flex min-w-0 flex-1 items-center gap-2 text-left"
          >
            <b className="text-[12.5px] font-semibold">마스터에게 묻기</b>
            <span className="truncate text-[11px]" style={{ color: "var(--color-mut2)" }}>
              {open ? "접으려면 누르세요" : "오늘 배추 얼마나 사야 해? · 창고에 얼마나 남았어?"}
            </span>
          </button>
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
            className="shrink-0 rounded-lg px-3 py-1.5 text-[12px] font-semibold transition"
            style={{ background: "var(--color-nav)", color: "#f4f3ee" }}
          >
            {open ? "접기" : "열기"}
          </button>
        </div>
      </div>
    </aside>
  );
}
