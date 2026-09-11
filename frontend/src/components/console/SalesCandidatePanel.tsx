"use client";

/**
 * 판매 후보 생성 — **마스터가 순서를 소유한다.**
 *
 * ```text
 * 화면(업무 요청)  →  POST /master/sales/run  →  물류 PRE_SALES → 판매 제안 → 재무 검증
 *                                              →  end_code · 후보 · 판정
 * ```
 *
 * 🔴 **화면이 보내는 것은 업무 요청뿐이다.** 원가·여신·마진·물류 판정·`end_code` 를
 *    실어 보내지 않는다 — 그 값을 화면이 정하면 판정이 화면에서 시작된다.
 *
 * 🔴 **화면이 계산하지 않는다.** 공헌이익도 판정도 회신에 실린 값을 적기만 한다.
 *    회신에 없는 칸은 «없음» 이지 0 이 아니다.
 */

import { useState } from "react";

import { Panel } from "@/components/console/Blocks";
import { Failed, Metric, Skeleton } from "@/components/console/ConsoleData";
import { ApiError, salesRun, type SalesCandidateOut, type SalesRunResponse } from "@/lib/api";
import { money, percent, quantity, type Money } from "@/lib/console_api";

/** 마스터가 낸 종료 코드의 뜻. **코드는 백엔드 값이고 여기서는 문장만 붙인다.** */
const END_CODES: Record<string, string> = {
  SL1_PRESENTED: "판매 가능한 안이 있습니다",
  SL2_NO_CANDIDATE: "후보가 만들어지지 않았습니다",
  SL3_ALL_REJECTED: "후보가 있었으나 검증에서 모두 탈락했습니다",
  SL4_NOT_STARTED: "판매 흐름이 시작되지 않았습니다",
  SL5_BUDGET_EXHAUSTED: "호출 예산이 소진되었습니다",
  SL6_VALIDATION_UNRESOLVED: "필요 검증이 완료되지 않았습니다",
};

interface Form {
  partner_id: string;
  item: string;
  requested_quantity_kg: string;
  preferred_unit_price_krw: string;
  preferred_delivery_date: string;
  preferred_payment_days: string;
  preferred_payment_terms_type: string;
}

const EMPTY: Form = {
  partner_id: "",
  item: "",
  requested_quantity_kg: "",
  preferred_unit_price_krw: "",
  preferred_delivery_date: "",
  preferred_payment_days: "30",
  preferred_payment_terms_type: "SINGLE",
};

function field(value: string): string | undefined {
  //  ⚠️ 빈 칸은 «안 적었다» 이다. 0 이나 오늘 날짜로 채우지 않는다.
  const trimmed = value.trim();
  return trimmed === "" ? undefined : trimmed;
}

function text(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function amount(value: unknown): Money | null {
  return typeof value === "string" || typeof value === "number" ? value : null;
}

function block(candidate: SalesCandidateOut, capability: string) {
  const reply = candidate.validations[capability];
  const payload = (reply?.payload ?? {}) as Record<string, unknown>;
  return {
    present: reply !== undefined,
    runtime: text(reply?.runtime_status) ?? "호출 없음",
    verdict: text(payload.finance_verdict) ?? text(payload.verdict),
    reasons: Array.isArray(payload.reason_codes) ? (payload.reason_codes as string[]) : [],
  };
}

export function SalesCandidatePanel({ simRun, asOf }: { simRun: string; asOf: string }) {
  const [form, setForm] = useState<Form>(EMPTY);
  const [state, setState] = useState<{
    data: SalesRunResponse | null;
    error: string | null;
    running: boolean;
  }>({ data: null, error: null, running: false });

  const ready = form.partner_id.trim() !== "" && form.item.trim() !== "" && form.requested_quantity_kg.trim() !== "";

  function submit() {
    setState({ data: null, error: null, running: true });
    salesRun({
      as_of: asOf,
      sim_run_id: simRun,
      business_mode: "SPOT_SALES",
      partner_id: form.partner_id.trim(),
      item: form.item.trim(),
      requested_quantity_kg: form.requested_quantity_kg.trim(),
      preferred_unit_price_krw: field(form.preferred_unit_price_krw),
      preferred_delivery_date: field(form.preferred_delivery_date),
      preferred_payment_days: field(form.preferred_payment_days)
        ? Number(form.preferred_payment_days)
        : undefined,
      preferred_payment_terms_type: field(form.preferred_payment_terms_type),
    })
      .then((data) => setState({ data, error: null, running: false }))
      .catch((error: unknown) =>
        setState({
          data: null,
          running: false,
          //  ★ 서버 문장을 그대로 올린다 — 무엇을 고쳐야 하는지 알려 주는 말이다.
          error:
            error instanceof ApiError
              ? `[${error.status || "연결 실패"}] ${error.message}`
              : String(error),
        }),
      );
  }

  return (
    <>
      <Panel title="판매 후보 생성" subtitle="업무 요청만 보냅니다 — 원가·여신·판정은 각 도메인이 답합니다">
        <div className="grid gap-2 sm:grid-cols-3">
          <Input label="거래처" value={form.partner_id} onChange={(v) => setForm({ ...form, partner_id: v })} mono />
          <Input label="품목" value={form.item} onChange={(v) => setForm({ ...form, item: v })} />
          <Input
            label="요청 수량 (kg)"
            value={form.requested_quantity_kg}
            onChange={(v) => setForm({ ...form, requested_quantity_kg: v })}
          />
          <Input
            label="희망 단가 (원/kg)"
            value={form.preferred_unit_price_krw}
            onChange={(v) => setForm({ ...form, preferred_unit_price_krw: v })}
          />
          <Input
            label="희망 납품일"
            value={form.preferred_delivery_date}
            onChange={(v) => setForm({ ...form, preferred_delivery_date: v })}
            placeholder="YYYY-MM-DD"
          />
          <Input
            label="결제일수"
            value={form.preferred_payment_days}
            onChange={(v) => setForm({ ...form, preferred_payment_days: v })}
          />
        </div>
        <button
          onClick={submit}
          disabled={!ready || state.running}
          className="mt-3 rounded-lg border px-3 py-2 text-[12px] disabled:opacity-50"
          style={{ borderColor: "var(--color-hair)" }}
        >
          {state.running ? "후보를 만드는 중" : "후보 생성"}
        </button>
        <p className="mb-0 mt-2 text-[11px] text-ink2">
          거래처·품목·수량은 필수입니다. 단가와 결제조건이 없으면 재무가 입력 미비로 판정을 닫습니다.
        </p>
      </Panel>
      {state.running && <Skeleton what="판매 후보" />}
      {state.error && <Failed what="판매 후보 생성" message={state.error} />}
      {state.data && <Result data={state.data} />}
    </>
  );
}

function Input({
  label,
  value,
  onChange,
  placeholder,
  mono,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  mono?: boolean;
}) {
  return (
    <label className="flex flex-col gap-1 text-[11.5px]">
      <span className="text-ink2">{label}</span>
      <input
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        spellCheck={false}
        className={`rounded-md border px-2 py-1 text-[12px] ${mono ? "font-mono text-[11px]" : ""}`}
        style={{ borderColor: "var(--color-hair)" }}
      />
    </label>
  );
}

function Result({ data }: { data: SalesRunResponse }) {
  return (
    <>
      <Panel title="Master 판정" subtitle="종료 코드는 마스터가 낸 값 그대로입니다">
        <div className="grid gap-2 sm:grid-cols-3">
          {/* 🔴 코드와 뜻을 같이 보여 준다 — 뜻만 남기면 되짚을 수 없다. */}
          <Metric label="end_code" value={data.end_code} hint={END_CODES[data.end_code] ?? "정의되지 않은 코드"} />
          <Metric label="요청" value={data.request_id} />
          <Metric label="후보 수" value={`${data.candidates.length}건`} />
        </div>
        <p className="mb-0 mt-3 text-[12px] text-ink2">{data.reason}</p>
      </Panel>
      {data.candidates.map((candidate, index) => (
        <CandidateCard key={index} candidate={candidate} />
      ))}
      {data.report_text && (
        <Panel title="AI 설명" subtitle="숫자는 위 판정에서 옵니다 — 설명이 값을 만들지 않습니다">
          <p className="mb-0 whitespace-pre-wrap text-[12px] text-ink2">{data.report_text}</p>
        </Panel>
      )}
    </>
  );
}

function CandidateCard({ candidate }: { candidate: SalesCandidateOut }) {
  const scenario = candidate.scenario;
  const finance = block(candidate, "FINANCIAL_VALIDATION");
  const logistics = block(candidate, "SELLABLE_SUPPLY_CONTEXT");
  return (
    <Panel
      title={`${text(scenario.scenario_id) ?? "후보"} · ${text(scenario.scenario_type) ?? ""}`}
      subtitle={candidate.detail}
    >
      <div className="grid gap-2 sm:grid-cols-4">
        <Metric label="수량" value={quantity(amount(scenario.quantity_kg))} />
        <Metric label="단가" value={money(amount(scenario.unit_price_krw))} />
        <Metric label="매출액" value={money(amount(scenario.reported_sales_amount_krw))} />
        <Metric
          label="공헌이익"
          value={money(amount(scenario.contribution_margin_krw))}
          hint={percent(amount(scenario.contribution_margin_rate))}
        />
      </div>
      <div className="mt-3 grid gap-2 sm:grid-cols-2">
        <Domain title="재무" block={finance} />
        <Domain title="물류" block={logistics} />
      </div>
      {candidate.missing_terms.length > 0 && (
        <p className="mb-0 mt-3 text-[11.5px] text-ink2">
          빠진 조건: <span className="font-mono">{candidate.missing_terms.join(", ")}</span>
        </p>
      )}
      {Array.isArray(scenario.evidence_refs) && (scenario.evidence_refs as string[]).length > 0 && (
        <details className="mt-3 text-[11.5px]">
          <summary className="cursor-pointer text-ink2">근거 {(scenario.evidence_refs as string[]).length}건</summary>
          <p className="mb-0 mt-1 break-all font-mono text-[10.5px] text-ink2">
            {(scenario.evidence_refs as string[]).join(", ")}
          </p>
        </details>
      )}
    </Panel>
  );
}

/**
 * 부서 판정 한 칸.
 *
 * 🔴 **Runtime 과 Verdict 를 합치지 않는다.** «못 돌았다» 와 «판정이 없다» 는 다른
 *    사실이고, 한 badge 로 뭉치면 그 둘을 구분할 수 없다.
 */
function Domain({
  title,
  block: state,
}: {
  title: string;
  block: { present: boolean; runtime: string; verdict: string | null; reasons: string[] };
}) {
  return (
    <div className="rounded-lg border p-3" style={{ borderColor: "var(--color-hair)" }}>
      <b className="text-[12px]">{title}</b>
      {!state.present ? (
        <p className="mb-0 mt-1 text-[11.5px] text-ink2">이 후보에 해당 검증이 호출되지 않았습니다.</p>
      ) : (
        <>
          <div className="mt-1 flex gap-4 text-[11.5px]">
            <span>
              Runtime <b className="font-mono">{state.runtime}</b>
            </span>
            <span>
              Verdict <b className="font-mono">{state.verdict ?? "판정 없음"}</b>
            </span>
          </div>
          {state.reasons.length > 0 && (
            <p className="mb-0 mt-1 break-all font-mono text-[10.5px] text-ink2">
              {state.reasons.join(", ")}
            </p>
          )}
        </>
      )}
    </div>
  );
}
