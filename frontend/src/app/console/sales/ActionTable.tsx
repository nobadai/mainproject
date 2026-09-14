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

import type { ReactNode } from "react";

export interface ActionColumn<T> {
  key: string;
  label: string;
  align?: "left" | "right";
  mono?: boolean;
  render: (row: T) => ReactNode;
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
  return (
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
          {rows.map((row, index) => (
            <tr key={rowKey(row, index)}>
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
  );
}
