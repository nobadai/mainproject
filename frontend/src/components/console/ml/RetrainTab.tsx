"use client";

/**
 * ML 모델 재학습 — 사람은 **한 번**만 누른다 (2026-09-09 바꿈).
 *
 *     판정 → 만들기 → 견주기 → 「모델 업데이트」 → 바꾸기
 *      (배치가 밤새 여기까지 해 둔다)      ↑ 사람
 *
 * ★ **후보가 현행보다 나을 때만 이 탭이 보입니다.** 못하면 배치가 후보를
 *   지우고 아무것도 안 남깁니다 — 사람이 볼 것이 없습니다.
 *
 * ★ **검증을 통과 못 하면 두 번째 물음이 아예 안 나옵니다.** 버튼을 띄워 두고
 *   막는 것보다, 물음을 안 내는 편이 낫습니다.
 *
 * ★ **지금은 버튼이 모두에게 보입니다.** 승인권자를 아직 안 만들어 놔서입니다
 *   (2026-09-09). 자리는 남겨 뒀습니다 — `canApprove` 를 `false` 로 넘기면
 *   버튼이 사라지고 **상태와 수치는 그대로 보입니다.** 가리면 무슨 일이
 *   도는지 모르니, 가리는 것은 «누를 수 있나» 뿐입니다.
 *
 * ★ 노드 하나도 LLM 을 안 부릅니다. 여기서 상태 기계는 **순서를 지키는 장치**이고
 *   판정은 전부 규칙입니다.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import {
  graphAct,
  graphStatus,
  MlError,
  type GraphStatus,
  type TargetKind,
} from "@/lib/mlConsole";

import { askEn } from "./labels";
import { Verdict } from "./Report";
import { VerifyTable } from "./VerifyTable";

const KIND_LABEL: Record<string, string> = {
  auc: "경락가",
  whsl: "중도매가",
  rtl: "소매가",
};

/** 노드 이름 → 사람 말. 화면에 raw 노드명을 그대로 보이지 않는다. */
const NODE_LABEL: Record<string, string> = {
  judge: "판단 중",
  ask_build: "후보 모델을 만들까요?",
  build: "후보 만드는 중",
  verify: "성능 비교 중",
  ask_apply: "새 모델로 바꿀까요?",
  apply: "교체하는 중",
};

function Button({
  onClick,
  disabled,
  tone = "plain",
  children,
}: {
  onClick: () => void;
  disabled?: boolean;
  tone?: "plain" | "primary" | "warn";
  children: React.ReactNode;
}) {
  const skin =
    tone === "primary"
      ? { background: "var(--color-nav)", color: "#f4f3ee", border: "1px solid transparent" }
      : tone === "warn"
        ? {
            background: "transparent",
            color: "var(--color-t-warn)",
            border: "1px solid var(--color-t-warn)",
          }
        : {
            background: "var(--color-panel)",
            color: "var(--color-ink2)",
            border: "1px solid var(--color-hair)",
          };
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="rounded-lg px-3 py-1.5 text-[12.5px] font-semibold transition disabled:cursor-not-allowed disabled:opacity-40"
      style={skin}
    >
      {children}
    </button>
  );
}

//  ★ 승인권자가 아직 없어 기본이 **열림**입니다. 역할이 생기면 여기로
//    `canApprove={CAN[session.role].approve}` 를 넘기면 됩니다.
export function RetrainTab({ canApprove = true }: { canApprove?: boolean }) {
  const [kind, setKind] = useState<TargetKind>("auc");
  const [st, setSt] = useState<GraphStatus | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    try {
      setSt(await graphStatus(kind));
      setErr(null);
    } catch (e) {
      setErr(e instanceof MlError ? `[${e.status || "연결 안 됨"}] ${e.message}` : String(e));
    }
  }, [kind]);

  //  ★ 처음 열 때 · 가격 종류를 바꿀 때. **효과 안에서 곧바로 상태를 건드리지
  //    않으려고** `load()` 를 부르지 않고 여기서 직접 물어봅니다.
  useEffect(() => {
    let alive = true;
    graphStatus(kind)
      .then((r) => {
        if (!alive) return;
        setSt(r);
        setErr(null);
      })
      .catch((e: unknown) => {
        if (!alive) return;
        setErr(e instanceof MlError ? `[${e.status || "연결 안 됨"}] ${e.message}` : String(e));
      });
    return () => {
      alive = false;
    };
  }, [kind]);

  //  돌고 있을 때만 물어본다. 끝나면 멈춘다 — 계속 물으면 서버가 논다.
  useEffect(() => {
    if (!st?.running.busy) {
      if (timer.current) clearInterval(timer.current);
      timer.current = null;
      return;
    }
    timer.current = setInterval(() => {
      void load();
    }, 3000);
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, [st?.running.busy, load]);

  const act = async (answer?: "build" | "apply" | "stop", confirmText?: string) => {
    if (confirmText && !window.confirm(confirmText)) return;
    setBusy(true);
    try {
      await graphAct(kind, answer);
      await load();
    } catch (e) {
      setErr(e instanceof MlError ? `[${e.status || "연결 안 됨"}] ${e.message}` : String(e));
    } finally {
      setBusy(false);
    }
  };

  const label = KIND_LABEL[kind] ?? kind;
  const running = st?.running.busy ?? false;
  const asking = st?.asking ?? null;
  const values = (st?.values ?? {}) as Record<string, unknown>;
  const done = !running && !asking && (st?.next.length ?? 0) === 0;
  const where = running
    ? st?.next.map((n) => NODE_LABEL[n] ?? n).join(" · ") || "실행 중"
    : asking
      ? askEn(asking.ask)
      : done && Object.keys(values).length > 0
        ? "완료"
        : "준비됨";

  return (
    <div className="flex flex-col gap-4">
      <section
        className="flex flex-col gap-3.5 rounded-xl border bg-panel p-4"
        style={{ borderColor: "var(--color-hair)" }}
      >
        <header className="flex flex-wrap items-center gap-2">
          {st && <Verdict level={(values.verdict as string) ?? "OK"} />}
          <h2 className="m-0 text-[13.5px] font-semibold">{label} — 모델 재학습 순서</h2>
          <span className="text-[11.5px]" style={{ color: "var(--color-mut)" }}>
            {where}
          </span>

          <span className="ml-auto flex flex-wrap items-center gap-1.5">
            {(["auc", "whsl", "rtl"] as const).map((k) => (
              <button
                key={k}
                type="button"
                onClick={() => setKind(k)}
                aria-pressed={k === kind}
                className="rounded-md px-2.5 py-1 text-[11.5px] font-medium transition"
                style={
                  k === kind
                    ? { background: "var(--color-nav)", color: "#f4f3ee" }
                    : { background: "var(--color-sunk)", color: "var(--color-mut)" }
                }
              >
                {KIND_LABEL[k]}
              </button>
            ))}
          </span>
        </header>

        <p className="m-0 text-[11.5px] leading-relaxed" style={{ color: "var(--color-mut)" }}>
          <b style={{ color: "var(--color-ink)" }}>새 모델을 만들고 지금 모델과 견주는 것까지</b>{" "}
          자동으로 끝나 있습니다. 아래 표가 그 결과입니다.
          <br />★{" "}
          <b style={{ color: "var(--color-ink)" }}>
            이 화면은 새 모델이 지금 모델보다 나을 때만 보입니다.
          </b>{" "}
          못하면 새 모델을 지우고 아무것도 알리지 않습니다. 사람이 누를 자리는{" "}
          <b style={{ color: "var(--color-ink)" }}>&laquo;모델 업데이트&raquo; 하나</b>뿐입니다.
        </p>

        {/*  ★ 지금은 모두에게 보입니다 — 승인권자가 아직 없습니다. */}
        {canApprove ? (
          <div className="flex flex-wrap gap-2">
            {/*  ★ 「판단하기」 와 「후보 만들기」 버튼을 없앴습니다 (2026-09-09).
                   둘 다 배치가 밤새 해 둡니다. 사람이 누를 자리는 하나입니다.

                   ★ 대조는 백엔드가 보내는 **한글** 로 합니다. 영어로 바꾸면
                     안 걸립니다. */}
            {asking?.ask?.includes("바꿀까요") && (
              <Button
                onClick={() =>
                  void act(
                    "apply",
                    `지금 사용 중인 ${label} 모델을 새로 만든 후보 모델로 바꿉니다.\n\n` +
                      "- 현재 사용 중인 모델은 그대로 전체 백업됩니다\n" +
                      "- 모델 이름은 절대 바뀌지 않습니다 — 매입 시스템이 이 정확한 이름으로 모델을 찾아서 씁니다\n\n정말 바꿀까요?",
                  )
                }
                disabled={busy}
                tone="primary"
              >
                모델 업데이트
              </Button>
            )}
            {asking && (
              <Button onClick={() => void act("stop")} disabled={busy}>
                중단하기
              </Button>
            )}

          </div>
        ) : (
          <p
            className="m-0 rounded-lg px-3.5 py-2.5 text-[12px] leading-relaxed"
            style={{ background: "var(--color-t-info-bg)", color: "var(--color-t-info)" }}
          >
            <b>모델 교체는 권한이 있는 승인자만 할 수 있습니다.</b> 현재 상태와 수치는 모든
            사람이 진행 상황을 알 수 있도록 모두에게 공개됩니다.
          </p>
        )}

        {err && (
          <p
            className="m-0 rounded-lg px-3.5 py-2.5 text-[12px]"
            style={{ background: "var(--color-t-bad-bg)", color: "var(--color-t-bad)" }}
          >
            {err}
          </p>
        )}

        {!st && !err && (
          <p className="m-0 text-[12.5px]" style={{ color: "var(--color-mut2)" }}>
            진행 상태를 불러오는 중…
          </p>
        )}

        {/* 지금 묻고 있는 것 */}
        {asking && (
          <div
            className="rounded-lg p-3.5"
            style={{ background: "var(--color-t-warn-bg)", color: "var(--color-t-warn)" }}
          >
            <p className="m-0 text-[13.5px] font-semibold">★ {asking.ask}</p>
            {asking.hint && (
              <p className="m-0 mt-1.5 text-[12px] leading-relaxed">{asking.hint}</p>
            )}
            {/*  ★ 표를 **글보다 먼저** 보인다. 「바꿀까요」 에 답하려면 숫자를
                   나란히 봐야 한다. */}
            {asking.items && asking.items.length > 0 && (
              <div className="mt-3">
                <VerifyTable items={asking.items} />
              </div>
            )}
          </div>
        )}

        {/* 묻는 중이 아니어도 마지막 검증 표는 남긴다 — 「왜 안 바꿨나」 를 되짚는다 */}
        {!asking && st?.verify_items && st.verify_items.length > 0 && (
          <VerifyTable items={st.verify_items} />
        )}
        {!asking && !(st?.verify_items?.length) && st?.last_verify && (
          <section className="flex flex-col gap-2">
            <p
              className="m-0 flex flex-wrap items-baseline gap-x-2 text-[12px]"
              style={{ color: "var(--color-mut)" }}
            >
              <b style={{ color: "var(--color-ink)" }}>최근 검증 결과</b>
              <span className="tabular font-mono text-[11.5px]">{st.last_verify.at}</span>
              <span className="text-[11.5px]">
                후보 모델 <span className="font-mono">{st.last_verify.candidate}</span> · 비교 기간{" "}
                {st.last_verify.eval_from}
              </span>
              <span
                className="rounded px-2 py-0.5 text-[11px] font-semibold"
                style={
                  st.last_verify.passed
                    ? { background: "var(--color-t-good-bg)", color: "var(--color-t-good)" }
                    : { background: "var(--color-t-warn-bg)", color: "var(--color-t-warn)" }
                }
              >
                {st.last_verify.passed ? "통과" : "안 바꿈"}
              </span>
            </p>
            <VerifyTable items={st.last_verify.items} />
          </section>
        )}

        {/* 돌고 있을 때의 기록 — 실패해도 남는다 */}
        {st?.build_tail && (
          <pre
            className="thin-scroll m-0 max-h-56 overflow-auto rounded-lg border p-3 text-[11px] leading-relaxed"
            style={{ borderColor: "var(--color-hair)", background: "var(--color-sunk)" }}
          >
            {st.build_tail}
          </pre>
        )}
      </section>
    </div>
  );
}
