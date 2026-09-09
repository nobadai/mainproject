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

import { en, KIND } from "./labels";
import { VerifyTable } from "./VerifyTable";

/** 「언제 확인한 것인가」. 없으면 «아직 안 돌았다» 는 뜻입니다. */
function Checked({ at, ran }: { at: string | null; ran: boolean }) {
  return (
    <p className="m-0 text-[11.5px]" style={{ color: "var(--color-mut2)" }}>
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

export function RetrainTab() {
  const [rows, setRows] = useState<PendingRetrain[] | null>(null);
  const [at, setAt] = useState<string | null>(null);
  const [ran, setRan] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [done, setDone] = useState<string[]>([]);

  const say = (e: unknown) =>
    e instanceof MlError ? `[${e.status || "연결 안 됨"}] ${e.message}` : String(e);

  const load = useCallback(async () => {
    try {
      const r = await retrainPending();
      setRows(r.pending);
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
      .then((r) => {
        if (!alive) return;
        setRows(r.pending);
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
      await load();
    } catch (e) {
      setErr(say(e));
    } finally {
      setBusy(null);
    }
  };

  if (err)
    return (
      <p
        className="m-0 rounded-lg px-4 py-3.5 text-[12.5px]"
        style={{ background: "var(--color-t-bad-bg)", color: "var(--color-t-bad)" }}
      >
        {err}
      </p>
    );
  if (!rows)
    return (
      <p className="m-0 py-10 text-center text-[12.5px]" style={{ color: "var(--color-mut2)" }}>
        확인하는 중…
      </p>
    );

  const waiting = rows.filter((r) => !done.includes(r.kind));

  return (
    <div className="flex flex-col gap-4">
      {done.length > 0 && (
        <p
          className="m-0 rounded-lg px-3.5 py-2.5 text-[12.5px] leading-relaxed"
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
          <p className="m-0 text-[14px] font-semibold">
            현재는 모델을 업데이트할 필요가 없습니다.
          </p>
          <Checked at={at} ran={ran} />
        </section>
      ) : (
        <>
          <p
            className="m-0 rounded-lg px-3.5 py-2.5 text-[12.5px] leading-relaxed"
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
                <h2 className="m-0 text-[13.5px] font-semibold">{en(KIND, r.kind)}</h2>
                {r.candidate && (
                  <span className="font-mono text-[11.5px]" style={{ color: "var(--color-mut2)" }}>
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
                  className="rounded-lg px-3.5 py-2 text-[13px] font-semibold transition disabled:cursor-not-allowed disabled:opacity-40"
                  style={{ background: "var(--color-nav)", color: "#f4f3ee" }}
                >
                  {busy === r.kind ? "바꾸는 중…" : "모델 업데이트"}
                </button>
                <span className="text-[11.5px]" style={{ color: "var(--color-mut)" }}>
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
