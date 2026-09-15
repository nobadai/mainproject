/**
 * 입력 출처 배지 — **이 화면이 반드시 보여줘야 하는 셋 중 하나.**
 *
 * 같은 값이라도 실 DB 에서 온 것과 mock 에서 온 것은 판단의 무게가 다르다. 값만
 * 그리면 리포트를 읽는 사람이 **전부 실측으로 읽는다.**
 */

const GRADE_STYLE: Record<string, string> = {
  MEASURED: "bg-accent-wash text-accent-ink",
  DERIVED: "bg-sky-wash text-sky",
  MOCK: "bg-gold-wash text-gold",
  MISSING: "bg-warn-wash text-warn",
};

const GRADE_NOTE: Record<string, string> = {
  MEASURED: "실 DB 에서 그대로 읽음",
  DERIVED: "실 DB 값에서 규칙으로 파생",
  MOCK: "mock 파일에서 옴 — 실측이 아님",
  MISSING: "못 구해서 비움",
};

export function SourceBadges({ sources }: { sources: Record<string, string> }) {
  const entries = Object.entries(sources);
  if (entries.length === 0) return null;

  return (
    <div>
      <p className="mb-1.5 text-[10.5px] font-semibold uppercase tracking-[0.12em] text-faint">
        입력 출처
      </p>
      <div className="flex flex-wrap gap-1.5">
        {entries.map(([key, value]) => {
          const [grade, ...rest] = value.split(":");
          return (
            <span
              key={key}
              title={`${rest.join(":")} — ${GRADE_NOTE[grade] ?? ""}`}
              className={`rounded px-2 py-0.5 font-mono text-[11px] font-medium ${
                GRADE_STYLE[grade] ?? "bg-sunk text-muted"
              }`}
            >
              {key} · {grade}
            </span>
          );
        })}
      </div>
    </div>
  );
}

/** 지적·확인 필요 묶음. **비어 있으면 그리지 않는다** — 할 말이 없으면 안 한다. */
export function Panel({
  title,
  items,
  tone = "plain",
}: {
  title: string;
  items: string[];
  tone?: "plain" | "attn";
}) {
  if (items.length === 0) return null;
  const attn = tone === "attn";

  return (
    <div
      className={`rounded-lg border p-3 ${
        attn ? "border-warn/25 bg-warn-wash" : "border-line bg-sunk"
      }`}
    >
      <p
        className={`mb-1.5 text-[11px] font-semibold uppercase tracking-[0.05em] ${
          attn ? "text-warn" : "text-muted"
        }`}
      >
        {title}
      </p>
      <ul className="m-0 list-disc space-y-1 pl-4 text-[13px] text-muted">
        {items.map((line) => (
          <li key={line}>{line}</li>
        ))}
      </ul>
    </div>
  );
}
