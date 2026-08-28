"use client";

import { useCallback, useRef, useState } from "react";

import { ResultScreen } from "@/components/ResultScreen";
import { RunScreen, type StageState } from "@/components/RunScreen";
import { loadScene } from "@/lib/api";
import { DEMO_DATES } from "@/lib/fixtures";
import type { ViewModel } from "@/lib/types";

const IDLE: StageState[] = ["idle", "idle", "idle", "idle"];
/** 단계 하나가 켜지고 꺼지는 간격. 녹화에서 읽을 수 있는 속도다. */
const STEP_MS = 640;
/**
 * 값이 다 차고 나서 결과 탭으로 넘어가기까지의 여유.
 *
 * 없으면 **각 파트가 낸 값을 볼 수 없다** — 값이 채워지는 순간과 탭 전환이 같은
 * 프레임에서 일어나서, 실행 경로에 재무 31,854,627원 · 물류 7,636.72kg ·
 * ML 1,881원이 찬 화면이 한 프레임도 안 보인 채 넘어간다. 그 장면이 시연의 핵심이다.
 */
const SETTLE_MS = 2_000;

export default function Home() {
  const [tab, setTab] = useState<"run" | "result">("run");
  const [asOf, setAsOf] = useState(DEMO_DATES[0]);
  const [states, setStates] = useState<StageState[]>(IDLE);
  const [vm, setVm] = useState<ViewModel | null>(null);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("대기");
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);

  const clearTimers = () => {
    timers.current.forEach(clearTimeout);
    timers.current = [];
  };

  /**
   * 실제 호출과 단계 표시를 **함께** 돌린다.
   *
   * 응답이 먼저 와도 단계 애니메이션이 끝날 때까지 기다린다 — 녹화에서 경로가
   * 보여야 하기 때문이고, 그동안 화면이 값을 지어내지 않도록 단계는 "조회 중"에
   * 머문다.
   */
  const run = useCallback(async () => {
    clearTimers();
    setBusy(true);
    setStatus("실행 중");
    setStates(IDLE);
    setVm(null);

    const reduced =
      typeof window !== "undefined" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const step = reduced ? 0 : STEP_MS;

    const paint = () =>
      new Promise<void>((resolve) => {
        if (step === 0) {
          setStates(["done", "done", "done", "done"]);
          resolve();
          return;
        }
        IDLE.forEach((_, i) => {
          timers.current.push(
            setTimeout(() => setStates((s) => s.map((v, j) => (j === i ? "run" : v))), step * i * 2),
          );
          timers.current.push(
            setTimeout(
              () => setStates((s) => s.map((v, j) => (j === i ? "done" : v))),
              step * (i * 2 + 1),
            ),
          );
        });
        timers.current.push(setTimeout(resolve, step * IDLE.length * 2));
      });

    const [next] = await Promise.all([loadScene(asOf), paint()]);
    setVm(next);
    setStatus(
      next.source === "api"
        ? `완료 · ${next.endCode ?? ""}`
        : "완료 · 표본으로 표시 (API 결과 아님)",
    );
    setBusy(false);
    // 타이머를 ref에 담는다 — 재실행·초기화가 이걸 취소하지 못하면, 사용자가
    // 실행 탭으로 되돌아온 뒤에 뒤늦게 결과 탭으로 튕긴다.
    timers.current.push(setTimeout(() => setTab("result"), SETTLE_MS));
  }, [asOf]);

  /** 장면 전환 = as_of 를 바꿔 **재호출**한다. 화면에 담아 둔 다른 응답을 꺼내는 게 아니다. */
  const flip = useCallback(async (next: string) => {
    setBusy(true);
    setAsOf(next);
    setStatus("실행 중");
    const loaded = await loadScene(next);
    setVm(loaded);
    setStates(["done", "done", "done", "done"]);
    setStatus(
      loaded.source === "api"
        ? `완료 · ${loaded.endCode ?? ""}`
        : "완료 · 표본으로 표시 (API 결과 아님)",
    );
    setBusy(false);
  }, []);

  const reset = () => {
    clearTimers();
    setStates(IDLE);
    setVm(null);
    setStatus("대기");
    setTab("run");
  };

  return (
    <main>
      <header className="sticky top-0 z-40 flex h-18 flex-wrap items-center gap-6 border-b border-rule bg-surface px-8">
        <div className="mr-auto flex items-center gap-3">
          <span className="grid size-8 place-items-center rounded-ctl bg-accent text-md2 font-semibold text-on-accent">
            햇
          </span>
          <b className="text-lg2 font-semibold tracking-tight">매입 시나리오 콘솔</b>
          <span className="text-sm2 text-ink-3">햇들농산 · 매입 에이전트 v1.1</span>
        </div>
        <div
          role="tablist"
          aria-label="화면"
          className="flex gap-1 rounded-card border border-rule bg-surface-2 p-1"
        >
          {(
            [
              ["run", "01", "실행", false],
              ["result", "02", "결과", vm === null],
            ] as const
          ).map(([key, num, label, disabled]) => (
            <button
              key={key}
              role="tab"
              type="button"
              aria-selected={tab === key}
              disabled={disabled}
              onClick={() => setTab(key)}
              className={`flex items-center gap-3 rounded-ctl px-5 py-2 text-md2 font-normal disabled:cursor-not-allowed disabled:opacity-40 ${
                tab === key ? "bg-surface font-semibold text-ink" : "text-ink-2"
              }`}
            >
              <span className={`font-mono text-xs2 ${tab === key ? "text-accent" : "text-ink-3"}`}>
                {num}
              </span>
              {label}
            </button>
          ))}
        </div>
      </header>

      <div className="mx-auto max-w-[1300px] px-8 pt-8 pb-28">
        {tab === "run" ? (
          <RunScreen
            asOf={asOf}
            onAsOf={setAsOf}
            onRun={run}
            onReset={reset}
            states={states}
            vm={vm}
            busy={busy}
            status={status}
          />
        ) : (
          vm && <ResultScreen vm={vm} onFlip={flip} busy={busy} />
        )}
      </div>
    </main>
  );
}
