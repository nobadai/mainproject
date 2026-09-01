"use client";

import { useState } from "react";

import { ApiError, runReport } from "@/lib/api";

/**
 * 매입안 보고서 내려받기.
 *
 * ★ **화면이 문서를 조립하지 않는다.** 서버가 낸 Markdown 을 그대로 파일로 만든다 —
 *   화면이 조립하기 시작하면 **화면과 문서가 다른 숫자**를 말하게 된다.
 *
 * 🔴 **문서에는 못 한 것도 들어간다.** 지적·확인 필요·못 돈 검사·입력 출처가 안
 *   옆에 있어야 들고 나간 사람이 그 숫자를 어떻게 읽어야 하는지 안다.
 *   **결론만 담은 문서가 가장 위험하다** — 읽는 사람은 그것을 확정으로 읽는다.
 */
export function ReportDownload({ requestId }: { requestId: string }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function download() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const report = await runReport(requestId);
      const blob = new Blob([report.markdown], {
        type: "text/markdown;charset=utf-8",
      });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = report.filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      // 브라우저가 저장을 시작한 뒤에 푼다 — 바로 풀면 파일이 비는 브라우저가 있다.
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) {
      // 🔴 서버 문장을 덮지 않는다
      setError(e instanceof ApiError ? `[${e.status}] ${e.message}` : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      <button
        type="button"
        onClick={download}
        disabled={busy}
        className="rounded-lg border border-line bg-sunk px-3 py-1.5 text-[12.5px] text-muted hover:border-accent hover:text-accent-ink disabled:opacity-45"
      >
        {busy ? "만드는 중…" : "↓ 보고서 내려받기 (.md)"}
      </button>
      {error && <span className="text-[12px] text-warn">{error}</span>}
    </div>
  );
}
