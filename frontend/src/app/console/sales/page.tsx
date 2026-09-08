"use client";

/**
 * 판매 탭. 소유: **판매 파트.** 조회 전용입니다.
 *
 * ★ 재고·물류와 마찬가지로 카드가 목록으로 옵니다 — 판매가 카드를 더해도
 *   이 파일은 안 고칩니다.
 */

import {
  CardBlock,
  ErrorBox,
  Loading,
  Note,
  SourceTag,
  StatRow,
} from "@/components/console/Blocks";
import { AS_OF, useTab } from "@/components/console/useTab";
import { sales, type SalesTab } from "@/lib/screen";

export default function SalesPage() {
  const { data, error } = useTab<SalesTab>(AS_OF, () => sales(AS_OF));

  if (error) return <ErrorBox message={error} />;
  if (!data) return <Loading what="판매" />;

  return (
    <>
      <SourceTag sources={[data.source]} />
      <Note note={data.read_only} />
      <StatRow items={data.stats} />
      {data.cards.map((c) => (
        <CardBlock key={c.key} card={c} />
      ))}
    </>
  );
}
