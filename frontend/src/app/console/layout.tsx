"use client";

/**
 * 운영 콘솔 셸 — 사이드바 · 상단 · 아래 서랍.
 *
 * ★ **탭마다 폴더가 하나입니다.** `console/finance/page.tsx` 는 재무 파트가
 *   고칩니다. 파일이 안 겹치므로 여섯 사람이 같이 작업해도 충돌하지 않습니다.
 *
 * ★ **이 파일은 값을 안 만듭니다.** 틀만 잡습니다. 숫자는 각 탭이 자기
 *   API 에서 받습니다.
 */

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useSyncExternalStore } from "react";

import { MasterDock } from "@/components/console/MasterDock";
//  기준일 선택기. 사용자가 날짜를 골라 그날 기준으로 본다.
import { asOfSnapshot, serverAsOf, setDemoAsOf, subscribeAsOf } from "@/lib/demo_as_of";
import {
  ROLE_LABEL,
  clearSession,
  serverSnapshot,
  sessionSnapshot,
  subscribeSession,
} from "@/lib/session";

const TABS = [
  { href: "/console", label: "대시보드", mark: "D", group: "top" },
  { href: "/console/forecast", label: "가격 예측", mark: "ML", group: "dept" },
  { href: "/console/purchase", label: "매입", mark: "PU", group: "dept" },
  //  ★ 「재고 · 물류」 → 「재고」 (`#812`). 옆 메뉴가 전부 두 글자(매입 · 재무 · 판매)인데
  //    여기만 길어 줄이 튀었고, 사용자가 이 화면을 부르는 말도 「재고」다.
  { href: "/console/inventory", label: "재고", mark: "LG", group: "dept" },
  { href: "/console/finance", label: "재무", mark: "FN", group: "dept" },
  { href: "/console/sales", label: "판매", mark: "SL", group: "dept" },
] as const;

/**
 * 기준일 선택기. 기능이라 남기고, 화면에는 「기준일」 라벨만 싣는다 (2026-09-15 결정).
 *
 * 운영에서 기준일은 스케줄러가 정한다 (`app/master/clock.py` · `#422`).
 */
function DemoAsOfPicker() {
  const asOf = useSyncExternalStore(subscribeAsOf, asOfSnapshot, serverAsOf);

  return (
    <span className="ml-auto flex items-center gap-2">
      <label
        className="flex items-center gap-1.5 text-[15.5px]"
        style={{ color: "var(--color-mut2)" }}
      >
        기준일
        <input
          type="date"
          value={asOf}
          onChange={(e) => {
            //  ★ 빈 값(달력을 지운 상태)은 무시한다. 날짜가 맞는지는 **서버가 답한다**
            //    — 개장일 판정 같은 규칙을 화면에 새로 만들지 않는다.
            if (e.target.value) setDemoAsOf(e.target.value);
          }}
          className="rounded-md border px-2 py-1 font-mono text-[15.5px]"
          style={{ borderColor: "var(--color-hair)", background: "var(--color-panel)" }}
        />
      </label>
    </span>
  );
}

export default function ConsoleLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();

  // localStorage 는 React 바깥이라 구독해서 읽는다 (`session.ts` 주석 참조)
  const session = useSyncExternalStore(subscribeSession, sessionSnapshot, serverSnapshot);
  // 🔴 **"아직 모른다" 와 "없다" 를 가른다.** 서버 렌더에는 저장소가 없어 첫
  //    렌더의 세션이 늘 null 이다. 그걸 "없다" 로 읽고 로그인으로 보내면
  //    로그인한 사용자가 새로고침마다 튕긴다 (실측으로 잡았던 것).
  const hydrated = useSyncExternalStore(subscribeSession, () => true, () => false);

  useEffect(() => {
    if (hydrated && session === null) router.replace("/");
  }, [hydrated, session, router]);

  if (!hydrated || !session) return null;

  const active =
    [...TABS].sort((a, b) => b.href.length - a.href.length).find((t) => pathname === t.href) ??
    TABS[0];

  return (
    <div
      //  ★ **`content-start` 가 있어야 좁은 화면에서 배경이 안 갈립니다** (2026-09-16).
      //    격자의 기본 `align-content` 는 «늘리기» 라, 화면보다 내용이 짧으면
      //    남는 세로 공간을 **두 줄에 똑같이 나눠 줍니다.** 그런데 첫 줄에 든
      //    왼쪽 띠(`nav`)는 좁은 화면에서 높이가 `h-14` 로 못 박혀 있어 늘어난
      //    칸을 안 채웁니다 — 그 빈자리로 바탕색(`--color-desk`)이 드러나
      //    **띠 바로 아래에 색이 다른 가로 줄**이 생깁니다.
      //    `content-start` 는 칸을 내용 높이 그대로 두고 위로 붙입니다.
      className="grid min-h-screen content-start [grid-template-columns:1fr] md:[grid-template-columns:238px_minmax(0,1fr)]"
      style={{ background: "var(--color-desk)" }}
    >
      <nav
        className="sticky top-0 z-30 flex h-14 flex-row items-center gap-3 px-4
          md:h-screen md:flex-col md:items-stretch md:gap-6 md:px-4 md:py-6"
        style={{ background: "var(--color-nav)", color: "var(--color-nav-ink)" }}
      >
        <div className="flex items-center gap-2.5 md:px-1.5">
          <span
            className="grid size-[26px] shrink-0 place-items-center rounded-[7px] font-mono text-[16px]"
            style={{ background: "var(--color-nav-on)", color: "#a8cbb2" }}
          >
            햇
          </span>
          <span className="min-w-0">
            <b className="block text-[18px] font-semibold" style={{ color: "#f0f2ec" }}>
              햇들농산
            </b>
            <small
              className="block text-[13.5px] uppercase tracking-[0.16em]"
              style={{ color: "var(--color-nav-cap)" }}
            >
              운영 콘솔
            </small>
          </span>
        </div>

        <div className="thin-scroll -mx-1 flex min-w-0 flex-1 flex-row gap-0.5 overflow-x-auto px-1 md:flex-col md:gap-3.5 md:overflow-visible">
          {(["top", "dept"] as const).map((group) => (
            <div key={group} className="flex flex-row gap-0.5 md:flex-col">
              {group === "dept" && (
                <div
                  className="hidden px-3 pb-1.5 pt-1 text-[13.5px] uppercase tracking-[0.16em] md:block"
                  style={{ color: "var(--color-nav-cap)" }}
                >
                  영역별 화면
                </div>
              )}
              {TABS.filter((t) => t.group === group).map((t) => {
                const on = t.href === active.href;
                return (
                  <Link
                    key={t.href}
                    href={t.href}
                    aria-current={on ? "page" : undefined}
                    className="flex shrink-0 items-center gap-2 whitespace-nowrap rounded-lg px-3 py-2 text-[16.5px] font-medium transition"
                    style={{
                      background: on ? "var(--color-nav-on)" : "transparent",
                      color: on ? "#f0f2ec" : "var(--color-nav-ink)",
                    }}
                  >
                    <span className="flex-1">{t.label}</span>
                    <span
                      className="font-mono text-[13.5px] tracking-wider"
                      style={{ color: on ? "#a8cbb2" : "var(--color-nav-cap)" }}
                    >
                      {t.mark}
                    </span>
                  </Link>
                );
              })}
            </div>
          ))}
        </div>

        <div className="hidden flex-col gap-2.5 md:flex">
          <div
            className="flex items-center gap-2.5 rounded-lg px-2.5 py-2"
            style={{ background: "rgba(255,255,255,.05)" }}
          >
            <span
              className="grid size-7 shrink-0 place-items-center rounded-full text-[15px] font-semibold"
              style={{ background: "var(--color-nav-on)", color: "#a8cbb2" }}
            >
              {session.name.slice(0, 1)}
            </span>
            <span className="min-w-0 flex-1">
              <b className="block truncate text-[16px]" style={{ color: "#f0f2ec" }}>
                {session.name}
              </b>
              <small className="block text-[14px]" style={{ color: "var(--color-nav-cap)" }}>
                {ROLE_LABEL[session.role]}
              </small>
            </span>
            <button
              type="button"
              onClick={() => {
                clearSession();
                router.replace("/");
              }}
              className="rounded-md px-2 py-1 text-[14.5px] transition hover:bg-white/10"
              style={{ color: "var(--color-nav-cap)" }}
            >
              나가기
            </button>
          </div>
        </div>
      </nav>

      <div className="flex min-w-0 flex-col">
        <header
          className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b px-5 py-3.5"
          style={{ borderColor: "var(--color-hair)", background: "var(--color-panel)" }}
        >
          <h1 className="m-0 text-[21px] font-semibold tracking-[-0.02em]">{active.label}</h1>
          <DemoAsOfPicker />
        </header>

        {/* 아래 서랍이 화면을 가리므로 바닥에 자리를 비워 둔다 */}
        <main className="flex min-w-0 flex-1 flex-col gap-4 px-5 pb-28 pt-5">{children}</main>
      </div>

      <MasterDock session={session} />
    </div>
  );
}
