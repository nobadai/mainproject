import html2canvas from "html2canvas";
import { jsPDF } from "jspdf";

export async function createReportPdfBlob({ root }: { root: HTMLElement }): Promise<Blob> {
  let stage = "02 target found";
  try {
    await new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
    stage = "03 target measured";
    const target = root.getBoundingClientRect();
    console.debug("[report-pdf] 03 target measured", {
      width: target.width,
      height: target.height,
      scrollWidth: root.scrollWidth,
      scrollHeight: root.scrollHeight,
    });
    if (target.width <= 0 || target.height <= 0 || root.scrollWidth <= 0 || root.scrollHeight <= 0) {
      throw new Error("report capture target has no layout");
    }
    const pages = Array.from(root.querySelectorAll<HTMLElement>("[data-report-page]"));
    if (!pages.length) throw new Error("report pages are unavailable");
    const pdf = new jsPDF({ orientation: "landscape", unit: "mm", format: "a4" });
    const width = pdf.internal.pageSize.getWidth();
    const height = pdf.internal.pageSize.getHeight();
    for (const [index, page] of pages.entries()) {
      stage = "04 canvas created";
      const pageRect = page.getBoundingClientRect();
      if (pageRect.width <= 0 || pageRect.height <= 0) {
        throw new Error("report page has no layout");
      }
      const canvas = await html2canvas(page, { scale: 2, useCORS: true, backgroundColor: "#ffffff" });
      if (canvas.width <= 0 || canvas.height <= 0) throw new Error("report canvas is empty");
      const image = canvas.toDataURL("image/png");
      if (!image.startsWith("data:image/png")) throw new Error("report canvas is not a PNG");
      console.debug("[report-pdf] 04 canvas created", { index, width: canvas.width, height: canvas.height });
      if (index) pdf.addPage("a4", "landscape");
      pdf.addImage(image, "PNG", 0, 0, width, height, undefined, "FAST");
    }
    stage = "05 jsPDF rendered";
    console.debug("[report-pdf] 05 jsPDF rendered", { pages: pages.length });
    stage = "06 blob created";
    const blob = pdf.output("blob");
    console.debug("[report-pdf] 06 blob created", { size: blob.size, type: blob.type });
    if (!(blob instanceof Blob) || blob.size <= 0 || blob.type !== "application/pdf") {
      throw new Error("PDF 생성 결과가 유효하지 않습니다.");
    }
    return blob;
  } catch (error) {
    console.error("[report-pdf] failed", { stage, error });
    throw error;
  }
}

export function triggerBrowserDownload(blob: Blob, filename: string) {
  if (!(blob instanceof Blob) || blob.size <= 0 || blob.type !== "application/pdf") {
    throw new Error("PDF 다운로드에 사용할 파일이 없습니다.");
  }
  const url = URL.createObjectURL(blob);
  console.debug("[report-pdf] 07 object URL created", { hasUrl: Boolean(url), filename });
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.style.display = "none";
  document.body.appendChild(link);
  console.debug("[report-pdf] 08 anchor appended", {
    attached: document.body.contains(link), href: link.href, download: link.download,
  });
  link.click();
  console.debug("[report-pdf] 09 anchor click invoked", { filename });
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  console.debug("[report-pdf] 10 cleanup scheduled", { filename });
}
