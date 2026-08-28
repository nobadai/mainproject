import { isHold, n, pct, won } from "@/lib/format";
import type { Scenario } from "@/lib/types";

import { Chip, GradeBadge } from "./ui";

const TH = "bg-surface-2 px-4 py-3 font-mono text-xs2 font-semibold tracking-wider uppercase text-ink-3";
const TD = "border-b border-rule px-4 py-3 whitespace-nowrap num";

function Section({
  title,
  ref_,
  right,
  children,
}: {
  title: string;
  ref_: string;
  right?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="border-b border-rule p-6 last:border-b-0">
      <div className="mb-4 flex items-baseline gap-3">
        <h3 className="text-md2 font-semibold">{title}</h3>
        <span className="font-mono text-xs2 text-ink-3">{ref_}</span>
        {right && <span className="ml-auto">{right}</span>}
      </div>
      {children}
    </div>
  );
}

function Fold({
  title,
  count,
  open,
  children,
}: {
  title: string;
  count: number;
  open?: boolean;
  children: React.ReactNode;
}) {
  return (
    <details open={open} className="overflow-hidden rounded-card border border-rule bg-surface">
      <summary className="flex cursor-pointer list-none items-center gap-3 px-5 py-4 text-md2 font-semibold hover:bg-surface-2 [&::-webkit-details-marker]:hidden">
        <span className="font-mono text-xs2 text-ink-3">▸</span>
        {title}
        <span className="ml-auto font-mono text-sm2 text-ink-3">{count}건</span>
      </summary>
      <div className="border-t border-rule">{children}</div>
    </details>
  );
}

export function ScenarioDetail({ s }: { s: Scenario }) {
  const unit = s.sourcing_plan[0]?.grade_unit_price ?? 0;
  const pay = s.payment_schedule;

  return (
    <div className="overflow-hidden rounded-card border border-rule bg-surface">
      <div className="grid gap-px bg-rule lg:grid-cols-2">
        <div className="bg-surface">
          <Section title="등급 배분" ref_="sourcing_plan">
            <div className="overflow-x-auto border-y border-rule">
              <table className="w-full border-collapse text-md2">
                <thead>
                  <tr>
                    <th className={`${TH} text-left`}>시장</th>
                    <th className={`${TH} text-right`}>등급</th>
                    <th className={`${TH} text-right`}>수량 kg</th>
                    <th className={`${TH} text-right`}>등급 단가</th>
                    <th className={`${TH} text-right`}>금액 원</th>
                  </tr>
                </thead>
                <tbody>
                  {s.sourcing_plan.map((r, i) => (
                    <tr key={i}>
                      <td className={`${TD} text-left font-sans`}>{r.market}</td>
                      <td className={`${TD} text-right`}>{r.grade}</td>
                      <td className={`${TD} text-right`}>{n(r.qty_kg)}</td>
                      <td className={`${TD} text-right`}>{n(r.grade_unit_price)}</td>
                      <td className={`${TD} text-right`}>{n(r.qty_kg * r.grade_unit_price)}</td>
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  <tr className="border-t border-rule-2 bg-surface-2 font-semibold">
                    <td className="px-4 py-3 text-left">합계</td>
                    <td />
                    <td className="num px-4 py-3 text-right">{n(s.total_qty_kg)}</td>
                    <td className="num px-4 py-3 text-right">{n(unit)}</td>
                    <td className="num px-4 py-3 text-right">{n(s.total_amount_krw)}</td>
                  </tr>
                </tfoot>
              </table>
            </div>
          </Section>
        </div>

        <div className="bg-surface">
          <Section
            title="회차 분할"
            ref_="split_plan"
            right={
              <Chip>{s.split_plan.length === 1 ? "일괄 1회" : `${s.split_plan.length}회 분할`}</Chip>
            }
          >
            <div className="overflow-x-auto border-y border-rule">
              <table className="w-full border-collapse text-md2">
                <thead>
                  <tr>
                    <th className={`${TH} text-left`}>회차</th>
                    <th className={`${TH} text-right`}>매입일</th>
                    <th className={`${TH} text-right`}>수량 kg</th>
                    <th className={`${TH} text-right`}>비중</th>
                  </tr>
                </thead>
                <tbody>
                  {s.split_plan.map((r) => (
                    <tr key={r.seq}>
                      <td className={`${TD} text-left font-sans`}>{r.seq}회</td>
                      <td className={`${TD} text-right`}>{r.date}</td>
                      <td className={`${TD} text-right`}>{n(r.qty_kg)}</td>
                      <td className={`${TD} text-right`}>{pct(r.qty_kg / s.total_qty_kg)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>
        </div>
      </div>

      <Section title="지급계획" ref_="payment_schedule">
        {pay ? (
          <>
            <div className="overflow-x-auto border-y border-rule">
              <table className="w-full border-collapse text-md2">
                <thead>
                  <tr>
                    <th className={`${TH} text-left`}>회차</th>
                    <th className={`${TH} text-right`}>매입일</th>
                    <th className={`${TH} text-right`}>지급일</th>
                    <th className={`${TH} text-right`}>수량 kg</th>
                    <th className={`${TH} text-right`}>BASE 원</th>
                    <th className={`${TH} text-right`}>STRESS 원</th>
                  </tr>
                </thead>
                <tbody>
                  {pay.map((r) => (
                    <tr key={r.seq}>
                      <td className={`${TD} text-left font-sans`}>{r.seq}회</td>
                      <td className={`${TD} text-right`}>{r.purchase_date}</td>
                      <td className={`${TD} text-right`}>{r.payment_date}</td>
                      <td className={`${TD} text-right`}>{n(r.qty_kg)}</td>
                      <td className={`${TD} text-right`}>{n(r.amount_krw)}</td>
                      <td className={`${TD} text-right text-ink-2`}>{n(r.amount_max_krw)}</td>
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  <tr className="border-t border-rule-2 bg-surface-2 font-semibold">
                    <td className="px-4 py-3 text-left" colSpan={3}>
                      합계
                    </td>
                    <td className="num px-4 py-3 text-right">
                      {n(pay.reduce((a, r) => a + r.qty_kg, 0))}
                    </td>
                    <td className="num px-4 py-3 text-right">
                      {n(pay.reduce((a, r) => a + r.amount_krw, 0))}
                    </td>
                    <td className="num px-4 py-3 text-right text-ink-2">
                      {n(pay.reduce((a, r) => a + r.amount_max_krw, 0))}
                    </td>
                  </tr>
                </tfoot>
              </table>
            </div>
            <p className="mt-3 text-sm2 leading-relaxed text-ink-3">
              BASE는 당일 등급 단가로, STRESS는 상한가 {n(s.max_price)}원/kg로 계산한 같은 회차다.
              단가 기준 <span className="num">{pay[0].basis}</span> · 지급일 = 매입일 + 지급조건 7일.
              STRESS 합계는 매입 상한과 다른 기준이라 나란히 두고 비교하지 않는다 — 자금이 견디는지는
              재무가 이 표를 받아 판정한다.
            </p>
          </>
        ) : (
          /* ★ 일괄 안에는 payment_schedule 키 자체가 없다 — 설계다.
               빈 표를 그리면 "있는데 비었다"로 읽혀 누락과 구분되지 않는다. */
          <div className="rounded-card border border-rule bg-surface-2 px-6 py-5">
            <div className="mb-2 text-lg2 font-semibold">일괄 매입 — 회차 지급계획 없음</div>
            <div className="text-md2 leading-relaxed text-ink-2">
              한 번에 사는 안이라 회차가 나뉘지 않는다. 지급계획은 <b>분할 안에만</b> 실린다 — 비어
              있는 게 아니라 <b>해당하지 않는다.</b> 총액 {won(s.total_amount_krw)}이 지급일 하나에
              그대로 잡힌다.
            </div>
          </div>
        )}
      </Section>

      <Section title="근거와 고지" ref_="rationale · risks">
        <Fold title="근거" count={s.rationale.length} open>
          {s.rationale.map((r, i) => (
            <div key={i} className="border-b border-rule px-5 py-4 last:border-b-0">
              <div className="mb-2 flex flex-wrap items-center gap-3">
                <span className="rounded-ctl border border-rule-2 bg-surface-2 px-3 py-1 text-sm2 font-semibold text-ink-2">
                  {r.source}
                </span>
                <GradeBadge grade={r.evidence_grade} />
                <span className="ml-auto font-mono text-xs2 text-ink-3">{r.ref_id}</span>
              </div>
              <div className="text-md2 leading-relaxed">{r.claim}</div>
              <div className="mt-2 border-l-2 border-rule pl-3 text-sm2 leading-relaxed text-ink-3">
                {r.evidence_detail}
              </div>
            </div>
          ))}
        </Fold>
        <div className="mt-3">
          <Fold title="고지" count={s.risks.length}>
            {s.risks.map((t, i) => (
              <div
                key={i}
                className="flex items-start gap-3 border-b border-rule px-5 py-4 last:border-b-0"
              >
                <Chip tone={isHold(t) ? "hold" : "plain"}>{isHold(t) ? "보류" : "확인"}</Chip>
                <span className="text-md2 leading-relaxed text-ink-2">{t}</span>
              </div>
            ))}
          </Fold>
        </div>
      </Section>
    </div>
  );
}
