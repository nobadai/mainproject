"use client";

/**
 * Claude 가 남긴 `.md` 점검 보고서를 문서로 그립니다.
 *
 * ★ **왜 손으로 만들었나.** 우리 화면(`localhost:3100`)은 `react-markdown` 을
 *   씁니다. 이쪽은 **팀 공용 저장소**라 우리 탭 하나 때문에 꾸러미를 늘리지
 *   않습니다. 보고서가 쓰는 문법이 정해져 있어(제목 · 표 · 굵은 글씨 ·
 *   코드칸 · 목록 · 인용) 그만큼만 그립니다.
 *
 * ★ **원본 HTML 은 그리지 않습니다.** 보고서는 우리가 만든 것이지만 AI 가 쓴
 *   글이 화면으로 들어오는 통로입니다. 글자는 전부 React 가 글자로 넣습니다 —
 *   `dangerouslySetInnerHTML` 을 안 씁니다.
 *
 * ★ **코드칸 안은 손대지 않습니다.** 그 안에는 세로로 줄 맞춘 수치표가
 *   들어 있습니다. 굵은 글씨로 바꾸려 들면 줄 맞춤이 깨집니다.
 */

import type { ReactNode } from "react";

/* ── 한 줄 안 ── `**굵게**` 와 `` `코드` `` 만 봅니다 ───────────────── */

function inline(text: string, keyBase: string): ReactNode[] {
  const out: ReactNode[] = [];
  //  코드가 먼저입니다 — 코드칸 안의 별표는 굵은 글씨가 아닙니다.
  const re = /`([^`]+)`|\*\*([^*]+)\*\*/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index));
    if (m[1] !== undefined) {
      out.push(
        <code
          key={`${keyBase}-c${i}`}
          className="rounded px-1 py-0.5 font-mono text-[11px]"
          style={{ background: "var(--color-sunk)", color: "var(--color-t-info)" }}
        >
          {m[1]}
        </code>,
      );
    } else {
      out.push(
        <strong key={`${keyBase}-b${i}`} className="font-semibold" style={{ color: "var(--color-ink)" }}>
          {m[2]}
        </strong>,
      );
    }
    last = m.index + m[0].length;
    i += 1;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

/** 표의 한 줄을 칸으로 가릅니다. 양 끝 `|` 는 버립니다. */
function cells(line: string): string[] {
  return line
    .replace(/^\s*\|/, "")
    .replace(/\|\s*$/, "")
    .split("|")
    .map((c) => c.trim());
}

const isDivider = (line: string) => /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(line) && line.includes("-");

const HEAD = [
  "m-0 mb-3 mt-1 text-[15.5px] font-bold",
  "m-0 mb-2 mt-5 border-b pb-1 text-[13.5px] font-semibold",
  "m-0 mb-1.5 mt-4 text-[12.5px] font-semibold",
  "m-0 mb-1.5 mt-3 text-[12px] font-semibold",
];

export function Markdownish({ text }: { text: string }) {
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const out: ReactNode[] = [];
  let i = 0;
  let k = 0;
  const key = () => `b${k++}`;

  while (i < lines.length) {
    const line = lines[i];

    //  빈 줄
    if (!line.trim()) {
      i += 1;
      continue;
    }

    //  코드칸 — 닫는 표시가 없으면 끝까지가 코드입니다
    if (/^\s*```/.test(line)) {
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !/^\s*```/.test(lines[i])) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1;
      out.push(
        <pre
          key={key()}
          className="tabular thin-scroll m-0 mb-3 overflow-x-auto rounded-lg p-3 font-mono text-[11px] leading-relaxed"
          style={{ background: "var(--color-sunk)" }}
        >
          {body.join("\n")}
        </pre>,
      );
      continue;
    }

    //  가로줄
    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      out.push(
        <hr key={key()} className="my-4 border-0 border-t" style={{ borderColor: "var(--color-hair)" }} />,
      );
      i += 1;
      continue;
    }

    //  제목
    const h = /^(#{1,4})\s+(.*)$/.exec(line);
    if (h) {
      const lv = h[1].length;
      const cls = HEAD[lv - 1];
      const style = lv === 2 ? { borderColor: "var(--color-hair)" } : undefined;
      const kids = inline(h[2], `h${k}`);
      out.push(
        lv === 1 ? (
          <h3 key={key()} className={cls}>{kids}</h3>
        ) : lv === 2 ? (
          <h4 key={key()} className={cls} style={style}>{kids}</h4>
        ) : (
          <h5 key={key()} className={cls}>{kids}</h5>
        ),
      );
      i += 1;
      continue;
    }

    //  표 — 다음 줄이 구분선일 때만 표로 봅니다
    if (line.trim().startsWith("|") && i + 1 < lines.length && isDivider(lines[i + 1])) {
      const head = cells(line);
      i += 2;
      const body: string[][] = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        body.push(cells(lines[i]));
        i += 1;
      }
      out.push(
        <div key={key()} className="thin-scroll mb-3 overflow-x-auto">
          <table className="tabular w-full border-collapse text-[11.5px]">
            <thead>
              <tr>
                {head.map((c, ci) => (
                  <th
                    key={ci}
                    scope="col"
                    className="border px-2 py-1 text-left font-semibold"
                    style={{ borderColor: "var(--color-hair)", background: "var(--color-sunk)" }}
                  >
                    {inline(c, `th${ci}`)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {body.map((row, ri) => (
                <tr key={ri}>
                  {row.map((c, ci) => (
                    <td
                      key={ci}
                      className="border px-2 py-1 align-top"
                      style={{ borderColor: "var(--color-hair)" }}
                    >
                      {inline(c, `td${ri}-${ci}`)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }

    //  인용
    if (/^\s*>/.test(line)) {
      const body: string[] = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) {
        body.push(lines[i].replace(/^\s*>\s?/, ""));
        i += 1;
      }
      out.push(
        <blockquote
          key={key()}
          className="m-0 mb-2 whitespace-pre-line border-l-[3px] py-1.5 pl-3 text-[12px] leading-relaxed"
          style={{ borderColor: "var(--color-t-info)", color: "var(--color-mut)" }}
        >
          {inline(body.join("\n"), `q${k}`)}
        </blockquote>,
      );
      continue;
    }

    //  목록 — 번호가 붙은 것도 같은 모양으로 그립니다
    if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*([-*]|\d+\.)\s+/, ""));
        i += 1;
      }
      const ordered = /^\s*\d+\./.test(line);
      const kids = items.map((it, ii) => (
        <li key={ii} className="mb-0.5 leading-relaxed">
          {inline(it, `li${k}-${ii}`)}
        </li>
      ));
      out.push(
        ordered ? (
          <ol key={key()} className="m-0 mb-2 list-decimal pl-5 text-[12.5px]">{kids}</ol>
        ) : (
          <ul key={key()} className="m-0 mb-2 list-disc pl-5 text-[12.5px]">{kids}</ul>
        ),
      );
      continue;
    }

    //  그 밖은 문단. 빈 줄이 나올 때까지 이어 붙입니다
    const para: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^\s*(#{1,4}\s|```|>|[-*]\s|\d+\.\s)/.test(lines[i]) &&
      !/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(lines[i]) &&
      !lines[i].trim().startsWith("|")
    ) {
      para.push(lines[i]);
      i += 1;
    }
    if (para.length === 0) {
      //  위 어디에도 안 걸린 한 줄 (예: 표 구분선만 남은 경우) — 그냥 넘깁니다
      i += 1;
      continue;
    }
    out.push(
      <p key={key()} className="m-0 mb-2 whitespace-pre-line text-[12.5px] leading-relaxed">
        {inline(para.join("\n"), `p${k}`)}
      </p>,
    );
  }

  //  ★ 줄바꿈을 살리는 것은 **문단 안에서만** 합니다. 바깥에 걸면 표 칸과
  //    목록에도 걸려 칸 사이가 벌어집니다.
  return <div>{out}</div>;
}
