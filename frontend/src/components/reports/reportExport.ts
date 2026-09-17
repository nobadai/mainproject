import html2canvas from "html2canvas";
import { jsPDF } from "jspdf";

export async function exportReportPdf({ root, filename }: { root: HTMLElement; filename: string }) {
  await new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
  const target = root.getBoundingClientRect();
  if (target.width <= 0 || target.height <= 0) {
    console.error("[report-pdf] capture blocked: zero-sized report target", target);
    throw new Error("report capture target has no layout");
  }
  const pages = Array.from(root.querySelectorAll<HTMLElement>("[data-report-page]"));
  if (!pages.length) throw new Error("report pages are unavailable");
  const pdf = new jsPDF({ orientation: "landscape", unit: "mm", format: "a4" });
  const width = pdf.internal.pageSize.getWidth();
  const height = pdf.internal.pageSize.getHeight();
  for (const [index, page] of pages.entries()) {
    const pageRect = page.getBoundingClientRect();
    if (pageRect.width <= 0 || pageRect.height <= 0) {
      console.error("[report-pdf] capture blocked: zero-sized report page", { index, pageRect });
      throw new Error("report page has no layout");
    }
    const canvas = await html2canvas(page, { scale: 2, useCORS: true, backgroundColor: "#ffffff" });
    if (canvas.width <= 0 || canvas.height <= 0) {
      console.error("[report-pdf] canvas was empty", { index, width: canvas.width, height: canvas.height });
      throw new Error("report canvas is empty");
    }
    if (index) pdf.addPage("a4", "landscape");
    pdf.addImage(canvas.toDataURL("image/png"), "PNG", 0, 0, width, height, undefined, "FAST");
  }
  const blob = pdf.output("blob");
  if (!(blob instanceof Blob) || blob.size <= 0 || blob.type !== "application/pdf") {
    console.error("[report-pdf] invalid PDF blob", { size: blob.size, type: blob.type });
    throw new Error("PDF 생성 결과가 유효하지 않습니다.");
  }
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.style.display = "none";
  document.body.appendChild(link);
  link.click();
  link.remove();
  console.debug("[report-pdf] download triggered", { filename, bytes: blob.size });
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
