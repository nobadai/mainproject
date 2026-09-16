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
 * ★ **높이는 사람이 정합니다.** 예전에는 높이 `min(58vh, 520px)` 가 박혀 있었습니다.
 *   이제 위 모서리 손잡이를 끌어 정하고, 그 값을 브라우저가 기억합니다
 *   (`lib/dock_size.ts`).
 *
 *   🔴 리사이즈를 넣으면서 **위의 "지우지 않고 숨긴다" 구조는 그대로 둡니다.**
 *      손잡이는 판 *바깥*(위)에 붙는 남매 요소라 판 안의 대화를 건드리지 않습니다.
 *
 * ★ **채팅 바깥을 누르면 닫힙니다.** 예전에는 탭을 누르면 높이만 `MIN_HEIGHT` 로
 *   줄였습니다(«양보»). 화면 주인이 **"작아지는 게 아니라 닫혀야 한다"** 고 해서
 *   바꿨습니다.
 *
 *   ★ **높이는 안 건드립니다.** 닫는 것은 보여 주는 일뿐이고, 사람이 손잡이로 정해
 *     둔 높이(`lib/dock_size.ts`)는 그대로 남습니다. 다시 열면 그 높이로 돌아옵니다.
 *
 *   ★ **«바깥» 은 이 `aside` 바깥입니다.** 대화·손잡이·열기 버튼이 모두 이 안에
 *     들어 있어 한 번에 걸립니다. 지금 화면에는 `createPortal` 이 한 군데도 없어서
 *     (2026-09-16 실측) 판 안에서 뜨는 팝업도 전부 이 안에 그려집니다. 🔴 나중에
 *     포털로 띄우는 달력·메뉴를 판 안에 넣거든 **여기 검사도 같이 고쳐야 합니다** —
 *     안 고치면 달력을 누르는 순간 서랍이 닫힙니다.
 *
 *   ★ **누르기 시작한 자리로 판단합니다** (`pointerdown`). 판 안에서 글을 끌어
 *     선택하다 손이 바깥으로 나가는 일은 흔한데, 그때 닫으면 쓰던 글이 사라집니다.
 *     떼는 자리가 아니라 **잡는 자리**를 보므로 그런 일이 없습니다.
 *
 * ★ **너비는 늘 꽉 찹니다 — 사람이 고르는 것이 아닙니다.**
 *   하루 동안 너비 상한 820px 과 그것을 푸는 「넓게」 버튼이 있었습니다. 화면 주인이
 *   그것을 보고 **"그게 아니라 네비 오른쪽을 다 채워라"** 고 해서 상한과 버튼을 함께
 *   걷었습니다. **다시 넣지 마십시오.**
 *
 *   ★ 채우는 일은 두 줄이 나눠 합니다. 바깥 `aside` 가 `md:left-[238px]` 로 왼쪽
 *     네비를 피하고, 안쪽 판은 `w-full` 로 그 나머지를 다 씁니다. 둘 다 CSS 라
 *     **창을 줄이는 동안 JS 없이 따라 줄어듭니다.** 좌우 `px-3` 만 남겨 둡니다 —
 *     글이 화면 끝에 딱 붙으면 읽기 나쁩니다.
 */

import { usePathname } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { MasterConsole } from "@/components/console/MasterConsole";
import {
  clampHeight,
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
  const drag = useRef<{ startY: number; startHeight: number; height: number } | null>(null);

  /** 지금 쓰는 값. 손잡이·버튼은 열린 뒤에만 도니까 저장소를 다시 볼 일은 거의 없다. */
  const current = useCallback((): DockSize => size ?? readDockSize(window.innerHeight), [size]);

  const commit = useCallback((next: DockSize) => {
    setSize(next);
    writeDockSize(next);
  }, []);

  /**
   * 여닫기.
   *
   * 🔴 **높이는 여기서 안 건드린다.** 닫아도 `size` 는 그대로 남고 저장소도 안 지운다.
   *    그래서 다시 열면 직전 높이 그대로다. 아직 저장소를 안 봤으면(첫 열기) 그때 읽는다.
   */
  const toggleOpen = useCallback(() => {
    setSize((prev) => prev ?? readDockSize(window.innerHeight));
    setOpen((v) => !v);
  }, []);

  /* ── 탭을 누르면 닫는다 ───────────────────────────────────────────────
   *
   * 탭을 누른 사람은 **그 화면을 보려는 것**이다. 아래 «바깥 누르기» 로도 대개
   * 걸리지만, 뒤로 가기처럼 **누르지 않고 일어나는 이동**은 여기서만 잡힌다.
   * ------------------------------------------------------------------ */

  const pathname = usePathname();
  const firstPath = useRef(true);
  useEffect(() => {
    // 🔴 첫 렌더에서는 아무것도 하지 않는다. 서랍은 어차피 접힌 채로 뜨고, 여기서
    //    건드리면 뜨자마자 한 번 깜빡인다.
    if (firstPath.current) {
      firstPath.current = false;
      return;
    }
    setOpen(false);
  }, [pathname]);

  /* ── 채팅 바깥을 누르면 닫는다 ────────────────────────────────────────
   *
   * ★ **떼는 자리가 아니라 잡는 자리**(`pointerdown`)로 판단한다 — 판 안에서 글을
   *   끌어 선택하다 손이 밖으로 나가도 닫히지 않는다 (머리말 참고).
   *
   * ★ **캡처 단계**에서 듣는다. 판 안의 버튼이 `stopPropagation` 을 하든, 눌린 요소가
   *   그 자리에서 화면에서 사라지든, 우리가 먼저 보므로 «안이었나» 를 틀리지 않는다.
   * ------------------------------------------------------------------ */

  const rootRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    // 접혀 있으면 닫을 것이 없다 — 듣지도 않는다
    if (!open) return;

    const onPointerDownAnywhere = (e: PointerEvent) => {
      const root = rootRef.current;
      const target = e.target;
      // 🔴 판단할 수 없으면 **그대로 둔다.** 모르는 것을 닫는 쪽으로 해석하지 않는다
      if (!root || !(target instanceof Node)) return;
      if (root.contains(target)) return;
      setOpen(false);
    };

    document.addEventListener("pointerdown", onPointerDownAnywhere, true);
    return () => document.removeEventListener("pointerdown", onPointerDownAnywhere, true);
  }, [open]);

  /* ── 손잡이 ──────────────────────────────────────────────────────────
   *
   * 판의 **아래 모서리는 막대에 붙어 고정**이라, 위로 끌면 높이가 커진다.
   * ------------------------------------------------------------------ */

  const onPointerDown = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      if (e.pointerType === "mouse" && e.button !== 0) return;
      const start = current();
      drag.current = { startY: e.clientY, startHeight: start.height, height: start.height };
      setSize((prev) => (prev ? { ...prev, height: start.height } : { height: start.height }));
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
      if (d) writeDockSize({ height: d.height });
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

  /**
   * ★ 대화 화면을 **한 번만 만든다.** 손잡이를 끄는 동안 이 서랍은 프레임마다 다시
   *   그려지는데, 그때마다 대화까지 같이 그리면 끌리는 것이 뚝뚝 끊긴다. 같은 요소를
   *   돌려주면 React 가 그 아래를 건너뛴다.
   */
  const consoleEl = useMemo(() => <MasterConsole session={session} />, [session]);

  /**
   * 눈에 보이는 높이.
   *
   * ★ **접히면 0 이다 — 저장된 높이는 그대로 남아 있고**, 여기서만 접는다.
   *   그래서 다시 열면 직전 높이로 돌아온다.
   */
  const shownHeight = size?.height ?? null;
  const height = !open
    ? 0
    : shownHeight !== null
      ? `min(${shownHeight}px, ${MAX_HEIGHT_CSS})`
      : DEFAULT_HEIGHT_CSS;

  return (
    <aside
      ref={rootRef}
      className="fixed inset-x-0 bottom-0 z-40 flex flex-col md:left-[238px]"
      aria-label="마스터 에이전트"
    >
      {/* 너비 상한을 걸지 않는다 — 네비 오른쪽을 끝까지 채운다 (머리말 참고) */}
      <div className="flex w-full flex-col px-3">
        {/* 손잡이 — 펼쳤을 때만. 접히면 잡을 판 자체가 없다 */}
        {open && size ? (
          <div
            className="flex items-center rounded-t-2xl border border-b-0 px-3 pt-1.5 pb-1"
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
