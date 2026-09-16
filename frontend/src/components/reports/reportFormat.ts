export type ReportFacts = Record<string, unknown>;

export function record(value: unknown): ReportFacts {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as ReportFacts)
    : {};
}

export function rows(value: unknown): ReportFacts[] {
  return Array.isArray(value) ? value.map(record) : [];
}

export function text(value: unknown, empty = "—"): string {
  return value === null || value === undefined || value === "" ? empty : String(value);
}

export function won(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  return Number.isFinite(number) ? `${number.toLocaleString("ko-KR")}원` : "—";
}

export function kg(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  return Number.isFinite(number) ? `${number.toLocaleString("ko-KR")}kg` : "—";
}

export function date(value: unknown): string {
  return text(value, "기록 없음");
}

export function filename(kind: "finance" | "sales", facts: ReportFacts): string {
  const start = text(facts.start_date, "");
  const end = text(facts.end_date, "");
  const range = start && end && start !== end ? `${start}_${end}` : end || start || "report";
  return `haetdeul_${kind}_report_${range}.pdf`;
}
