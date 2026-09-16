"use client";

import { useRef, useState } from "react";

import { ApiError, runReport } from "@/lib/api";
import { userErrorText } from "@/lib/procurementLabels";
import { FinanceReport } from "@/components/reports/FinanceReport";
import { SalesReport } from "@/components/reports/SalesReport";
import { exportReportPdf } from "@/components/reports/reportExport";
import { filename, type ReportFacts } from "@/components/reports/reportFormat";

/**
 * 매입안 보고서 내려받기.
 *
 * ★ **화면이 문서를 조립하지 않는다.** 서버가 낸 Markdown 을 그대로 파일로 만든다 —
 *   화면이 조립하기 시작하면 **화면과 문서가 다른 숫자**를 말하게 된다.
 *
 * ★ **문서는 실제 서비스 사용자가 읽는 매입 제안이다** (2026-09-15 결정).
 *   안별 매입량·금액·등급·이유와 부서 검토만 사람 말로 싣고, 검증 기록·종료 코드·
 *   입력 출처·참조 번호 같은 개발용 정보는 넣지 않는다. 그 기록은 실행 이력에 남는다.
 *   무엇을 싣는지는 서버 `backend/app/master/report.py` 가 정한다.
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
      setError(
        userErrorText(
          e instanceof ApiError ? e.status : null,
          e instanceof Error ? e.message : "",
          "보고서를 내려받지 못했습니다. 잠시 뒤 다시 시도해 주세요.",
        ),
      );
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


export function DomainReportPreview({
  kind,
  facts,
}: {
  kind: "FINANCE" | "SALES";
  facts: ReportFacts;
}) {
  const root = useRef<HTMLDivElement>(null);
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function downloadPdf() {
    if (exporting || !root.current) return;
    setExporting(true);
    setError(null);
    try {
      await exportReportPdf({
        root: root.current,
        filename: filename(kind === "FINANCE" ? "finance" : "sales", facts),
      });
    } catch {
      setError("보고서를 PDF로 만들지 못했습니다.");
    } finally {
      setExporting(false);
    }
  }

  return (
    <>
      <div ref={root} className="domain-report-print overflow-auto rounded-xl bg-[#cdd6d1] p-4">
        {kind === "FINANCE" ? <FinanceReport facts={facts} /> : <SalesReport facts={facts} />}
      </div>
      <div className="domain-report-controls mt-2 flex gap-2">
        <button type="button" onClick={downloadPdf} disabled={exporting} className="rounded-lg border border-line bg-sunk px-3 py-1.5 text-[12.5px] text-muted hover:border-accent hover:text-accent-ink disabled:opacity-45">
          {exporting ? "PDF 만드는 중…" : "PDF 다운로드"}
        </button>
        <button type="button" onClick={() => window.print()} className="rounded-lg border border-line bg-sunk px-3 py-1.5 text-[12.5px] text-muted hover:border-accent hover:text-accent-ink">
          인쇄
        </button>
        {error && <span className="self-center text-[12px] text-warn">{error}</span>}
      </div>
      <style>{`
        .report-page { width: 297mm; min-height: 210mm; box-sizing: border-box; display: flex; flex-direction: column; break-after: page; page-break-after: always; }
        .report-page:last-child { break-after: auto; page-break-after: auto; }
        @media print {
          body * { visibility: hidden !important; }
          .domain-report-print, .domain-report-print * { visibility: visible !important; }
          .domain-report-print { position: absolute !important; left: 0 !important; top: 0 !important; width: 297mm !important; padding: 0 !important; background: #fff !important; }
          .domain-report-controls { display: none !important; }
          @page { size: A4 landscape; margin: 0; }
        }
      `}</style>
    </>
  );
}
