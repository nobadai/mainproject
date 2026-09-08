"use client";

/**
 * 재고 · 물류 탭. 소유: **물류 파트.**
 *
 * ★ 안에서 넷으로 또 나뉩니다 (재고·예약 / 입고 / 창고 / 출고).
 *   카드가 목록으로 오므로, 물류가 카드를 하나 더해도 **이 파일은 안 고칩니다.**
 */

import { useState, useSyncExternalStore } from "react";

import {
  CardBlock,
  ErrorBox,
  Loading,
  Note,
  SourceTag,
  StatRow,
  TabButtons,
} from "@/components/console/Blocks";
import { useTab } from "@/components/console/useTab";
//  🔴 시연용 기준일 (`#431`). 시연이 끝나면 이 줄과 아래 `asOf` 를 지우고
//     `useTab` 의 `AS_OF` 로 되돌린다.
import { asOfSnapshot, serverAsOf, subscribeAsOf } from "@/lib/demo_as_of";
import { logistics, type LogisticsTab } from "@/lib/screen";

export default function InventoryPage() {
  const [pane, setPane] = useState("stock");
  const asOf = useSyncExternalStore(subscribeAsOf, asOfSnapshot, serverAsOf);
  const { data, error } = useTab<LogisticsTab>(`${asOf}|${pane}`, () => logistics(asOf, pane));

  if (error) return <ErrorBox message={error} />;
  if (!data) return <Loading what="재고 · 물류" />;

  const current = data.panes.find((p) => p.key === data.selected) ?? data.panes[0];

  return (
    <>
      <TabButtons
        items={data.panes.map((p) => ({ key: p.key, label: p.label }))}
        value={current.key}
        onChange={setPane}
      />
      <SourceTag sources={[data.source]} />
      <Note note={data.principle} />
      <StatRow items={current.stats} />
      {current.cards.map((c) => (
        <CardBlock key={c.key} card={c} />
      ))}
    </>
  );
}
