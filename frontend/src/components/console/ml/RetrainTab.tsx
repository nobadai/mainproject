"use client";

/**
 * ML 모델 재학습 — 사람은 **한 번**만 누릅니다.
 *
 *     판정 -> 만들기 -> 견주기 -> 「모델 업데이트」 -> 바꾸기
 *      (배치가 밤새 여기까지 해 둔다)      ^ 사람
 *
 * ★ **탭은 늘 있고, 안에서 갈립니다.**
 *
 *       바꿀 것이 있으면   비교표 + 「모델 업데이트」 버튼
 *       바꿀 것이 없으면   «현재는 모델을 업데이트할 필요가 없습니다» 한 줄
 *
 *   새 모델이 지금 것보다 못하면 배치가 후보를 지우고 아무것도 안 남깁니다 —
 *   사람이 볼 것이 없습니다.
 *
 * ★ **판정을 화면이 다시 하지 않습니다.** 배치가 밤에 해 둔 결과를 읽어
 *   그릴 뿐입니다. 두 곳에서 재면 두 답이 갈리고, 그때 어느 쪽이 맞는지
 *   알 방법이 없습니다.
 *
 * ★ 이 흐름에 LLM 은 한 번도 안 나옵니다. 판정은 전부 규칙입니다.
 */

import { useCallback, useEffect, useState } from "react";

import { graphAct, MlError, retrainPending, type PendingRetrain } from "@/lib/mlConsole";
import { announceRetrainChanged, useRetrainChanged } from "@/lib/retrainSignal";

import { en, KIND } from "./labels";
import { VerifyTable } from "./VerifyTable";

/**
 * 지금 도는 모델 하나.
 *
 * ★ **이름은 바꿔도 그대로입니다.** `ops_auc` · `ops_whsl` · `ops_rtl` 은 모델을
 *   갈아 끼워도 안 바꿉니다 — 매입 시스템이 이 이름으로 정확히 찾기 때문에
 *   바꾸면 오류 없이 **0건**이 됩니다.
 *
 *   그래서 이름만으로는 «지금 무엇이 도는가» 를 알 수 없습니다. **만든 날**과
 *   **학습 끝**이 같이 있어야 가려집니다. 2026-09-15 저녁에 소매가 모델이
 *   바뀌었는데 화면에도 답에도 그 사실이 한 글자도 없었습니다.
 */
export interface CurrentModel {
  /** AUC · WHSL · RTL */
  kind: string;
  model_ver: string;
  /** 번들을 만든 날. `null` 이면 아직 못 읽었다는 뜻입니다 */
  created_at?: string | null;
  /** 이 모델이 배운 마지막 날. 교체 이력 표가 없으면 `null` 입니다 */
  train_end?: string | null;
  last_swapped_at?: string | null;
  last_swap_note?: string | null;
  /**
   * 교체 **시각**까지 아는가.
   *
   * ★ 되짚어 적은 기록은 백업 폴더 **이름**에서 날짜만 건진 것이 있어, 시각이
   *   `00:00` 으로 앉아 있습니다. 그대로 보이면 «한밤중에 바꿨나» 로 읽힙니다 —
   *   실제로는 **모릅니다.**
   *
   * 🔴 `null`·없음 은 «모른다» 가 아니라 **«아직 안 알려 준다»** 입니다.
   *   그때는 예전처럼 날짜와 시각을 그대로 보입니다.
   */
  last_swap_time_known?: boolean | null;
}

/**
 * 「현재 모델」 줄. **칸이 없으면 아예 안 그립니다.**
 *
 * ★ 백엔드가 `current_models` 를 아직 안 실어 줄 수 있습니다. 그때 빈 표를
 *   그리면 «모델이 없다» 로 읽힙니다 — 없는 것과 못 받은 것은 다릅니다.
 */
function CurrentModels({ rows }: { rows: CurrentModel[] }) {
  if (rows.length === 0) return null;
  return (
    <section
      className="flex flex-col gap-2 rounded-xl border bg-panel px-4 py-3.5"
      style={{ borderColor: "var(--color-hair)" }}
    >
      <h2 className="m-0 text-[17px] font-semibold">현재 모델</h2>
      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-[16px]">
          <thead>
            <tr style={{ color: "var(--color-mut2)" }}>
              {["가격", "이름", "만든 날", "학습 끝", "최근 업데이트"].map((h) => (
                <th key={h} className="whitespace-nowrap px-2 py-1.5 text-left font-medium">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.kind} style={{ borderTop: "1px solid var(--color-hair)" }}>
                <td className="whitespace-nowrap px-2 py-1.5">
                  {/*  백엔드는 대문자로 보내고 라벨표는 소문자 열쇠입니다. */}
                  {en(KIND, r.kind?.toLowerCase())}
                </td>
                <td className="tabular whitespace-nowrap px-2 py-1.5 font-mono">
                  {r.model_ver}
                </td>
                <td className="tabular whitespace-nowrap px-2 py-1.5">
                  {r.created_at ?? "—"}
                </td>
                <td className="tabular whitespace-nowrap px-2 py-1.5">
                  {r.train_end ?? "—"}
                </td>
                <td className="tabular px-2 py-1.5">
                  {/*  ★ «없다» 를 «—» 로만 적으면 왜 빈칸인지 모릅니다. */}
                  {r.last_swapped_at ? (
                    r.last_swap_time_known === false ? (
                      <>
                        {/*  시각을 모르면 **날짜만** 적습니다. 00:00 을 그대로
                            보이면 «한밤중에 바꿨나» 로 읽힙니다. */}
                        {r.last_swapped_at.slice(0, 10)}
                        <span className="ml-1.5" style={{ color: "var(--color-mut2)" }}>
                          시각 미상
                        </span>
                      </>
                    ) : (
                      r.last_swapped_at
                    )
                  ) : (
                    <span style={{ color: "var(--color-mut2)" }}>교체 이력 없음</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

/** 「언제 확인한 것인가」. 없으면 «아직 안 돌았다» 는 뜻입니다. */
function Checked({ at, ran }: { at: string | null; ran: boolean }) {
  return (
    <p className="m-0 text-[15.5px]" style={{ color: "var(--color-mut2)" }}>
      {ran && at ? (
        <>
          마지막 확인 <span className="tabular font-mono">{at}</span> · 매일 아침 자동으로
          모델을 점검합니다
        </>
      ) : (
        //  ★ 「아직 안 돌았다」 와 「돌았는데 없다」 는 다릅니다.
        //    앞은 고장일 수 있고 뒤는 정상입니다. 섞으면 안 됩니다.
        <>아직 한 번도 확인하지 않았습니다 — 배치가 돈 뒤에 채워집니다</>
      )}
    </p>
  );
}

/**
 * 재학습 응답. **`current_models` 는 아직 없을 수 있습니다** — 다른 일꾼이
 * 붙이는 중입니다. 없으면 「현재 모델」 줄을 안 그립니다.
 */
type PendingReply = Awaited<ReturnType<typeof retrainPending>> & {
  current_models?: CurrentModel[];
};

/**
 * @param currentModels 화면(`page.tsx`)이 이미 받아 둔 것이 있으면 넘겨 줍니다.
 *   안 넘기면 이 탭이 직접 받아 온 응답에서 찾습니다 — **두 번 묻지 않으려는**
 *   것이고, 어느 쪽으로 붙어도 돌게 두려는 것입니다.
 */
export function RetrainTab({ currentModels }: { currentModels?: CurrentModel[] } = {}) {
  const [rows, setRows] = useState<PendingRetrain[] | null>(null);
  const [models, setModels] = useState<CurrentModel[]>([]);
  const [at, setAt] = useState<string | null>(null);
  const [ran, setRan] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [done, setDone] = useState<string[]>([]);

  const say = (e: unknown) =>
    e instanceof MlError ? `[${e.status || "연결 안 됨"}] ${e.message}` : String(e);

  const load = useCallback(async () => {
    try {
      const r = (await retrainPending()) as PendingReply;
      setRows(r.pending);
      setModels(r.current_models ?? []);
      setAt(r.at);
      setRan(r.ran);
      setErr(null);
    } catch (e) {
      setErr(say(e));
    }
  }, []);

  useEffect(() => {
    let alive = true;
    retrainPending()
      .then((raw) => {
        if (!alive) return;
        const r = raw as PendingReply;
        setRows(r.pending);
        setModels(r.current_models ?? []);
        setAt(r.at);
        setRan(r.ran);
      })
      .catch((e: unknown) => {
        if (alive) setErr(say(e));
      });
    return () => {
      alive = false;
    };
  }, []);

  //  ★ **어디서 바꿨든 여기도 다시 받습니다.** 채팅 서랍에서 바꾼 경우가
  //    그렇습니다 — 그쪽은 이 탭의 존재를 모릅니다 (`lib/retrainSignal.ts`).
  useRetrainChanged(() => void load());

  const update = async (kind: string) => {
    const label = en(KIND, kind);
    const ok = window.confirm(
      [
        `지금 사용 중인 ${label} 모델을 새 모델로 바꿉니다.`,
        "",
        "- 현재 사용 중인 모델은 그대로 전체 백업됩니다",
        "- 모델 이름은 절대 바뀌지 않습니다 — 매입 시스템이 이 이름으로 찾습니다",
        "",
        "정말 바꿀까요?",
      ].join("\n"),
    );
    if (!ok) return;
    setBusy(kind);
    try {
      await graphAct(kind as "auc" | "whsl" | "rtl", "apply");
      setDone((d) => [...d, kind]);
      //   ★ **채팅에서 누른 것과 똑같이 알립니다.** 위의 `useRetrainChanged` 가
      //     받아 이 탭을 다시 받고(전에는 여기서 `load()` 를 직접 불렀습니다),
      //     같은 신호로 **페이지의 빨간 배지**도 같이 내려갑니다. 전에는 이
      //     길로 바꿔도 배지가 안 사라졌습니다 — 길이 둘인데 갱신이 하나뿐
      //     이면 안 됩니다.
      announceRetrainChanged(kind);
    } catch (e) {
      setErr(say(e));
    } finally {
      setBusy(null);
    }
  };

  //  ★ 화면이 넘겨준 것이 있으면 그것을 씁니다 — 같은 것을 두 번 묻지 않습니다.
  const shown = currentModels ?? models;

  if (err)
    return (
      <div className="flex flex-col gap-4">
        {/*  ★ 후보를 못 물어봤어도 «지금 무엇이 도는가» 는 보여야 합니다.
               둘은 다른 질문이고, 하나가 막혔다고 나머지를 지우지 않습니다. */}
        <CurrentModels rows={shown} />
        <p
          className="m-0 rounded-lg px-4 py-3.5 text-[16.5px]"
          style={{ background: "var(--color-t-bad-bg)", color: "var(--color-t-bad)" }}
        >
          {err}
        </p>
      </div>
    );
  if (!rows)
    return (
      <p className="m-0 py-10 text-center text-[16.5px]" style={{ color: "var(--color-mut2)" }}>
        확인하는 중…
      </p>
    );

  const waiting = rows.filter((r) => !done.includes(r.kind));

  return (
    <div className="flex flex-col gap-4">
      <CurrentModels rows={shown} />

      {done.length > 0 && (
        <p
          className="m-0 rounded-lg px-3.5 py-2.5 text-[16.5px] leading-relaxed"
          style={{ background: "var(--color-t-good-bg)", color: "var(--color-t-good)" }}
        >
          <b>{done.map((k) => en(KIND, k)).join(" · ")} 모델을 바꿨습니다.</b> 지금 것은 통째로
          백업해 뒀습니다. 내일 아침 배치부터 새 모델로 예측합니다.
        </p>
      )}

      {waiting.length === 0 ? (
        <section
          className="flex flex-col items-center gap-2.5 rounded-xl border bg-panel px-4 py-12 text-center"
          style={{ borderColor: "var(--color-hair)" }}
        >
          <p className="m-0 text-[18px] font-semibold">
            현재는 모델을 업데이트할 필요가 없습니다.
          </p>
          <Checked at={at} ran={ran} />
        </section>
      ) : (
        <>
          <p
            className="m-0 rounded-lg px-3.5 py-2.5 text-[16.5px] leading-relaxed"
            style={{ background: "var(--color-t-warn-bg)", color: "var(--color-t-warn)" }}
          >
            ★ <b>새로 학습한 모델이 지금 모델보다 낫습니다.</b> 아래 숫자를 보시고 바꿀지
            정해 주세요. 만들고 견주는 것까지는 이미 끝나 있습니다.
          </p>

          {waiting.map((r) => (
            <section
              key={r.kind}
              className="flex flex-col gap-3.5 rounded-xl border bg-panel p-4"
              style={{ borderColor: "var(--color-hair)" }}
            >
              <header className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <h2 className="m-0 text-[17.5px] font-semibold">{en(KIND, r.kind)}</h2>
                {r.candidate && (
                  <span className="font-mono text-[15.5px]" style={{ color: "var(--color-mut2)" }}>
                    새 모델 {r.candidate}
                  </span>
                )}
              </header>

              {/*  ★ 표를 **버튼보다 먼저** 보입니다. 「바꿀까요」 에 답하려면
                     숫자를 나란히 봐야 합니다. 글만 주면 «낫습니다» 를 믿고
                     누르는 것 말고 할 수 있는 게 없습니다. */}
              <VerifyTable items={r.items ?? []} />

              <div className="flex flex-wrap items-center gap-2.5">
                <button
                  type="button"
                  onClick={() => void update(r.kind)}
                  disabled={busy !== null}
                  className="rounded-lg px-3.5 py-2 text-[17px] font-semibold transition disabled:cursor-not-allowed disabled:opacity-40"
                  style={{ background: "var(--color-nav)", color: "#f4f3ee" }}
                >
                  {busy === r.kind ? "바꾸는 중…" : "모델 업데이트"}
                </button>
                <span className="text-[15.5px]" style={{ color: "var(--color-mut)" }}>
                  지금 것은 통째로 백업되고 되돌릴 수 있습니다 · 모델 이름은 안 바뀝니다
                </span>
              </div>
            </section>
          ))}

          <Checked at={at} ran={ran} />
        </>
      )}
    </div>
  );
}
