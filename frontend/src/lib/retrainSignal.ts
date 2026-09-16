"use client";

/**
 * 「모델이 바뀌었다」 를 **화면끼리 알리는 신호.**
 *
 * ★ **왜 필요한가.** 모델을 바꾸는 길이 이제 둘입니다.
 *
 *   ```text
 *   ① 가격 예측 화면 → 「모델 재학습」 탭 → 「모델 업데이트」
 *   ② 어느 화면에서나 → 아래 채팅 서랍 → 「모델 성능 어때?」 → 「모델 업데이트」
 *   ```
 *
 *   ②로 바꾸면 **탭의 빨간 배지가 안 사라졌습니다** (2026-09-16 사용자 실측).
 *   배지를 세는 곳(`console/forecast/page.tsx`)이 `retrainPending()` 을
 *   **뜰 때 한 번만** 불렀기 때문입니다. 채팅은 그 화면 바깥에 있어서
 *   (`console/layout.tsx` 의 `MasterDock`) 알려 줄 길이 없었습니다.
 *
 *   ★ ①도 같은 구멍이 있었습니다. 재학습 탭은 자기 목록만 다시 받고
 *     **페이지의 배지는 안 건드렸습니다.** 그래서 두 길을 다 이 신호로
 *     묶습니다 — 한쪽만 고치면 다음에 또 같은 것을 찾게 됩니다.
 *
 * ★ **한 문서 안에서만 돕니다.** 브라우저 창을 따로 띄워 놓은 쪽까지는 안
 *   갑니다. 그건 그대로 둡니다 — 그 화면은 다시 뜰 때(`useEffect` 첫 실행)
 *   새로 물어보므로 **돌아오면 최신**입니다.
 *
 * 🔴 **신호는 «다시 물어봐라» 일 뿐입니다.** 여기에 «몇 건 남았다» 를 실어
 *   보내지 않습니다. 그 수를 아는 곳은 ML 콘솔 하나뿐이고
 *   (`/retrain/pending` 이 파일과 재학습 그래프를 겹쳐서 셉니다), 화면끼리
 *   주고받은 수를 믿기 시작하면 두 곳이 조용히 어긋납니다.
 */

import { useEffect, useRef } from "react";

/** 신호 이름. **한 곳에서만 적습니다** — 양쪽이 글자로 맞추면 오타가 안 잡힙니다. */
export const RETRAIN_CHANGED = "haetdeul:retrain-changed";

/** 무엇이 바뀌었나. 지금 듣는 쪽은 전부 다시 묻지만, 적어 두면 나중에 가릅니다. */
export interface RetrainChangedDetail {
  /** `auc` · `whsl` · `rtl` */
  kind: string;
}

/** 「방금 이 가격 종류의 모델을 바꿨다」 고 알립니다. **바뀐 뒤에만** 부릅니다. */
export function announceRetrainChanged(kind: string): void {
  //  서버에서 그려질 때는 `window` 가 없습니다. 조용히 넘어갑니다
  if (typeof window === "undefined") return;
  window.dispatchEvent(
    new CustomEvent<RetrainChangedDetail>(RETRAIN_CHANGED, { detail: { kind } }),
  );
}

/**
 * 신호가 오면 `onChanged` 를 부릅니다.
 *
 * ★ **다시 붙지 않습니다.** 부르는 쪽이 매번 새 함수를 넘겨도(보통 그렇습니다)
 *   듣는 자리는 그대로 두고 **가장 최근 함수**만 갈아 끼웁니다. 안 그러면
 *   화면이 다시 그려질 때마다 떼었다 붙었다 하면서 그 틈에 온 신호를 놓칩니다.
 */
export function useRetrainChanged(onChanged: (kind: string) => void): void {
  const latest = useRef(onChanged);
  useEffect(() => {
    latest.current = onChanged;
  }, [onChanged]);

  useEffect(() => {
    const listen = (event: Event) => {
      const detail = (event as CustomEvent<RetrainChangedDetail>).detail;
      latest.current(detail?.kind ?? "");
    };
    window.addEventListener(RETRAIN_CHANGED, listen);
    return () => window.removeEventListener(RETRAIN_CHANGED, listen);
  }, []);
}
