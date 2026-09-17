"use client";

/**
 * 기간 고르기 — 시작일 · 끝일 두 칸.
 *
 * ★ **달력을 건드릴 때마다 부모가 API 를 다시 부릅니다.** 「조회」 버튼을 따로
 *   두지 않습니다. 버튼이 있으면 날짜만 바꿔 놓고 안 누른 채 화면을 보게 되고,
 *   그러면 **고른 날짜와 보이는 자료가 서로 다른 날**이 됩니다.
 *
 * ★ **기본값은 화면 오른쪽 위 기준일에서 따옵니다** — 기준일 −4일 ~ 기준일
 *   (양끝 포함 5일치). 기준일을 바꾸면 기본값도 따라갑니다. 단 **사람이 손으로
 *   고친 칸은 그대로 둡니다** — 직접 고른 값을 화면이 말없이 되돌리면
 *   «내가 뭘 눌렀더라» 가 됩니다.
 *
 * ★ **시작일이 끝일보다 뒤면 아무것도 안 부릅니다.** 빈 표를 돌려받으면
 *   «그 기간에 기록이 없다» 로 읽힙니다 — 실제로는 날짜를 거꾸로 고른 것입니다.
 *   그래서 요청을 막고 한 줄로 말합니다.
 *
 * ★ 기준일 읽는 방식은 `console/layout.tsx` 의 기준일 선택기와 **똑같이**
 *   `useSyncExternalStore` 입니다 (`lib/demo_as_of.ts`). 새 관습을 만들지
 *   않습니다 — 시연이 끝나고 그 갈래를 지울 때 같이 지웁니다 (`#431`).
 */

import { useEffect, useState, useSyncExternalStore } from "react";

import { asOfSnapshot, serverAsOf, subscribeAsOf } from "@/lib/demo_as_of";

const DAY = /^\d{4}-\d{2}-\d{2}$/;

/** 기본 기간의 길이 (양끝 포함). 5일 = 기준일과 그 앞 나흘. */
export const RANGE_DAYS = 5;

/**
 * `YYYY-MM-DD` 에서 며칠 옮긴 날.
 *
 * ★ **UTC 로 셉니다.** 그냥 `new Date("2026-09-16")` 를 쓰면 브라우저 시간대에
 *   따라 하루가 밀립니다 — 우리 쪽이 이미 한 번 데인 함정입니다 (CLAUDE.md 9절).
 *   날짜만 다루는 값이므로 시각을 섞지 않습니다.
 */
export function shiftDay(ymd: string, days: number): string {
  if (!DAY.test(ymd)) return ymd;
  const t = new Date(`${ymd}T00:00:00Z`);
  if (Number.isNaN(t.getTime())) return ymd;
  t.setUTCDate(t.getUTCDate() + days);
  return t.toISOString().slice(0, 10);
}

export function DateRange({
  onChange,
  hint,
}: {
  /** 고른 기간. **바뀔 때마다** 불립니다 — 부모는 여기서 다시 받아 옵니다.
   *  부모가 매번 새로 만드는 함수를 주면 계속 다시 부르게 되므로
   *  `useCallback` 으로 고정해서 넘기세요. */
  onChange: (from: string, to: string) => void;
  hint?: string;
}) {
  const asOf = useSyncExternalStore(subscribeAsOf, asOfSnapshot, serverAsOf);

  //  `null` 은 «사람이 아직 안 건드렸다» 는 뜻입니다. 그동안은 기준일을 따라갑니다.
  const [from, setFrom] = useState<string | null>(null);
  const [to, setTo] = useState<string | null>(null);

  const effFrom = from ?? shiftDay(asOf, -(RANGE_DAYS - 1));
  const effTo = to ?? asOf;
  //  `YYYY-MM-DD` 는 글자 순서가 곧 날짜 순서라 그대로 견줍니다.
  const reversed = effFrom > effTo;

  useEffect(() => {
    if (!reversed) onChange(effFrom, effTo);
  }, [reversed, effFrom, effTo, onChange]);

  const box = {
    borderColor: "var(--color-hair)",
    background: "var(--color-panel)",
  } as const;

  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap items-center gap-1.5 text-[15.5px]" style={{ color: "var(--color-mut2)" }}>
        <input
          type="date"
          aria-label="시작일"
          value={effFrom}
          //  ★ 빈 값(달력을 지운 상태)은 무시합니다. `layout.tsx` 의 기준일
          //    선택기와 같은 규칙입니다 — 빈 날짜로는 물어볼 것이 없습니다.
          onChange={(e) => e.target.value && setFrom(e.target.value)}
          className="rounded-md border px-2 py-1 font-mono text-[15.5px]"
          style={box}
        />
        <span aria-hidden>~</span>
        <input
          type="date"
          aria-label="끝일"
          value={effTo}
          onChange={(e) => e.target.value && setTo(e.target.value)}
          className="rounded-md border px-2 py-1 font-mono text-[15.5px]"
          style={box}
        />
        {hint && <span className="text-[15px]">{hint}</span>}
      </div>
      {reversed && (
        <p className="m-0 text-[15.5px]" style={{ color: "var(--color-t-warn)" }}>
          시작일이 끝일보다 뒤입니다 — 날짜를 다시 고르세요. (아직 아무것도 받아오지 않았습니다)
        </p>
      )}
    </div>
  );
}
