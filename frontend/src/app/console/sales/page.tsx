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
import { sales, type SalesTab } from "@/lib/screen";

export default function SalesPage() {
  const asOf = useSyncExternalStore(subscribeAsOf, asOfSnapshot, serverAsOf);
  const { data, error } = useTab<SalesTab>(asOf, () => sales(asOf));

  if (error) return <ErrorBox message={error} />;
  if (!data) return <Loading what="판매" />;

  return (
    <>
      <SourceTag sources={[data.source]} />
      <p className="m-0 text-[12px] leading-relaxed text-ink2">
        {data.read_only.text}
      </p>
      <StatRow items={data.stats} />
      {data.cards.map((c) => (
        <CardBlock key={c.key} card={c} />
      ))}
    </>
  );
}
