"use client";

/**
 * 답변 글 안에 실려 온 **버튼**.
 *
 * ★ **왜 글 안에 싣나.** 채팅까지 살아서 가는 것은 `answer_markdown` 한 덩어리
 *   뿐입니다 (`backend/app/master/answer.py` 의 `_MARKDOWN_AGENTS`). payload 의
 *   나머지 칸은 마스터가 *사실 줄*로 펴 버려서, 「여기에 버튼을 놓아라」 같은
 *   **구조**가 거품 안까지 못 들어옵니다. 그래서 글 안에 특별한 링크로 싣고
 *   (`[모델 업데이트 — 소매가](action:retrain-apply?kind=rtl)`) 화면이 그것만
 *   버튼으로 그립니다.
 *
 * ★ **새 길을 안 만듭니다.** 누르면 「모델 재학습」 탭이 쓰는 그 함수가 그대로
 *   돕니다 — `lib/mlConsole.ts` 의 `graphAct(kind, "apply")`. 같은 승인 단계를
 *   밟고 같은 백업을 남깁니다. 여기에만 있는 지름길을 내면 한쪽만 고쳐질
 *   자리가 생깁니다.
 *
 * ★ **모르는 동작은 버튼이 아닙니다.** `action:` 으로 시작하는데 우리가 아는
 *   것이 아니면 **글자 그대로** 보여 줍니다. 누르면 무슨 일이 나는지 모르는
 *   버튼을 그리지 않습니다.
 *
 * 🔴 **두 번 눌리지 않습니다.** 누르는 순간 잠그고, 끝나면 버튼 자리에 결과를
 *   적습니다. 모델 교체는 되돌릴 수 있지만 **두 번 도는 것**은 다른 이야기입니다.
 */

import { useState } from "react";

import { graphAct, MlError, type TargetKind } from "@/lib/mlConsole";
import { announceRetrainChanged } from "@/lib/retrainSignal";

/** 코드 이름을 사람 말로. **여기 없는 값은 버튼을 안 그립니다.** */
const KIND_LABEL: Record<string, string> = {
  auc: "경락가",
  whsl: "중도매가",
  rtl: "소매가",
};

/** 우리가 아는 동작. 지금은 하나뿐입니다. */
const APPLY = "retrain-apply";

/** `retrain-apply?kind=rtl` 을 `{ what, kind }` 로. 모르는 모양이면 `null`. */
function parse(href: string): { kind: TargetKind } | null {
  const [what, query = ""] = href.split("?", 2);
  if (what !== APPLY) return null;
  const kind = new URLSearchParams(query).get("kind") ?? "";
  if (!(kind in KIND_LABEL)) return null;
  return { kind: kind as TargetKind };
}

/** 오류를 사람이 읽을 한 줄로. **원문을 지우지 않습니다.** */
function say(error: unknown): string {
  if (error instanceof MlError) return `${error.message} (${error.status})`;
  return error instanceof Error ? error.message : String(error);
}

/** 「교체됨 · 14:32」 의 시각. */
function now(): string {
  return new Date().toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" });
}

export function ActionButton({ href, label }: { href: string; label: string }) {
  const parsed = parse(href);
  //  ★ 훅은 언제나 같은 수만큼 부릅니다 — 모르는 동작이라도 먼저 부르고
  //    나중에 가릅니다 (조건부 훅 금지).
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  //  모르는 동작은 **글자 그대로**. 버튼으로 속이지 않습니다
  if (!parsed) return <>{label}</>;

  const { kind } = parsed;
  const name = KIND_LABEL[kind];

  if (result)
    return (
      <span
        className="rounded-lg px-2.5 py-1 text-[12px] font-semibold"
        style={{
          background: result.ok ? "var(--color-t-good-bg)" : "var(--color-t-bad-bg)",
          color: result.ok ? "var(--color-t-good)" : "var(--color-t-bad)",
        }}
      >
        {result.text}
      </span>
    );

  const run = async () => {
    //  🔴 잠금을 **묻기 전에** 겁니다. 확인창이 떠 있는 동안에도 두 번째 손이
    //     못 들어옵니다.
    if (busy) return;
    setBusy(true);
    try {
      const ok = window.confirm(
        [
          `${name} 모델을 후보로 교체합니다. 되돌릴 수 있습니다.`,
          "",
          "- 지금 쓰는 모델은 통째로 백업됩니다",
          "- 모델 이름은 안 바뀝니다 — 매입 시스템이 이 이름으로 찾습니다",
          "",
          "정말 바꿀까요?",
        ].join("\n"),
      );
      if (!ok) return;
      await graphAct(kind, "apply");
      setResult({ ok: true, text: `교체됨 · ${now()}` });
      //  ★ **여기가 채팅 바깥을 고쳐 주는 유일한 자리입니다.** 채팅은 화면
      //    (`console/forecast/page.tsx`) 바깥에 있어서, 알려 주지 않으면 탭의
      //    빨간 배지가 새로고침 전까지 그대로 남습니다 — 실제로 그랬습니다.
      announceRetrainChanged(kind);
    } catch (error) {
      //  ★ 오류는 **원문 그대로** 남깁니다. 「실패했습니다」 로 뭉개면 무엇이
      //    막혔는지(콘솔이 안 떴나 · 승인 단계가 지났나)를 아무도 못 봅니다.
      setResult({ ok: false, text: say(error) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <button
      type="button"
      onClick={() => void run()}
      disabled={busy}
      className="rounded-lg px-3 py-1.5 text-[12.5px] font-semibold transition disabled:cursor-not-allowed disabled:opacity-40"
      style={{ background: "var(--color-nav)", color: "#f4f3ee" }}
    >
      {busy ? "바꾸는 중…" : label}
    </button>
  );
}
