"use client";

/**
 * 재고 · 물류 탭. 소유: **물류 파트.**
 *
 * ★ 안에서 넷으로 또 나뉩니다 (재고·예약 / 입고 / 창고 / 출고).
 *   카드가 목록으로 오므로, 물류가 카드를 하나 더해도 **이 파일은 안 고칩니다.**
 */

import { useState } from "react";

import {
  CardBlock,
  ErrorBox,
  Loading,
  Note,
  SourceTag,
  StatRow,
  TabButtons,
} from "@/components/console/Blocks";
import { AS_OF, useTab } from "@/components/console/useTab";
import { logistics, type LogisticsTab } from "@/lib/screen";

export default function InventoryPage() {
  const [pane, setPane] = useState("stock");
  const { data, error } = useTab<LogisticsTab>(pane, () => logistics(AS_OF, pane));

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
