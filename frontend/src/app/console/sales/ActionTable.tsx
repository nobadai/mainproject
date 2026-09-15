"use client";

/**
 * 행마다 버튼이 붙는 표.
 *
 * ★ **왜 공용 `Table` 을 안 쓰는가.** 그쪽 `Column.render` 는 `string` 만 돌려줄 수 있어
 *   버튼을 넣을 수 없고, 공용 컴포넌트는 이번 판의 수정 범위 밖이다. 그래서 **버튼이
 *   필요한 표만** 판매 쪽에 따로 둔다 — 모양은 공용 표와 같게 맞춘다.
 *
 * 🔴 **버튼을 표 밖에 묶지 않는다.** 이름이 같은 버튼을 행 수만큼 아래에 나열하면 어느
 *    행의 버튼인지 알 수 없다. 액션은 그 행 안에 있어야 한다.
 */

import { useState, type ReactNode } from "react";

export interface ActionColumn<T> {
  key: string;
  label: string;
  align?: "left" | "right";
  mono?: boolean;
  render: (row: T) => ReactNode;
  /** 버튼처럼 화면용 JSX인 셀도 있어, 검색할 업무 텍스트는 따로 둔다. */
  searchText?: (row: T) => string;
}

export function ActionTable<T>({
  columns,
  rows,
  rowKey,
}: {
  columns: ActionColumn<T>[];
  rows: T[];
  rowKey: (row: T, index: number) => string;
}) {
  const [search, setSearch] = useState("");
  const [columnKey, setColumnKey] = useState("");
  const [page, setPage] = useState(0);
  const pageSize = 10;
  const query = search.trim().toLocaleLowerCase("ko-KR");
  const entries = rows.map((row) => ({
    row,
    values: Object.fromEntries(
      columns.map((column) => [
        column.key,
        column.searchText?.(row) ?? readableCellText(column.render(row)),
      ]),
    ),
  }));
  const filtered = entries.filter(({ values }) => {
    if (!query) return true;
    const valuesToSearch = columnKey ? [values[columnKey]] : Object.values(values);
    return valuesToSearch.some((value) => value.toLocaleLowerCase("ko-KR").includes(query));
  });
  const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
  const currentPage = Math.min(page, pageCount - 1);
  const visible = filtered.slice(currentPage * pageSize, (currentPage + 1) * pageSize);

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2 text-[12px]">
        <label className="sr-only">주문·판매 검색</label>
        <input
          value={search}
          onChange={(event) => {
            setSearch(event.target.value);
            setPage(0);
          }}
          placeholder="거래처·금액·상태 검색"
          className="min-w-[180px] rounded-md border px-3 py-1.5"
          style={{ borderColor: "var(--color-hair)" }}
        />
        <label className="sr-only">검색 항목</label>
        <select
          value={columnKey}
          onChange={(event) => {
            setColumnKey(event.target.value);
            setPage(0);
          }}
          className="rounded-md border px-2 py-1.5"
          style={{ borderColor: "var(--color-hair)" }}
        >
          <option value="">모든 항목에서 검색</option>
          {columns.map((column) => <option key={column.key} value={column.key}>{column.label}</option>)}
        </select>
        <span className="text-ink2">검색 결과 {filtered.length}건</span>
      </div>
      {filtered.length === 0 ? (
        <p className="m-0 rounded-lg border px-4 py-5 text-[12px] text-ink2" style={{ borderColor: "var(--color-hair)" }}>
          조건에 맞는 주문·판매 항목이 없습니다. 검색어 또는 검색 항목을 바꿔 보세요.
        </p>
      ) : <>
      <div className="thin-scroll overflow-x-auto">
        <table className="w-full border-collapse text-[12px]">
        <thead>
          <tr>
            {columns.map((column) => (
              <th
                key={column.key}
                className={`border-b px-2 py-2 text-[11px] font-semibold text-ink2 ${
                  column.align === "right" ? "text-right" : "text-left"
                }`}
                style={{ borderColor: "var(--color-hair)" }}
              >
                {column.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {visible.map(({ row }, index) => (
            <tr key={rowKey(row, currentPage * pageSize + index)}>
              {columns.map((column) => (
                <td
                  key={column.key}
                  className={`border-b px-2 py-2 ${
                    column.align === "right" ? "text-right tabular-nums" : ""
                  } ${column.mono ? "font-mono text-[11px]" : ""}`}
                  style={{ borderColor: "var(--color-hair)" }}
                >
                  {column.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
      <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-[12px] text-ink2">
        <span>{currentPage * pageSize + 1}–{Math.min((currentPage + 1) * pageSize, filtered.length)} / {filtered.length}건</span>
        <div className="flex items-center gap-2">
          <button type="button" onClick={() => setPage((value) => Math.max(0, value - 1))} disabled={currentPage === 0} className="rounded-md border px-2 py-1 disabled:cursor-not-allowed disabled:opacity-50" style={{ borderColor: "var(--color-hair)" }}>이전</button>
          <span>{currentPage + 1} / {pageCount}</span>
          <button type="button" onClick={() => setPage((value) => Math.min(pageCount - 1, value + 1))} disabled={currentPage >= pageCount - 1} className="rounded-md border px-2 py-1 disabled:cursor-not-allowed disabled:opacity-50" style={{ borderColor: "var(--color-hair)" }}>다음</button>
        </div>
      </div>
      </>}
    </div>
  );
}

function readableCellText(value: ReactNode): string {
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (Array.isArray(value)) return value.map(readableCellText).join(" ");
  return "";
}
