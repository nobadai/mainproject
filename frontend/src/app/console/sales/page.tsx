"use client";

/**
 * 판매 탭. 소유: **판매 파트.** 조회 전용입니다.
 *
 * ★ 재고·물류와 마찬가지로 카드가 목록으로 옵니다 — 판매가 카드를 더해도
 *   이 파일은 안 고칩니다.
 */

import { useSyncExternalStore } from "react";

import {
  CardBlock,
  ErrorBox,
  Loading,
  SourceTag,
  StatRow,
} from "@/components/console/Blocks";
import { useTab } from "@/components/console/useTab";
//  🔴 시연용 기준일 (`#431`). 시연이 끝나면 이 줄과 아래 `asOf` 를 지우고
//     `useTab` 의 `AS_OF` 로 되돌린다.
import { asOfSnapshot, serverAsOf, subscribeAsOf } from "@/lib/demo_as_of";
import { sales, type Card, type SalesTab, type Tone } from "@/lib/screen";

export default function SalesPage() {
  const asOf = useSyncExternalStore(subscribeAsOf, asOfSnapshot, serverAsOf);
  const { data, error } = useTab<SalesTab>(asOf, () => sales(asOf));

  if (error) return <ErrorBox message={error} />;
  if (!data) return <Loading what="판매" />;

  const [actions, ...details] = data.cards;

  return (
    <div className="mx-auto flex w-full max-w-[1400px] flex-col gap-4 sm:gap-5">
      <SourceTag sources={[data.source]} />
      <p className="m-0 text-[12px] leading-relaxed text-ink2 sm:text-[12.5px]">
        {data.read_only.text}
      </p>
      <StatRow items={data.stats} />
      {actions && <SalesActionSummary card={actions} />}
      {details.map((card) => (
        <CardBlock key={card.key} card={card} />
      ))}
    </div>
  );
}

function SalesActionSummary({ card }: { card: Card }) {
  return (
    <section className="overflow-hidden rounded-lg border border-hair bg-panel">
      <header className="border-b border-hair px-4 py-3.5 sm:px-5">
        <h2 className="m-0 text-[15px] font-semibold">{card.title}</h2>
        {card.subtitle && <p className="mb-0 mt-1 text-[11.5px] text-ink2">{card.subtitle}</p>}
      </header>
      <dl className="m-0 grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4">
        {card.stats.map((item) => (
          <div
            key={item.label}
            className="flex min-w-0 items-center justify-between gap-4 border-b border-hair px-4 py-3 last:border-b-0 sm:block sm:border-b-0 sm:border-r sm:last:border-r-0"
          >
            <dt className="text-[11.5px] text-ink2">{item.label}</dt>
            <dd
              className="m-0 text-right text-[17px] font-semibold tabular-nums sm:mt-1 sm:text-left"
              style={{ color: toneColor(item.tone) }}
            >
              {item.value}{item.unit ? ` ${item.unit}` : ""}
            </dd>
            {item.detail && (
              <p className="mb-0 mt-1 hidden text-[10.5px] leading-snug text-ink2 sm:block">
                {item.detail}
              </p>
            )}
          </div>
        ))}
      </dl>
    </section>
  );
}

function toneColor(tone: Tone): string {
  if (tone === "good") return "var(--color-t-good)";
  if (tone === "warn") return "var(--color-t-warn)";
  if (tone === "bad") return "var(--color-t-bad)";
  return "var(--color-ink)";
}
