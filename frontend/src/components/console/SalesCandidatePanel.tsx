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
import { Failed, Metric, Skeleton, useConsoleData } from "@/components/console/ConsoleData";
import { ApiError, salesRun, type SalesCandidateOut, type SalesRunResponse } from "@/lib/api";
import {
  money,
  percent,
  quantity,
  salesConsole,
  type Money,
} from "@/lib/console_api";
import { TechDetails } from "@/app/console/finance/TechDetails";
import { runtimeText, verdictText } from "@/app/console/finance/user_text";

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
  allow_additional_sourcing: boolean;
  user_request: string;
}

const EMPTY: Form = {
  partner_id: "",
  item: "",
  requested_quantity_kg: "",
  preferred_unit_price_krw: "",
  preferred_delivery_date: "",
  //  🔴 결제일수를 화면이 미리 정하지 않는다. 비워 두면 판매가 **거래처 계약 결제일수**를
  //     싣는다 — 여기 30 을 박아 두면 거래처와 7일 결제로 바꾼 뒤에도 안이 30일로 선다.
  preferred_payment_days: "",
  preferred_payment_terms_type: "",
  allow_additional_sourcing: false,
  user_request: "",
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

export function SalesCandidatePanel({ simRun, asOf, onCreated }: { simRun: string; asOf: string; onCreated?: () => void }) {
  const [form, setForm] = useState<Form>(EMPTY);
  const partners = useConsoleData(
    `sales-candidate-partners:${simRun}:${asOf}`,
    () => salesConsole.activeCustomers(simRun, asOf),
    true,
  );
  const items = useConsoleData("sales-candidate-items", () => salesConsole.items(), true);
  const [state, setState] = useState<{
    data: SalesRunResponse | null;
    error: string | null;
    running: boolean;
  }>({ data: null, error: null, running: false });

  const ready = form.partner_id.trim() !== "" && form.item.trim() !== "" && form.requested_quantity_kg.trim() !== "";

  function submit() {
    const paymentDays = field(form.preferred_payment_days);
    if (paymentDays !== undefined) {
      const parsed = Number(paymentDays);
      if (!Number.isFinite(parsed) || !Number.isInteger(parsed) || parsed < 0) {
        setState({ data: null, error: "결제일수는 0 이상의 정수로 입력해 주세요.", running: false });
        return;
      }
    }
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
      preferred_payment_days: paymentDays === undefined ? undefined : Number(paymentDays),
      preferred_payment_terms_type: field(form.preferred_payment_terms_type),
      allow_additional_sourcing: form.allow_additional_sourcing,
      user_request: field(form.user_request),
    })
      .then((data) => {
        setState({ data, error: null, running: false });
        onCreated?.();
      })
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
          <Select
            label="거래처"
            value={form.partner_id}
            onChange={(value) => setForm({ ...form, partner_id: value })}
            disabled={partners.loading || Boolean(partners.error)}
            placeholder={partners.loading ? "거래처 조회 중" : "거래처 선택"}
            options={(partners.data?.rows ?? []).map((partner) => ({
              value: partner.partner_id,
              label: `${partner.partner_name ?? "이름 없음"} (${partner.partner_id})`,
            }))}
          />
          <FieldHelp>판매 대상 고객사를 선택합니다.</FieldHelp>
          <Select
            label="품목"
            value={form.item}
            onChange={(value) => setForm({ ...form, item: value })}
            disabled={items.loading || Boolean(items.error)}
            placeholder={items.loading ? "품목 조회 중" : "품목 선택"}
            options={(items.data?.rows ?? []).map((item) => ({
              value: item.item_name,
              label: `${item.item_name} (${item.item_code})`,
            }))}
          />
          <FieldHelp>등록된 품목 원장에서 선택합니다.</FieldHelp>
          <Input
            label="요청 수량 (kg)"
            value={form.requested_quantity_kg}
            onChange={(v) => setForm({ ...form, requested_quantity_kg: v })}
            type="number"
            inputMode="decimal"
            min="0"
            step="any"
          />
          <FieldHelp>판매를 검토할 수량입니다.</FieldHelp>
          <label className="flex flex-col gap-1 text-[11.5px]">
            <span className="text-ink2">결제 방식</span>
            <select value={form.preferred_payment_terms_type} onChange={(event) => setForm({ ...form, preferred_payment_terms_type: event.target.value })} className="rounded-md border px-2 py-1 text-[12px]" style={{ borderColor: "var(--color-hair)" }}>
              <option value="">미지정</option><option value="SINGLE">일시 결제</option><option value="INSTALLMENT">분할 결제</option>
            </select>
          </label>
          <FieldHelp>미지정은 일시 결제로 간주하지 않습니다.</FieldHelp>
          <Input label="요청 메모" value={form.user_request} onChange={(v) => setForm({ ...form, user_request: v })} />
          <FieldHelp>수량·단가 대신 쓰는 입력이 아닙니다. 추가 상황 설명에 사용합니다.</FieldHelp>
          <Input
            label="희망 단가 (원/kg)"
            value={form.preferred_unit_price_krw}
            onChange={(v) => setForm({ ...form, preferred_unit_price_krw: v })}
            type="number"
            inputMode="decimal"
            min="0"
            step="any"
          />
          <FieldHelp>미입력 시 현재 요청만으로 재무 검증을 완료하지 못할 수 있습니다.</FieldHelp>
          <Input
            label="희망 납품일"
            value={form.preferred_delivery_date}
            onChange={(v) => setForm({ ...form, preferred_delivery_date: v })}
            type="date"
          />
          <FieldHelp>판매를 희망하는 납품일입니다.</FieldHelp>
          <Input
            label="결제일수"
            value={form.preferred_payment_days}
            onChange={(v) => setForm({ ...form, preferred_payment_days: v })}
            placeholder="비우면 거래처 계약 결제일"
            type="number"
            inputMode="numeric"
            min="0"
            step="1"
          />
          <FieldHelp>비우면 명시적인 결제일수를 요청하지 않습니다.</FieldHelp>
          <label className="flex items-start gap-2 text-[11.5px] sm:col-span-2">
            <input
              type="checkbox"
              checked={form.allow_additional_sourcing}
              onChange={(event) =>
                setForm({ ...form, allow_additional_sourcing: event.target.checked })
              }
              className="mt-0.5"
            />
            <span>
              재고가 부족하면 추가 매입 가능성을 검토
              <small className="mt-1 block text-[10.5px] leading-relaxed text-ink2">
                선택하면 부족분의 추가 매입 가능량을 확인합니다. 실제 매입이나 판매가
                자동 확정되는 것은 아닙니다.
              </small>
            </span>
          </label>
        </div>
        {partners.error && <p className="mb-0 mt-2 text-[11px] text-[var(--color-t-bad)]">거래처 조회 실패: {partners.error}</p>}
        {items.error && <p className="mb-0 mt-2 text-[11px] text-[var(--color-t-bad)]">품목 조회 실패: {items.error}</p>}
        {!partners.loading && !partners.error && (partners.data?.rows.length ?? 0) === 0 && <p className="mb-0 mt-2 text-[11px] text-ink2">선택 가능한 거래처가 없습니다.</p>}
        {!items.loading && !items.error && (items.data?.rows.length ?? 0) === 0 && <p className="mb-0 mt-2 text-[11px] text-ink2">등록된 품목이 없습니다.</p>}
        <button
          onClick={submit}
          disabled={!ready || state.running}
          className="mt-3 rounded-lg border px-3 py-2 text-[12px] disabled:opacity-50"
          style={{ borderColor: "var(--color-hair)" }}
        >
          {state.running ? "후보를 만드는 중" : "판매 후보 생성"}
        </button>
        <p className="mb-0 mt-2 text-[11px] text-ink2">
          거래처·품목·수량은 필수입니다. 이 단계에서는 판매 후보만 생성되며 실제 판매 확정은 기존 승인 및 재검증 절차를 거쳐야 합니다.
        </p>
      </Panel>
      {state.running && <Skeleton what="판매 후보" />}
      {state.error && <Failed what="판매 후보 생성" message={state.error} />}
      {state.data && <Result data={state.data} />}
    </>
  );
}

function Select({
  label,
  value,
  onChange,
  options,
  placeholder,
  disabled,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: { value: string; label: string }[];
  placeholder: string;
  disabled?: boolean;
}) {
  return (
    <label className="flex flex-col gap-1 text-[11.5px]">
      <span className="text-ink2">{label}</span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        disabled={disabled}
        className="rounded-md border px-2 py-1 text-[12px] disabled:opacity-50"
        style={{ borderColor: "var(--color-hair)" }}
      >
        <option value="">{options.length === 0 && !disabled ? `${placeholder} (없음)` : placeholder}</option>
        {options.map((option) => (
          <option key={option.value} value={option.value}>{option.label}</option>
        ))}
      </select>
    </label>
  );
}

function Input({
  label,
  value,
  onChange,
  placeholder,
  mono,
  type = "text",
  inputMode,
  min,
  step,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  mono?: boolean;
  type?: "text" | "number" | "date";
  inputMode?: "decimal" | "numeric";
  min?: string;
  step?: string;
}) {
  return (
    <label className="flex flex-col gap-1 text-[11.5px]">
      <span className="text-ink2">{label}</span>
      <input
        type={type}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        spellCheck={false}
        inputMode={inputMode}
        min={min}
        step={step}
        className={`rounded-md border px-2 py-1 text-[12px] ${mono ? "font-mono text-[11px]" : ""}`}
        style={{ borderColor: "var(--color-hair)" }}
      />
    </label>
  );
}

function FieldHelp({ children }: { children: React.ReactNode }) {
  return <p className="-mt-1 mb-0 text-[10.5px] leading-relaxed text-ink2">{children}</p>;
}

function Result({ data }: { data: SalesRunResponse }) {
  return (
    <>
      <Panel title="검토 결과" subtitle="아래 값은 각 부서가 낸 판정 그대로입니다">
        <div className="grid gap-2 sm:grid-cols-2">
          <Metric label="처리 결과" value={END_CODES[data.end_code] ?? "정의되지 않은 결과"} />
          <Metric label="만들어진 후보" value={`${data.candidates.length}건`} />
        </div>
        <p className="mb-0 mt-3 text-[12px] text-ink2">후보 {data.candidates.length}건을 생성했습니다. 아래 금일 판매안에서 검토할 수 있습니다.</p>
        {data.reason && <p className="mb-0 mt-1 text-[12px] text-ink2">{data.reason}</p>}
        <div className="mt-3">
          <TechDetails>
            {/* 🔴 코드와 뜻을 같이 보여 준다 — 뜻만 남기면 되짚을 수 없다. */}
            <p className="m-0 font-mono text-[11px] text-ink2">
              end_code {data.end_code} · request_id {data.request_id} · history_run_id{" "}
              {data.history_run_id ?? "null"}
            </p>
          </TechDetails>
        </div>
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
 *
 * ⚠️ **`reason_codes` 는 실패 사유 목록이 아니다.** 그 배열에는 통과 사유까지 함께
 *   들어 있다(2026-09-14 실측: PASS 판정에도 일곱 개가 실린다). «거절 사유» 라고
 *   이름 붙여 보여 주면 통과한 규칙이 실패로 읽힌다 — 이름을 붙이지 않고 기술
 *   상세 안에 그대로 둔다.
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
        <p className="mb-0 mt-1 text-[11.5px] text-ink2">이 후보에 해당 검토가 호출되지 않았습니다.</p>
      ) : (
        <>
          <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-[11.5px]">
            <span>
              판단 <b>{verdictText(state.verdict)}</b>
            </span>
            <span className="text-ink2">조회 {runtimeText(state.runtime)}</span>
          </div>
          <div className="mt-2">
            <TechDetails summary="적용된 규칙 사유">
              <p className="m-0 font-mono text-[11px] text-ink2">
                runtime {state.runtime} · verdict {state.verdict ?? "null"}
              </p>
              {state.reasons.length > 0 && (
                <p className="mb-0 mt-2 break-all font-mono text-[10.5px] text-ink2">
                  {state.reasons.join(", ")}
                </p>
              )}
            </TechDetails>
          </div>
        </>
      )}
    </div>
  );
}
