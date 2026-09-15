"use client";

/**
 * 거래처 여신 현황 — **얼마까지 더 팔 수 있고, 언제 여유가 생기는가.**
 *
 * 🔴 **화면이 여신을 세지 않는다.** 한도 − 미수, 미수 ÷ 한도, 수금 뒤 남는 여신은 전부
 *    백엔드(`/console/finance/credit`)가 판정 경로와 같은 함수로 센 값이다.
 *
 * 🔴 **수금 예정은 예정이다.** «이 돈이 들어오면» 이라고 적고, 들어온 것처럼 적지 않는다.
 *    한도를 되돌려 주는 날짜는 없다 — 여신은 미수금이 실제로 줄어야 풀린다.
 */

import { Panel } from "@/components/console/Blocks";
import {
  EmptyRows,
  Failed,
  Metric,
  Metrics,
  Skeleton,
  useConsoleData,
} from "@/components/console/ConsoleData";

import { fetchCredit, type CreditResponse, type PartnerCredit } from "./credit_api";
import {
  creditGradeText,
  longDate,
  moneyWon,
  partnerText,
  paymentTermText,
  ratioPercent,
  toNumber,
} from "./user_text";

export function CreditPanel({ simRun, asOf }: { simRun: string; asOf: string }) {
  const state = useConsoleData<CreditResponse>(
    `credit:${simRun}:${asOf}`,
    () => fetchCredit(simRun, asOf),
    true,
  );
  return (
    <Panel
      title="거래처 여신"
      subtitle="얼마까지 더 판매할 수 있는지와 미수금이 들어오면 여신이 얼마나 풀리는지를 봅니다"
    >
      {state.loading ? (
        <Skeleton what="여신 현황" />
      ) : state.error ? (
        <Failed what="여신 현황" message={state.error} />
      ) : !state.data || state.data.partners.length === 0 ? (
        <EmptyRows what="여신이 걸린 거래처" />
      ) : (
        <div className="flex flex-col gap-5">
          {state.data.partners.map((partner) => (
            <PartnerCreditBlock key={partner.partner_id} partner={partner} />
          ))}
        </div>
      )}
    </Panel>
  );
}

function PartnerCreditBlock({ partner }: { partner: PartnerCredit }) {
  const available = toNumber(partner.available_credit_krw);
  const hasLimit = toNumber(partner.credit_limit_krw) !== null;
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <b className="text-[13px]">{partnerText(partner.partner_name, partner.partner_id)}</b>
        <span className="text-[12px] text-ink2">결제 조건 · {paymentTermText(partner.payment_days)}</span>
        {hasLimit && (
          <span className="text-[11.5px] text-ink2">{creditGradeText(partner.credit_limit_evidence_grade)}</span>
        )}
      </div>

      <Metrics>
        <Metric
          label="여신 한도"
          value={hasLimit ? moneyWon(partner.credit_limit_krw) : "한도 미정"}
          hint={hasLimit ? undefined : "한도가 정해지지 않아 판매 여신 판정을 할 수 없습니다"}
        />
        <Metric
          label="현재 미수금"
          value={moneyWon(partner.current_ar_krw)}
          hint={`받지 않은 채권 ${partner.open_receivable_count}건`}
        />
        <Metric
          label="남은 여신"
          value={available === null ? "판단 불가" : moneyWon(partner.available_credit_krw)}
          hint={
            available === null
              ? undefined
              : available < 0
                ? "이미 한도를 넘었습니다 — 미수금을 먼저 받아야 판매할 수 있습니다"
                : "이 금액 안의 판매는 여신 기준을 넘지 않습니다"
          }
        />
        <Metric
          label="여신 사용률"
          value={toNumber(partner.credit_utilization_rate) === null ? "판단 불가" : ratioPercent(partner.credit_utilization_rate)}
          hint={toNumber(partner.overdue_ar_krw) ? `그중 연체 ${moneyWon(partner.overdue_ar_krw)}` : "연체된 미수금 없음"}
        />
      </Metrics>

      <div>
        <b className="text-[12px]">미수금이 들어오면 풀리는 여신</b>
        {partner.upcoming_collections.length === 0 ? (
          <p className="mb-0 mt-1 text-[12px] text-ink2">받을 미수금이 없습니다.</p>
        ) : (
          <ul className="m-0 mt-2 flex list-none flex-col gap-1.5 p-0 text-[12px]">
            {partner.upcoming_collections.map((item, index) => (
              <li
                key={`${item.due_date}-${index}`}
                className="flex flex-wrap items-center justify-between gap-2 rounded-lg border px-3 py-2"
                style={{ borderColor: "var(--color-hair)" }}
              >
                <span>
                  {longDate(item.due_date)}
                  {item.overdue && (
                    <span className="ml-2" style={{ color: "var(--color-t-bad)" }}>
                      결제 예정일 지남
                    </span>
                  )}
                </span>
                <span className="tabular-nums">{moneyWon(item.amount_krw)} 회수 예정</span>
                <span className="tabular-nums text-ink2">
                  {item.available_credit_after_krw === null
                    ? "한도 미정"
                    : `받으면 남는 여신 ${moneyWon(item.available_credit_after_krw)}`}
                </span>
              </li>
            ))}
          </ul>
        )}
        <p className="mb-0 mt-2 text-[11.5px] text-ink2">
          계약상 결제 예정일을 기준으로 한 예상입니다. 실제 입금이 확인되어야 여신이 풀립니다.
        </p>
      </div>
    </div>
  );
}
