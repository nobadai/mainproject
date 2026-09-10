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
 *
 * ★ **크기는 사람이 정합니다.** 예전에는 너비 820px · 높이 `min(58vh, 520px)` 가
 *   박혀 있었습니다. 이제 위 모서리 손잡이를 끌어 높이를 정하고, 넓게 보기로 너비
 *   상한을 풉니다. 정한 값은 브라우저가 기억합니다 (`lib/dock_size.ts`).
 *
 *   🔴 리사이즈를 넣으면서 **위의 "지우지 않고 숨긴다" 구조는 그대로 둡니다.**
 *      손잡이는 판 *바깥*(위)에 붙는 남매 요소라 판 안의 대화를 건드리지 않습니다.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { MasterConsole } from "@/components/console/MasterConsole";
import {
  clampHeight,
  defaultHeight,
  maxHeight,
  readDockSize,
  writeDockSize,
  DEFAULT_HEIGHT_CSS,
  KEY_STEP,
  MAX_HEIGHT_CSS,
  MIN_HEIGHT,
  type DockSize,
} from "@/lib/dock_size";
import type { Session } from "@/lib/session";

export function MasterDock({ session }: { session: Session }) {
  const [open, setOpen] = useState(false);

  /**
   * 정해 둔 크기. `null` 은 **아직 저장소를 안 봤다** 는 뜻이다.
   *
   * ★ 첫 렌더에서 `localStorage` 를 보면 서버 렌더와 값이 갈려 하이드레이션이
   *   어긋난다. 서랍은 접힌 채로 뜨니 **열 때 처음 읽으면 된다** — 접혀 있는 동안은
   *   높이가 0 이라 어차피 쓸 데가 없다.
   */
  const [size, setSize] = useState<DockSize | null>(null);
  const [dragging, setDragging] = useState(false);
  const drag = useRef<{ startY: number; startHeight: number; height: number; wide: boolean } | null>(
    null,
  );

  /** 지금 값. 손잡이·버튼은 열린 뒤에만 도니까 저장소를 다시 볼 일은 거의 없다. */
  const current = useCallback((): DockSize => size ?? readDockSize(window.innerHeight), [size]);

  const commit = useCallback((next: DockSize) => {
    setSize(next);
    writeDockSize(next);
  }, []);

  const toggleOpen = useCallback(() => {
    setSize((prev) => prev ?? readDockSize(window.innerHeight));
    setOpen((v) => !v);
  }, []);

  /* ── 손잡이 ──────────────────────────────────────────────────────────
   *
   * 판의 **아래 모서리는 막대에 붙어 고정**이라, 위로 끌면 높이가 커진다.
   * ------------------------------------------------------------------ */

  const onPointerDown = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      if (e.pointerType === "mouse" && e.button !== 0) return;
      const start = current();
      drag.current = {
        startY: e.clientY,
        startHeight: start.height,
        height: start.height,
        wide: start.wide,
      };
      // 끄는 동안 글자가 딸려 잡히지 않게
      e.preventDefault();
      setDragging(true);
    },
    [current],
  );

  /**
   * 끄는 동안은 **창**이 듣는다.
   *
   * ★ 손잡이에만 걸면 손이 손잡이 밖으로 나가는 순간 놓친다 — 위로 끄는 동작은 거의
   *   언제나 밖으로 나간다. `setPointerCapture` 도 같은 일을 하지만, 창에 거는 쪽이
   *   마우스·손가락·펜을 가리지 않고 똑같이 돌아 예외가 없다.
   */
  useEffect(() => {
    if (!dragging) return;

    const onMove = (e: PointerEvent) => {
      const d = drag.current;
      if (!d) return;
      d.height = clampHeight(d.startHeight + (d.startY - e.clientY), window.innerHeight);
      setSize((prev) => (prev ? { ...prev, height: d.height } : prev));
    };

    const onEnd = () => {
      const d = drag.current;
      drag.current = null;
      setDragging(false);
      // 🔴 저장은 **손을 뗄 때 한 번**이다. 끄는 동안 매 프레임 쓰면 저장소가 쉴 새 없이 돈다.
      if (d) writeDockSize({ height: d.height, wide: d.wide });
    };

    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onEnd);
    window.addEventListener("pointercancel", onEnd);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onEnd);
      window.removeEventListener("pointercancel", onEnd);
    };
  }, [dragging]);

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>) => {
      if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
      e.preventDefault();
      const now = current();
      const step = e.key === "ArrowUp" ? KEY_STEP : -KEY_STEP;
      commit({ ...now, height: clampHeight(now.height + step, window.innerHeight) });
    },
    [commit, current],
  );

  /** 넓게 보기 — 켜면 너비 상한을 풀고 높이를 상한까지, 끄면 처음 크기로. */
  const toggleWide = useCallback(() => {
    const viewport = window.innerHeight;
    const now = current();
    commit(
      now.wide
        ? { height: defaultHeight(viewport), wide: false }
        : { height: maxHeight(viewport), wide: true },
    );
  }, [commit, current]);

  /**
   * ★ 대화 화면을 **한 번만 만든다.** 손잡이를 끄는 동안 이 서랍은 프레임마다 다시
   *   그려지는데, 그때마다 대화까지 같이 그리면 끌리는 것이 뚝뚝 끊긴다. 같은 요소를
   *   돌려주면 React 가 그 아래를 건너뛴다.
   */
  const consoleEl = useMemo(() => <MasterConsole session={session} />, [session]);

  const wide = size?.wide === true;
  const height = !open ? 0 : size ? `min(${size.height}px, ${MAX_HEIGHT_CSS})` : DEFAULT_HEIGHT_CSS;

  return (
    <aside
      className="fixed inset-x-0 bottom-0 z-40 flex flex-col md:left-[238px]"
      aria-label="마스터 에이전트"
    >
      <div className={`mx-auto flex w-full flex-col px-3 ${wide ? "" : "max-w-[820px]"}`}>
        {/* 손잡이 — 펼쳤을 때만. 접히면 잡을 판 자체가 없다 */}
        {open && size ? (
          <div
            className="flex items-center gap-2 rounded-t-2xl border border-b-0 px-3 pt-1.5 pb-1"
            style={{
              borderColor: "var(--color-hair)",
              background: "rgba(255,255,255,.97)",
              backdropFilter: "blur(14px)",
            }}
          >
            <div
              role="separator"
              aria-orientation="horizontal"
              aria-label="마스터 콘솔 높이"
              aria-valuenow={size.height}
              aria-valuemin={MIN_HEIGHT}
              aria-valuetext={`${size.height}픽셀`}
              tabIndex={0}
              title="끌어서 높이를 조절합니다 · 화살표 위아래로도 됩니다"
              onPointerDown={onPointerDown}
              onKeyDown={onKeyDown}
              className="group flex flex-1 cursor-ns-resize touch-none items-center justify-center rounded-md py-1.5 outline-none"
            >
              <span
                aria-hidden
                className="h-[3px] w-11 rounded-full bg-hair transition-colors group-hover:bg-mut2 group-focus-visible:bg-accent"
              />
            </div>
            <button
              type="button"
              onClick={toggleWide}
              onPointerDown={(e) => e.stopPropagation()}
              aria-pressed={wide}
              className="hover:bg-hair-soft shrink-0 rounded-md px-2 py-1 text-[11px] font-semibold transition-colors"
              style={{ color: "var(--color-mut2)" }}
            >
              {wide ? "원래대로" : "넓게"}
            </button>
          </div>
        ) : null}

        {/* 펼친 판 — 접혀도 지우지 않는다 (되묻던 확인이 사라지지 않게) */}
        <div
          className={`overflow-hidden border border-b-0 shadow-[0_-12px_40px_-24px_rgba(21,26,22,.5)] ${
            open ? "" : "rounded-t-2xl"
          } ${dragging ? "" : "transition-[height]"}`}
          style={{
            borderColor: "var(--color-hair)",
            background: "rgba(255,255,255,.97)",
            backdropFilter: "blur(14px)",
            // 손잡이가 위 테두리를 이미 그렸다. 두 줄로 보이지 않게 판은 위를 비운다
            borderTopWidth: open ? 0 : undefined,
            height,
          }}
          aria-hidden={!open}
        >
          <div className="h-full" style={{ display: open ? "block" : "none" }}>
            {consoleEl}
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
            onClick={toggleOpen}
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
            onClick={toggleOpen}
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
