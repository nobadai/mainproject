import type { DomainActionAnswer } from "@/lib/types";
import { date, kg, record, rows, text, won } from "@/components/reports/reportFormat";

type Facts = Record<string, unknown>;

function Value({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-[15px] text-faint">{label}</dt>
      <dd className="m-0 break-words text-[17px] text-ink">{value}</dd>
    </div>
  );
}

function Card({ children }: { children: React.ReactNode }) {
  return <div className="rounded-lg border border-line-soft bg-sunk p-3">{children}</div>;
}

function PartnerList({ facts }: { facts: Facts }) {
  const items = rows(facts.rows);
  if (items.length === 0) return <p className="m-0 text-[18px] text-muted">등록된 거래처가 없습니다.</p>;
  return (
    <section className="mt-3">
      <p className="m-0 mb-2 text-[16px] font-semibold text-muted">거래처 목록</p>
      <div className="grid gap-2 sm:grid-cols-2">
        {items.map((item, index) => (
          <Card key={text(item.partner_id, String(index))}>
            <p className="m-0 font-semibold text-ink">{text(item.partner_name, "이름 기록 없음")}</p>
            <dl className="m-0 mt-2 grid grid-cols-2 gap-2">
              <Value label="유형" value={text(item.partner_type, "기록 없음")} />
              <Value label="상태" value={text(item.status, "기록 없음")} />
            </dl>
          </Card>
        ))}
      </div>
      <p className="m-0 mt-2 text-[16px] text-faint">거래처 이름을 입력하면 상세 정보를 확인할 수 있습니다.</p>
    </section>
  );
}

function PartnerDetail({ facts }: { facts: Facts }) {
  const basic = record(facts.basic);
  return (
    <section className="mt-3">
      <p className="m-0 mb-2 text-[16px] font-semibold text-muted">거래처 정보</p>
      <Card>
        <dl className="m-0 grid gap-3 sm:grid-cols-2">
          <Value label="이름" value={text(basic.partner_name, "기록 없음")} />
          <Value label="유형" value={text(basic.partner_type, "기록 없음")} />
          <Value label="결제 조건" value={basic.sales_collection_days == null ? "기록 없음" : `${text(basic.sales_collection_days)}일`} />
          <Value label="상태" value={text(basic.status, "기록 없음")} />
        </dl>
      </Card>
    </section>
  );
}

function FinanceRows({ facts, kind }: { facts: Facts; kind: "receivable" | "payable" | "expense" }) {
  const items = rows(facts.rows);
  const empty = kind === "receivable" ? "현재 미수금이 없습니다." : kind === "payable" ? "현재 지급 예정 건이 없습니다." : "등록된 비용이 없습니다.";
  if (items.length === 0) return <p className="m-0 mt-3 text-[18px] text-muted">{empty}</p>;
  return (
    <section className="mt-3 overflow-x-auto">
      <p className="m-0 mb-2 text-[16px] font-semibold text-muted">{kind === "receivable" ? "미수금 목록" : kind === "payable" ? "지급 예정 목록" : "비용 내역"}</p>
      <table className="w-full min-w-[520px] text-left text-[16px]">
        <thead className="text-faint"><tr>{kind === "receivable" ? <><th>거래처</th><th>지급 예정일</th><th>미수 금액</th><th>상태</th></> : kind === "payable" ? <><th>지급 예정일</th><th>미지급 금액</th><th>상태</th></> : <><th>분류</th><th>금액</th><th>발생일</th><th>상태</th></>}</tr></thead>
        <tbody>{items.map((item, index) => <tr key={text(item.receivable_id ?? item.payable_id ?? item.expense_id, String(index))} className="border-t border-line-soft text-ink">{kind === "receivable" ? <><td>{text(item.partner_name, "기록 없음")}</td><td>{date(item.due_date)}</td><td>{won(item.outstanding_amount_krw)}</td><td>{text(item.status)}</td></> : kind === "payable" ? <><td>{date(item.due_date)}</td><td>{won(item.outstanding_amount_krw)}</td><td>{text(item.status)}</td></> : <><td>{text(item.display_category, "기록 없음")}</td><td>{won(item.amount_krw)}</td><td>{date(item.expense_date)}</td><td>{text(item.status)}</td></>}</tr>)}</tbody>
      </table>
    </section>
  );
}

function SalesRows({ facts, confirmed }: { facts: Facts; confirmed: boolean }) {
  const items = rows(facts.rows);
  if (items.length === 0) return <p className="m-0 mt-3 text-[18px] text-muted">{confirmed ? "해당 기간에 확정된 판매가 없습니다." : "오늘 제안된 판매안이 없습니다."}</p>;
  return <section className="mt-3 overflow-x-auto"><p className="m-0 mb-2 text-[16px] font-semibold text-muted">{confirmed ? "확정 판매" : "오늘 판매안"}</p><table className="w-full min-w-[580px] text-left text-[16px]"><thead className="text-faint"><tr><th>품목</th><th>거래처</th><th>수량</th><th>매출</th><th>상태</th></tr></thead><tbody>{items.map((item, index) => <tr key={`${text(item.request_id, "row")}:${text(item.scenario_id ?? item.sale_id, "row")}:${index}`} className="border-t border-line-soft text-ink"><td>{text(item.item, "기록 없음")}</td><td>{text(item.partner_id, "기록 없음")}</td><td>{kg(item.quantity_kg)}</td><td>{won(item.reported_sales_amount_krw ?? item.sales_amount_krw)}</td><td>{text(confirmed ? item.sale_status : item.presentation_state, "기록 없음")}</td></tr>)}</tbody></table></section>;
}

/** Authoritative Domain read payload only; no DB call, inference, or aggregate is added here. */
export function DomainReadResult({ result }: { result: DomainActionAnswer }) {
  const facts = record(result.data);
  switch (result.action) {
    case "PARTNER_LIST": return <PartnerList facts={facts} />;
    case "PARTNER_DETAIL_GET": return <PartnerDetail facts={facts} />;
    case "FINANCE_RECEIVABLES_GET": return <FinanceRows facts={facts} kind="receivable" />;
    case "FINANCE_PAYABLES_GET": return <FinanceRows facts={facts} kind="payable" />;
    case "FINANCE_EXPENSE_LIST": return <FinanceRows facts={facts} kind="expense" />;
    case "SALES_PROPOSALS_TODAY": return <SalesRows facts={facts} confirmed={false} />;
    case "SALES_CONFIRMED_TODAY": return <SalesRows facts={facts} confirmed />;
    default: return null;
  }
}
