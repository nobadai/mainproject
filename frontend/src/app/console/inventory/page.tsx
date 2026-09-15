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
  //  ★ 기본은 「한눈에 보기」다 (#675). 값의 주인은 백엔드 `PANES` 이고 여기서는
  //    첫 요청에 실을 값만 고른다 — 탭 목록도 `data.panes` 가 그대로 준다.
  const [pane, setPane] = useState("summary");
  const asOf = useSyncExternalStore(subscribeAsOf, asOfSnapshot, serverAsOf);
  //  🔴 **작은 탭을 바꿔도 다시 받지 않는다** (2026-09-15). 응답에는 네 pane 이 **다** 실려
  //     있고 `pane` 은 서버가 `selected` 한 칸에 되돌려 줄 뿐이라, 키를 `asOf` 로만 두면
  //     탭 클릭은 요청 0회다 — 종전엔 클릭마다 한 판(1.1 s)을 통째로 다시 받았다.
  //     기준일이 바뀌면 키가 바뀌어 그때만 다시 받는다.
  const { data, error } = useTab<LogisticsTab>(asOf, () => logistics(asOf, pane));

  if (error) return <ErrorBox message={error} />;
  if (!data) return <Loading what="재고 · 물류" />;

  //  ★ 고른 탭은 화면 상태다. `data.selected` 는 첫 요청 때 값이라 그 뒤로는 안 본다.
  const current = data.panes.find((p) => p.key === pane) ?? data.panes[0];

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
