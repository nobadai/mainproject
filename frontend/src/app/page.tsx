"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { saveSession } from "@/lib/session";

/**
 * 로그인.
 *
 * 백엔드에 인증이 없어 적힌 이름을 검증하지 않습니다. 결정 API 가 받는 승인자
 * 이름은 여기서 적은 값입니다. 서버 인증이 붙으면 `lib/session.ts` 만 바뀝니다.
 */
export default function LoginPage() {
  const router = useRouter();
  // 🔴 **미리 채우지 않는다.** 시연 편의로 값을 넣어 뒀더니, 클릭 후 타이핑이
  //    덧붙어 `이현서이현서` 가 그대로 승인자 이름으로 저장됐다 (실측 2026-08-30).
  //    `onFocus` 의 select() 로는 못 막는다 — 이미 포커스된 칸을 다시 클릭하면
  //    focus 이벤트가 안 난다. 예시는 placeholder 로 내리면 **빈 칸에서 시작**하고,
  //    빈 칸은 `handleSubmit` 의 trim 검사가 이미 막는다.
  const [employeeId, setEmployeeId] = useState("");
  const [name, setName] = useState("");

  function signIn() {
    if (!employeeId.trim() || !name.trim()) return;
    saveSession({ employeeId: employeeId.trim(), name: name.trim(), role: "admin" });
    router.push("/console");
  }

  return (
    <main className="grid min-h-screen place-items-center bg-sunk px-5 py-10">
      <div className="w-full max-w-[360px]">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            signIn();
          }}
          className="rounded-xl border border-line bg-surface p-6 shadow-[0_1px_2px_rgba(21,26,22,.05),0_8px_24px_-12px_rgba(21,26,22,.18)]"
        >
          <span className="mb-3.5 grid size-[34px] place-items-center rounded-[9px] bg-accent text-[20px] font-bold text-white">
            햇
          </span>
          <h1 className="m-0 text-[23px] font-semibold">햇들농산 운영 콘솔</h1>
          <p className="m-0 mb-5 mt-0.5 text-[17px] text-muted">사번과 이름으로 들어갑니다.</p>

          <label className="mb-2.5 block">
            <span className="mb-1 block text-[16px] text-muted">사번</span>
            <input
              value={employeeId}
              onChange={(e) => setEmployeeId(e.target.value)}
              placeholder="2026-0142"
              className="w-full rounded-lg border border-line bg-surface px-3 py-2 font-mono text-[18px] outline-none focus:border-accent"
            />
          </label>

          <label className="mb-1 block">
            <span className="mb-1 block text-[16px] text-muted">이름</span>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="이현서"
              className="w-full rounded-lg border border-line bg-surface px-3 py-2 text-[18px] outline-none focus:border-accent"
            />
          </label>

          <button
            type="submit"
            className="mt-4 w-full rounded-lg bg-accent py-2.5 text-[18px] font-semibold text-white"
          >
            로그인
          </button>
        </form>
      </div>
    </main>
  );
}
