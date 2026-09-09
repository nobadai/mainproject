"use client";

/**
 * 재학습 검증 표 — **바꿀지 말지를 여기서 정합니다.**
 *
 * ★ 왜 표인가. 지금까지 화면은 「배추 — 후보가 낫습니다」 한 줄만 보였습니다.
 *   그러면 사람이 할 수 있는 게 **글을 믿고 누르는 것뿐**입니다.
 *   바꿀지 말지는 숫자를 나란히 놓고 정하는 일입니다.
 *
 * ★ 판정 규칙을 그대로 보입니다.
 *
 *       차이(현행−후보)  >  시드 편차×2     ->  후보가 낫다
 *       차이가 그 안      ->  판정 불가 (**«같다» 가 아니라 «모른다»**)
 *
 *   그래서 「차이」와 「필요」를 **나란히** 놓습니다. 둘을 떨어뜨려 놓으면
 *   판정이 어디서 나왔는지 눈으로 못 따라갑니다.
 *
 * ★ 앵커를 같이 보입니다. 후보가 현행보다 나아도 **어제값을 못 이기면**
 *   모델을 쓸 이유가 없습니다. 그 사실이 표에서 바로 보여야 합니다.
 *
 * ★ `null` 은 0 이 아니라 공란입니다. 표본이 모자라 판정을 안 한 품목입니다.
 *
 * 우리 ML 콘솔(`localhost:3100`)의 같은 이름 컴포넌트를 이 화면 색으로 옮긴 것입니다.
 */

import type { VerifyItem } from "@/lib/mlConsole";

import { en, ITEM, VERIFY } from "./labels";

const TONE: Record<string, { fg: string; bg: string }> = {
  "후보가 낫다": { fg: "var(--color-t-good)", bg: "var(--color-t-good-bg)" },
  "후보가 나쁘다": { fg: "var(--color-t-warn)", bg: "var(--color-t-warn-bg)" },
  "판정 불가": { fg: "var(--color-mut)", bg: "var(--color-sunk)" },
  "표본 부족": { fg: "var(--color-mut)", bg: "var(--color-sunk)" },
};

/** Error %을 백분율로. `null` 이면 공란. */
function pct(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : `${(v * 100).toFixed(2)}%`;
}

/** 부호를 붙여 보입니다 — 양수면 후보가 낫다는 뜻이라 방향이 중요합니다. */
function signed(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return `${v >= 0 ? "+" : "−"}${(Math.abs(v) * 100).toFixed(2)}%p`;
}

const HEAD: { label: string; sub?: string; right?: boolean }[] = [
  { label: "품목" },
  { label: "측정 건수", right: true },
  { label: "출발점", sub: "어제 가격", right: true },
  { label: "현재 모델", right: true },
  { label: "후보 모델", right: true },
  { label: "차이", sub: "현재−후보", right: true },
  { label: "최소 차이", sub: "오차 편차×2", right: true },
  { label: "진단 결과" },
];

export function VerifyTable({ items }: { items: VerifyItem[] }) {
  if (!items || items.length === 0) return null;

  const decided = items.filter((r) => r.verdict !== "표본 부족");
  const better = decided.filter((r) => r.verdict === "후보가 낫다").length;
  const worse = decided.filter((r) => r.verdict === "후보가 나쁘다").length;

  return (
    <section className="flex flex-col gap-2.5">
      <div
        className="thin-scroll overflow-x-auto rounded-lg border"
        style={{ borderColor: "var(--color-hair)" }}
      >
        <table className="w-full border-collapse text-[12px]">
          <thead>
            <tr style={{ background: "var(--color-sunk)" }}>
              {HEAD.map((h) => (
                <th
                  key={h.label}
                  scope="col"
                  className={`whitespace-nowrap px-3 py-2 text-[11px] font-medium ${
                    h.right ? "text-right" : "text-left"
                  }`}
                  style={{ color: "var(--color-mut)" }}
                >
                  {h.label}
                  {h.sub && (
                    <span className="ml-1 font-normal" style={{ color: "var(--color-mut2)" }}>
                      {h.sub}
                    </span>
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {items.map((r) => {
              const tone = TONE[r.verdict] ?? TONE["판정 불가"];
              //  후보가 앵커를 못 이기면 그 칸을 눈에 띄게 둡니다 —
              //  현행보다 나아도 어제값보다 나쁘면 쓸 이유가 없습니다.
              const losesToAnchor =
                r.cand !== null && r.anchor !== null && r.cand >= r.anchor;
              return (
                <tr key={r.item} style={{ borderTop: "1px solid var(--color-hair-soft)" }}>
                  <td className="whitespace-nowrap px-3 py-2 font-medium">{en(ITEM, r.item)}</td>
                  <td
                    className="tabular px-3 py-2 text-right font-mono"
                    style={{ color: "var(--color-mut2)" }}
                  >
                    {r.n.toLocaleString("ko-KR")}
                  </td>
                  <td
                    className="tabular px-3 py-2 text-right font-mono"
                    style={
                      losesToAnchor
                        ? { background: "var(--color-t-warn-bg)", color: "var(--color-t-warn)" }
                        : { color: "var(--color-mut)" }
                    }
                    title={
                      losesToAnchor ? "후보 모델이 어제 가격 그대로 쓴 것보다 못합니다" : undefined
                    }
                  >
                    {pct(r.anchor)}
                  </td>
                  <td className="tabular px-3 py-2 text-right font-mono">{pct(r.cur)}</td>
                  <td className="tabular px-3 py-2 text-right font-mono font-semibold">
                    {pct(r.cand)}
                  </td>
                  <td
                    className="tabular px-3 py-2 text-right font-mono"
                    style={{
                      color:
                        r.diff !== null && r.diff !== undefined && r.diff > 0
                          ? "var(--color-t-good)"
                          : "var(--color-t-warn)",
                    }}
                  >
                    {signed(r.diff)}
                  </td>
                  <td
                    className="tabular px-3 py-2 text-right font-mono"
                    style={{ color: "var(--color-mut2)" }}
                  >
                    {r.need === null || r.need === undefined
                      ? "—"
                      : `±${(r.need * 100).toFixed(2)}%p`}
                  </td>
                  <td className="px-3 py-2">
                    <span
                      className="whitespace-nowrap rounded px-2 py-0.5 text-[11px] font-semibold"
                      style={{ background: tone.bg, color: tone.fg }}
                    >
                      {en(VERIFY, r.verdict)}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <p className="m-0 text-[11.5px] leading-relaxed" style={{ color: "var(--color-mut)" }}>
        <b style={{ color: "var(--color-ink)" }}>표 보는 법</b> — 오차의 &laquo;차이&raquo;가
        &laquo;최소 차이&raquo; 기준보다 더 크게 벌어졌을 때만 결과를 판정합니다. 기준 범위 안에
        있을 때는{" "}
        <b style={{ color: "var(--color-ink)" }}>
          &laquo;둘이 같다&raquo;가 아니라 &laquo;알 수 없다&raquo;
        </b>
        가 정답입니다. 초기작업 무작위값(시드)만 바꿔서 다시 배워도 그 정도의 수치 차이는 스스로
        흔들리기 때문입니다.
        <br />
        <b style={{ color: "var(--color-ink)" }}>출발점</b>은 어제 가격을 오늘 예측값으로 그대로
        가져다 썼을 때 발생하는 오차입니다.{" "}
        <b style={{ color: "var(--color-ink)" }}>
          후보 모델이 이 어제 가격 기준보다 오차가 크다면 모델을 쓸 이유가 전혀 없습니다
        </b>{" "}
        — 그런 수치는 노란색으로 표시됩니다.
      </p>

      {worse > 0 && (
        <p
          className="m-0 rounded-lg px-3.5 py-2.5 text-[12px] leading-relaxed"
          style={{ background: "var(--color-t-warn-bg)", color: "var(--color-t-warn)" }}
        >
          ★ <b>성능이 떨어진 품목이 {worse}개 있습니다.</b> 성능이 좋아진 품목이 {better}개
          있더라도 모델을 교체하지 않습니다. 과거에 양파 모델을 따로 분리했던 안이 3단계 기간
          검증을 모두 통과하고도, 실제 현장에서 배추 예측 오차를 5.70%나 나쁘게 만들었던 적이
          있습니다.
        </p>
      )}
      {worse === 0 && better === 0 && decided.length > 0 && (
        <p
          className="m-0 rounded-lg px-3.5 py-2.5 text-[12px] leading-relaxed"
          style={{ background: "var(--color-sunk)", color: "var(--color-mut)" }}
        >
          성능이 떨어진 품목은 없지만,{" "}
          <b style={{ color: "var(--color-ink)" }}>
            확실하게 좋아졌다고 증명된 품목도 없습니다.
          </b>{" "}
          모델을 바꿀 이유가 없으므로 교체하지 않고 유지합니다.
        </p>
      )}
    </section>
  );
}
