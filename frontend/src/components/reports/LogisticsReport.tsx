"use client";

import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { Metric, ReportChrome, Section, Table } from "./ReportChrome";
import { kg, record, rows, text, type ReportFacts } from "./reportFormat";
import {
  ARRIVAL_DISPLAY_STATE_LABEL,
  AVAILABLE_UNRESOLVED_TEXT,
  IN_TRANSIT_STATUS_TEXT,
  RESERVATION_STATUS_LABEL,
  TURNOVER_STATUS_LABEL,
  days,
  label,
  lotAction,
  lotDisplayName,
  monthDay,
  verdictText,
} from "./logisticsReportLabels";

/**
 * 재고·물류 운영 보고서 — **창고 운영자가 그대로 읽는 문서.**
 *
 * ★ **화면이 업무 숫자를 만들지 않는다.** 합계·창고 사용량·신선도·회전·예약 상태는 전부
 *   `backend/app/master/report.py::render_logistics_chat_report` 가 기존 재고·물류
 *   read model 에서 낸 값이다. 여기서 다시 세거나 다시 판정하면 화면과 문서가 **다른
 *   숫자**를 말하게 된다 (Finance/Sales 보고서와 같은 규율).
 *
 * 🔴 **내부 ID·영문 상태 코드를 본문에 싣지 않는다.** `LOT-RCPT-…` · `RSV-SI-SALE-…` ·
 *    `PUTAWAY_DONE` · `FEFO_AUTO_SELECTED` · `COLD_DRY_0_1` 은 사람이 읽을 말이 아니다.
 *    추적용으로 facts 에는 그대로 남아 있고 이 컴포넌트가 안 그릴 뿐이다.
 *
 * 🔴 **`null` 을 0 으로 바꾸지 않는다.** `kg()`·`days()` 는 못 읽은 칸을 「—」로 둔다.
 *
 * ★ **기준일 Snapshot 과 기간 발생 내역을 섞지 않는다.** 재고·Lot·예약은 `as_of` 상태이고
 *   입고 실적과 추이는 `start_date~end_date` 에 일어난 일이다.
 */

/** 본문에 펼치는 최대 행수. 넘치면 «외 N건» 으로 알린다 — 전체 원장을 붓지 않는다. */
const MAX_ROWS = 10;

/** 표 아래 «외 N건». 🔴 **건수를 추정하지 않는다** — facts 의 total 을 쓴다. */
function More({ total, shown }: { total: number; shown: number }) {
  if (!Number.isFinite(total) || total <= shown) return null;
  return <p className="m-0 mt-1 text-[10px] text-[#70857b]">외 {(total - shown).toLocaleString("ko-KR")}건</p>;
}

function Empty({ children }: { children: string }) {
  return <p className="m-0 rounded-lg border border-[#dbe7e0] bg-[#f4f8f6] p-3 text-[11px] text-[#70857b]">{children}</p>;
}

export function LogisticsReport({ facts }: { facts: ReportFacts }) {
  const summary = record(facts.summary);
  const inventory = record(facts.inventory);
  const inbound = record(facts.inbound);
  const outbound = record(facts.outbound);
  const arrival = record(inbound.arrival_summary);

  const items = rows(inventory.items);
  const lots = rows(inventory.lots);
  const trend = rows(facts.trend);
  const receiptRollup = rows(inbound.period_receipt_rollup);
  const reservations = rows(outbound.working_reservations);
  const inTransit = rows(inbound.in_transit);
  // ★ `null`(못 확인)과 `[]`(0건 확인)은 다른 사실이라 문장을 다르게 쓴다.
  const inTransitUnresolved = inbound.in_transit === null || inbound.in_transit === undefined;

  const availableKnown =
    summary.total_available_qty_kg !== null && summary.total_available_qty_kg !== undefined;
  // 🔴 하루짜리 보고서는 점 하나뿐이라 선을 그리지 않는다 — Snapshot 숫자로 답한다.
  const showTrend = summary.trend_is_single_day !== true && trend.length > 1;

  // ★ 운영 확인사항 — **facts 에 이미 있는 값의 단순 사실 비교다.** 새 등급도 KPI 도 아니고
  //   LLM 도 쓰지 않는다. 숫자는 전부 Backend 가 낸 것을 그대로 읽는다.
  const notes: string[] = [];
  if (Number(summary.total_unallocated_reserved_qty_kg) > 0) {
    notes.push(`아직 Lot 을 고르지 않은 예약이 ${kg(summary.total_unallocated_reserved_qty_kg)} 남아 있습니다.`);
  }
  if (summary.unallocated_exceeds_on_hand === true) {
    notes.push("미할당 예약량이 현재고보다 많습니다.");
  }
  if (availableKnown) {
    notes.push(`현재 판매가능 재고는 ${kg(summary.total_available_qty_kg)} 입니다.`);
  }
  if (Number(summary.disposal_candidate_lot_count) > 0) {
    notes.push(`폐기 검토 대상 Lot 이 ${text(summary.disposal_candidate_lot_count)}건 있습니다.`);
  }
  if (Number(summary.sell_priority_lot_count) > 0) {
    notes.push(`우선 출고 대상 Lot 이 ${text(summary.sell_priority_lot_count)}건 있습니다.`);
  }

  return <div className="space-y-5">
    <ReportChrome title="재고·물류 운영 보고서" subtitle="재고 상태와 창고 운영 현황" facts={facts} page={1}>
      <div className="grid grid-cols-4 gap-3">
        <Metric label="현재고" value={kg(summary.total_on_hand_qty_kg)} detail={`기준일 창고 보유량 · 품목 ${text(summary.item_count, "0")}종`} />
        <Metric
          label="판매가능량"
          value={kg(summary.total_available_qty_kg)}
          detail={availableKnown ? "예약을 뺀 팔 수 있는 양" : `확인 못 함 — ${label(AVAILABLE_UNRESOLVED_TEXT, summary.available_qty_unresolved_reason, "사유 없음")}`}
        />
        <Metric label="활성 예약 수량" value={kg(summary.total_reserved_qty_kg)} detail={`현재 활성 예약이 요구하는 양 · 미할당 ${kg(summary.total_unallocated_reserved_qty_kg)}`} />
        {/* 🔴 사용률 %를 화면이 새로 정의하지 않는다 — 원천 값을 그대로 보여준다. */}
        <Metric label="창고 사용량" value={kg(summary.used_capacity_kg)} detail={`보장 ${kg(summary.guaranteed_capacity_kg)} · 최대 ${kg(summary.burst_capacity_kg)}`} />
      </div>

      {notes.length > 0 && <Section title="운영 확인사항">
        <ul className="m-0 list-disc rounded-lg border border-[#dbe7e0] bg-[#f4f8f6] py-3 pl-8 pr-3 text-[11px] text-[#2c463b]">
          {notes.map((note) => <li key={note} className="mb-1 last:mb-0">{note}</li>)}
        </ul>
      </Section>}

      <Section title={showTrend ? "기간 재고 추이" : "기준일 재고"}>
        {showTrend
          ? <div className="h-56 rounded-lg border border-[#dbe7e0] p-3">
              {/* 🔴 빈 날은 0kg 이 아니라 «안 연 날» 이다 — 선을 잇지 않는다. */}
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={trend}>
                  <XAxis dataKey="date" tick={{ fontSize: 10 }} />
                  <YAxis tick={{ fontSize: 10 }} />
                  <Tooltip />
                  <Line type="monotone" dataKey="on_hand_qty_kg" stroke="#1d6b48" dot={false} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          : <Empty>{`기준일 재고 ${kg(summary.total_on_hand_qty_kg)} — 하루치 보고서라 추이 그래프를 그리지 않습니다.`}</Empty>}
      </Section>

      <Section title="품목별 재고">
        <Table headers={["품목", "현재고", "판매가능", "예약", "할당", "미할당", "활성 예약", "우선 출고 Lot", "폐기 검토 Lot"]}>
          {items.map((row) => <tr key={text(row.item_id)} className="border-t border-[#e6eee9]">
            <td className="px-2 py-2">{text(row.item_name, text(row.item_id))}</td>
            <td>{kg(row.on_hand_qty_kg)}</td>
            <td>{kg(row.available_qty_kg)}</td>
            <td>{kg(row.reserved_qty_kg)}</td>
            <td>{kg(row.allocated_qty_kg)}</td>
            <td>{kg(row.unallocated_reserved_qty_kg)}</td>
            <td>{text(row.active_reservation_count, "0")}건</td>
            <td>{text(row.sell_priority_lot_count, "0")}건</td>
            <td>{text(row.disposal_candidate_lot_count, "0")}건</td>
          </tr>)}
        </Table>
      </Section>
    </ReportChrome>

    <ReportChrome title="재고·물류 운영 보고서" subtitle="입고 · 검수" facts={facts} page={2}>
      <p className="m-0 mb-3 text-[11px] text-[#70857b]">창고 도착 전 입고 예정과 검수 진행 상황을 보여줍니다.</p>
      <div className="grid grid-cols-4 gap-3">
        <Metric label="도착 예정" value={`${text(arrival.due_count, "0")}건`} detail={Number(arrival.due_count) > 0 ? "오늘 받을 수 있는 입고" : "예정된 입고 없음"} />
        <Metric label="도착 지연" value={`${text(arrival.overdue_count, "0")}건`} detail={Number(arrival.overdue_count) > 0 ? "예정일이 지난 입고" : "지연된 입고 없음"} />
        {/* 내부 BLOCKED 를 그대로 쓰지 않는다 — 사람이 읽을 말로만 적는다. */}
        <Metric label="처리 보류" value={`${text(arrival.blocked_count, "0")}건`} detail="입고 처리에 필요한 정보 확인 필요" />
        <Metric label="판정 불가" value={`${text(arrival.unresolved_count, "0")}건`} detail={`입고 상태 확인에 필요한 정보 부족 · 대기 ${text(arrival.not_due_count, "0")}건`} />
      </div>

      {/* ⚠️ 내부 이름은 `in_transit` 이지만 차량 운송이 아니라 **도착 전 물량**이다. */}
      <Section title="입고 예정">
        {inTransitUnresolved || inTransit.length === 0
          ? <Empty>{label(IN_TRANSIT_STATUS_TEXT, inbound.in_transit_status, "예정된 입고 없음")}</Empty>
          : <>
              <Table headers={["품목", "수량", "예정 도착일", "도착 상태"]}>
                {inTransit.slice(0, MAX_ROWS).map((row, index) => <tr key={`${text(row.item)}-${index}`} className="border-t border-[#e6eee9]">
                  <td className="px-2 py-2">{text(row.item)}</td>
                  <td>{kg(row.quantity_kg)}</td>
                  <td>{monthDay(row.expected_arrival_date)}</td>
                  {/* 🔴 날짜를 보고 화면이 정하지 않는다 — Backend 가 준 표시 상태를 옮긴다. */}
                  <td>{label(ARRIVAL_DISPLAY_STATE_LABEL, row.arrival_display_state)}</td>
                </tr>)}
              </Table>
              <More total={inTransit.length} shown={Math.min(MAX_ROWS, inTransit.length)} />
            </>}
      </Section>

      <Section title="기간 입고 · 검수 실적">
        {receiptRollup.length === 0
          ? <Empty>해당 기간 입고 없음</Empty>
          : <>
              {/* ★ 원장 한 줄씩이 아니라 「입고일 + 품목」 실적이다 — 내부 Receipt ID 를 싣지 않는다. */}
              <Table headers={["입고일", "품목", "입고 건수", "주문량", "합격", "보류", "거절", "검수 결과", "재고 반영"]}>
                {receiptRollup.slice(0, MAX_ROWS).map((row) => <tr key={`${text(row.arrived_at)}-${text(row.item)}`} className="border-t border-[#e6eee9]">
                  <td className="px-2 py-2">{monthDay(row.arrived_at)}</td>
                  <td>{text(row.item)}</td>
                  <td>{text(row.receipt_count, "0")}건</td>
                  <td>{kg(row.ordered_qty_kg)}</td>
                  <td>{kg(row.accepted_qty_kg)}</td>
                  <td>{kg(row.hold_qty_kg)}</td>
                  <td>{kg(row.rejected_qty_kg)}</td>
                  <td>{verdictText(row.inspection_verdicts, row.inspection_unknown_count)}</td>
                  {/* 검수 → 재고 반영까지가 사용자가 보는 마지막 단계다. 세는 것은 Backend 다. */}
                  <td>{text(row.stock_applied_count, "0")} / {text(row.receipt_count, "0")}건</td>
                </tr>)}
              </Table>
              <More total={receiptRollup.length} shown={Math.min(MAX_ROWS, receiptRollup.length)} />
              <p className="m-0 mt-1 text-[10px] text-[#70857b]">재고가 되는 것은 주문 수량이 아니라 합격 수량입니다.</p>
            </>}
      </Section>
    </ReportChrome>

    <ReportChrome title="재고·물류 운영 보고서" subtitle="예약 · 출고" facts={facts} page={3}>
      <div className="grid grid-cols-3 gap-3">
        <Metric label="처리 중 예약" value={`${text(summary.working_reservation_count, "0")}건`} detail="기준일에 아직 일이 남은 예약" />
        <Metric label="미할당 예약 수량" value={kg(summary.total_unallocated_reserved_qty_kg)} detail="아직 Lot 을 고르지 않은 양" />
        <Metric label="처리 완료" value={`${text(summary.settled_reservation_count, "0")}건`} detail="전량 출고·해제되어 본문에서 뺀 예약" />
      </div>

      <Section title="처리 중 예약">
        {reservations.length === 0
          ? <Empty>기준일에 아직 처리할 예약이 없습니다.</Empty>
          : <>
              {/* 🔴 Reservation ID · Sale UUID · Allocation ID 는 본문에 싣지 않는다 (facts 에는 남아 있다). */}
              <Table headers={["품목", "필요 수량", "예약 수량", "할당 수량", "미할당 수량", "할당 Lot", "납기일", "처리 상태"]}>
                {reservations.slice(0, MAX_ROWS).map((row, index) => <tr key={`${text(row.reservation_id, String(index))}`} className="border-t border-[#e6eee9]">
                  <td className="px-2 py-2">{text(row.item_name, text(row.item_id))}</td>
                  <td>{kg(row.required_qty_kg)}</td>
                  <td>{kg(row.reserved_qty_kg)}</td>
                  <td>{kg(row.allocated_qty_kg)}</td>
                  <td>{kg(row.unallocated_qty_kg)}</td>
                  <td>{text(row.allocation_lot_count, "0")}개</td>
                  <td>{monthDay(row.due_date)}</td>
                  <td>{label(RESERVATION_STATUS_LABEL, row.status)}</td>
                </tr>)}
              </Table>
              <More total={reservations.length} shown={Math.min(MAX_ROWS, reservations.length)} />
              <p className="m-0 mt-1 text-[10px] text-[#70857b]">예약과 할당은 재고를 바로 줄이지 않습니다 — 실제 출고 때 줄어듭니다.</p>
            </>}
      </Section>
    </ReportChrome>

    <ReportChrome title="재고·물류 운영 보고서" subtitle="Lot · 신선도" facts={facts} page={4}>
      <div className="grid grid-cols-3 gap-3">
        <Metric label="현재 보유 Lot" value={`${text(summary.lot_count, "0")}개`} detail="기준일에 잔량이 남은 Lot" />
        <Metric label="우선 출고 대상" value={`${text(summary.sell_priority_lot_count, "0")}개`} detail="먼저 내보내야 할 Lot" />
        <Metric label="폐기 검토 대상" value={`${text(summary.disposal_candidate_lot_count, "0")}개`} detail="폐기 여부를 봐야 할 Lot" />
      </div>

      <Section title="Lot 별 신선도 · 회전">
        {lots.length === 0
          ? <Empty>기준일에 남아 있는 Lot 이 없습니다.</Empty>
          : <>
              {/* 🔴 raw Lot ID · 내부 Zone 코드 · ACTIVE 는 싣지 않는다. 신선도 숫자로 상태를 새로 매기지도 않는다. */}
              <Table headers={["Lot", "품목", "등급", "잔량", "입고일", "신선도 잔여", "회전 잔여", "회전 상태", "관리 조치"]}>
                {lots.slice(0, MAX_ROWS).map((row, index) => <tr key={`${text(row.lot_id, String(index))}`} className="border-t border-[#e6eee9]">
                  <td className="px-2 py-2">{lotDisplayName(row)}</td>
                  <td>{text(row.item_name, text(row.item_id))}</td>
                  <td>{text(row.grade)}</td>
                  <td>{kg(row.remaining_qty_kg)}</td>
                  <td>{monthDay(row.received_at)}</td>
                  <td>{days(row.remaining_freshness_days)}</td>
                  <td>{days(row.remaining_turnover_days)}</td>
                  <td>{label(TURNOVER_STATUS_LABEL, row.turnover_status)}</td>
                  <td>{lotAction(row.sell_priority, row.disposal_candidate)}</td>
                </tr>)}
              </Table>
              <More total={lots.length} shown={Math.min(MAX_ROWS, lots.length)} />
              <p className="m-0 mt-1 text-[10px] text-[#70857b]">급한 Lot 이 위에 옵니다 — 폐기 검토 · 우선 출고 · 신선도 잔여가 적은 순입니다.</p>
            </>}
      </Section>
    </ReportChrome>
  </div>;
}
