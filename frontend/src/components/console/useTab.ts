"use client";

/**
 * 탭 하나가 값을 받아오는 갈고리.
 *
 * ★ 여섯 탭이 다 같은 모양이라 한 군데로 모읍니다 — 읽는 중 · 실패 · 값.
 *   탭마다 따로 쓰면 어떤 탭은 실패를 안 보여주는 일이 생깁니다.
 *   **실제로 겪었습니다** (모델관리 패널이 실패해도 아무것도 안 뜨던 일).
 *
 * ★ **`key` 가 바뀔 때만 다시 받습니다.** 품목·상태·작은탭을 바꾸면 그 값이
 *   `key` 로 들어옵니다. 받아오는 함수는 `ref` 에 넣어 두는데, 매 렌더마다
 *   새로 만들어지는 화살표 함수라 그걸 의존성에 넣으면 **끝없이 다시 받습니다.**
 *
 * ★ **새 값이 올 때까지 옛 값을 지우지 않습니다.** 지우면 탭을 누를 때마다
 *   화면이 한 번 비었다가 다시 그려져 깜빡입니다.
 */

import { useEffect, useRef, useState } from "react";

import { DEFAULT_AS_OF } from "@/lib/demo_as_of";
import { ScreenError } from "@/lib/screen";

/**
 * 기본 기준일. 🔴 **값의 주인은 `lib/demo_as_of.ts` 하나다** (`#431`).
 *
 * 예전에는 이 파일이 `2026-01-06`, `lib/api.ts` 가 `2025-12-31` 을 각자 들고 있었다.
 * 지금 화면이 실제로 보는 값은 각 탭이 `asOfSnapshot()` 으로 읽는다.
 */
export const AS_OF = DEFAULT_AS_OF;

interface State<T> {
  data: T | null;
  error: string | null;
}

export function useTab<T>(key: string, load: () => Promise<T>): State<T> {
  const [state, setState] = useState<State<T>>({ data: null, error: null });

  //  최신 클로저를 담아 둔다. 렌더 중이 아니라 **효과 안에서** 넣는다.
  const loader = useRef(load);
  useEffect(() => {
    loader.current = load;
  });

  useEffect(() => {
    let alive = true;
    loader
      .current()
      .then((value) => {
        if (alive) setState({ data: value, error: null });
      })
      .catch((error: unknown) => {
        if (!alive) return;
        //  ★ 서버가 낸 문장을 그대로 올린다. "오류가 발생했습니다" 로 덮으면
        //    무엇을 고쳐야 하는지 알려주는 말이 사라진다.
        setState((prev) => ({
          data: prev.data,
          error:
            error instanceof ScreenError
              ? `[${error.status || "연결 실패"}] ${error.message}`
              : String(error),
        }));
      });
    return () => {
      alive = false;
    };
  }, [key]);

  return state;
}
