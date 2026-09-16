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
  pdf.save(filename);
}
