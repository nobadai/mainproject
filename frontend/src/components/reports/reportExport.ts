import html2canvas from "html2canvas";
import { jsPDF } from "jspdf";

export async function exportReportPdf({ root, filename }: { root: HTMLElement; filename: string }) {
  await new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
  const pages = Array.from(root.querySelectorAll<HTMLElement>("[data-report-page]"));
  if (!pages.length) throw new Error("report pages are unavailable");
  const pdf = new jsPDF({ orientation: "landscape", unit: "mm", format: "a4" });
  const width = pdf.internal.pageSize.getWidth();
  const height = pdf.internal.pageSize.getHeight();
  for (const [index, page] of pages.entries()) {
    const canvas = await html2canvas(page, { scale: 2, useCORS: true, backgroundColor: "#ffffff" });
    if (index) pdf.addPage("a4", "landscape");
    pdf.addImage(canvas.toDataURL("image/png"), "PNG", 0, 0, width, height, undefined, "FAST");
  }
  // Use the same explicit Blob lifecycle as the rest of the console downloads.
  // `pdf.save()` is browser-dependent and made the Finance report appear complete
  // while failing to start a download in the embedded console.
  const bytes = pdf.output("arraybuffer");
  const header = new TextDecoder().decode(bytes.slice(0, 4));
  if (header !== "%PDF") throw new Error("PDF 생성 결과가 유효하지 않습니다.");
  const blob = new Blob([bytes], { type: "application/pdf" });
  if (blob.size === 0) throw new Error("PDF 생성 결과가 비어 있습니다.");
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
