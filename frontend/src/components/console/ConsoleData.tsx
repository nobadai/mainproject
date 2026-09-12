"use client";

/**
 * 운영 콘솔 탭이 공통으로 쓰는 상태 표시와 표.
 *
 * 🔴 **여섯 상태를 가른다** — `loading` · `success` · `empty` · `error` · `blocked` ·
 *    `unsupported`. 특히 **`0` 과 «데이터 없음» 은 다르다**: 백엔드가 0 을 내면 0 을
 *    적고, 값 자체가 없으면 그렇게 말한다. 둘을 한 칸에 뭉치면 *"채권이 0원"* 과
 *    *"채권을 못 읽었다"* 가 화면에서 같은 말이 된다.
 *
 * 🔴 **여기서 업무 계산을 하지 않는다.** 합계도 마진도 백엔드가 낸 값을 그대로 적는다.
 */

import { useEffect, useRef, useState } from "react";

import { ConsoleError } from "@/lib/console_api";

interface State<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
}

/**
 * 실행 축이 정해졌을 때만 부른다.
 *
 * ⚠️ `enabled` 가 거짓이면 **요청을 아예 만들지 않는다.** 빈 실행으로 한 번 부르고
 *   400 을 받아 오류를 띄우면, 사람은 고장난 줄 안다 — 고르지 않았을 뿐이다.
 */
export function useConsoleData<T>(key: string, load: () => Promise<T>, enabled: boolean): State<T> {
  const [state, setState] = useState<State<T>>({ data: null, error: null, loading: enabled });
  const [shown, setShown] = useState(key);
  //  ★ 키가 바뀌면 **그리는 중에** 상태를 되돌린다. 효과 안에서 되돌리면 한 번 옛 값이
  //    그려진 뒤 다시 그려져, 실행을 바꾼 순간 **남의 실행 숫자가 한 프레임 보인다.**
  if (shown !== key) {
    setShown(key);
    setState({ data: null, error: null, loading: enabled });
  }
  const loader = useRef(load);
  useEffect(() => {
    loader.current = load;
  });
  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    loader
      .current()
      .then((value) => {
        if (alive) setState({ data: value, error: null, loading: false });
      })
      .catch((error: unknown) => {
        if (!alive) return;
        //  ★ 서버가 낸 문장을 그대로 올린다 — 무엇을 고쳐야 하는지 알려 주는 말이다.
        setState({
          data: null,
          loading: false,
          error:
            error instanceof ConsoleError
              ? `[${error.status || "연결 실패"}] ${error.message}`
              : String(error),
        });
      });
    return () => {
      alive = false;
    };
  }, [key, enabled]);
  return state;
}

function Frame({ title, body, tone }: { title: string; body: string; tone: string }) {
  return (
    <section
      className="rounded-xl border border-dashed bg-panel px-5 py-10 text-center"
      style={{ borderColor: "var(--color-hair)" }}
    >
      <p className="m-0 text-[15px] font-semibold">{title}</p>
      <p className="mb-0 mt-2 text-[12px] text-ink2">{body}</p>
      <p className="mb-0 mt-3 font-mono text-[10px] tracking-[0.12em] text-ink2">{tone}</p>
    </section>
  );
}

/** 실행을 안 골랐을 때. **API 를 부르지 않은 상태**이지 실패가 아니다. */
export function NoRunSelected() {
  return (
    <Frame
      title="실행을 선택해 주세요"
      body="운영 콘솔의 모든 조회는 실행(sim_run_id)에 묶입니다. 위에서 실행을 지정하면 그 실행의 저장된 사실만 조회합니다."
      tone="NO RUN CONTEXT"
    />
  );
}

/** 조회는 됐는데 행이 0건. **«0 건» 은 사실이다** — 미구축과 다르다. */
export function EmptyRows({ what }: { what: string }) {
  return <Frame title={`${what} 0건`} body="이 실행·기준일에 저장된 행이 없습니다." tone="EMPTY" />;
}

/** authoritative 계약이 없어서 못 하는 것. 아직 안 만든 것과 구분한다. */
export function Blocked({ what, why }: { what: string; why: string }) {
  return <Frame title={`${what} 차단`} body={why} tone="BLOCKED" />;
}

/** 원장 자체가 없어서 답할 수 없는 것. */
export function Unsupported({ what, why }: { what: string; why: string }) {
  return <Frame title={`${what} 미지원`} body={why} tone="UNSUPPORTED" />;
}

export function Skeleton({ what }: { what: string }) {
  return <Frame title={`${what} 조회 중`} body="저장된 사실을 읽고 있습니다." tone="LOADING" />;
}

export function Failed({ what, message }: { what: string; message: string }) {
  return (
    <section
      className="rounded-xl border px-5 py-6"
      style={{ borderColor: "var(--color-hair)" }}
    >
      <p className="m-0 text-[13px] font-semibold">{what} 조회 실패</p>
      <p className="mb-0 mt-2 whitespace-pre-wrap font-mono text-[11.5px] text-ink2">{message}</p>
    </section>
  );
}

export interface Column<T> {
  key: string;
  label: string;
  align?: "left" | "right";
  mono?: boolean;
  render: (row: T) => string;
}

export function Table<T>({ columns, rows }: { columns: Column<T>[]; rows: T[] }) {
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
            <tr key={index}>
              {columns.map((column) => (
                <td
                  key={column.key}
                  className={`border-b px-2 py-2 ${column.align === "right" ? "text-right tabular-nums" : ""} ${
                    column.mono ? "font-mono text-[11px]" : ""
                  }`}
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

/** 값 하나. **`null` 이면 «데이터 없음» 으로 적고 0 으로 바꾸지 않는다.** */
export function Metric({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-lg border p-3" style={{ borderColor: "var(--color-hair)" }}>
      <span className="text-[11.5px] text-ink2">{label}</span>
      <b className="mt-1 block text-[15px] tabular-nums">{value}</b>
      {hint && <span className="mt-1 block text-[11px] text-ink2">{hint}</span>}
    </div>
  );
}

export function Metrics({ children }: { children: React.ReactNode }) {
  return <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">{children}</div>;
}
