"use client";

import { CAN, ROLE_LABEL, type Session } from "@/lib/session";

/**
 * 🔴 **잠금은 화면에서만 걸린다.** 서버는 누구든 부를 수 있으므로 이건 안전장치가
 * 아니라 **안내**다. 그 사실을 잠긴 탭의 툴팁에 적어 둔다 — 화면이 잠갔다고
 * 안전하다고 믿는 것이 가장 위험하다.
 */

interface Props {
  session: Session;
  active: string;
  onSelect: (key: string) => void;
  onSignOut: () => void;
}

export function Sidebar({ session, active, onSelect, onSignOut }: Props) {
  const can = CAN[session.role];

  const agents = [
    { key: "master", icon: "◆", label: "마스터", badge: "대화", open: true },
    { key: "purchase", icon: "▤", label: "매입", open: can.procure },
    { key: "inventory", icon: "▥", label: "물류", open: true },
    { key: "finance", icon: "▦", label: "재무", open: can.finance },
  ];
  /**
   * 🔴 '승인 이력' 탭은 **없다.** 사이드바에만 있고 `console/page.tsx` 에 그리는
   * 분기가 없어서, 누르면 마스터 대화가 그대로 나오고 하이라이트만 옮겨 갔다 —
   * 누른 사람은 "안 눌렸나" 로 읽는다. **없는 화면을 눌러 보게 두는 것보다
   * 안 보이는 편이 낫다.**
   *
   * 지금은 실행 이력 화면의 '결정 N회차'가 승인·재요청을 회차와 대상 실행까지
   * 같이 보여준다. 승인만 따로 모아 보는 화면이 필요해지면 **먼저 화면을 만들고**
   * 여기에 되돌린다.
   */
  const records = [{ key: "runs", icon: "≡", label: "실행 이력", open: true }];

  const item = (row: { key: string; icon: string; label: string; badge?: string; open: boolean }) => (
    <button
      key={row.key}
      type="button"
      disabled={!row.open}
      onClick={() => onSelect(row.key)}
      title={row.open ? undefined : "이 역할에는 안 보입니다 — 화면에서만 가린 것이고 서버는 막지 않습니다"}
      className={`flex w-full items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-left text-[13.5px] ${
        active === row.key
          ? "bg-surface font-semibold text-ink shadow-[0_1px_2px_rgba(21,26,22,.06)]"
          : "text-muted"
      } ${row.open ? "hover:bg-surface/70" : "cursor-not-allowed opacity-40"}`}
    >
      <span className="w-4 text-center text-[13px] opacity-75">{row.icon}</span>
      {row.label}
      {row.badge && row.open && (
        <span className="ml-auto rounded-full bg-accent-wash px-1.5 py-px text-[10.5px] font-semibold text-accent-ink">
          {row.badge}
        </span>
      )}
      {!row.open && (
        <span className="ml-auto rounded-full border border-line px-1.5 py-px text-[10.5px] text-faint">
          권한
        </span>
      )}
    </button>
  );

  return (
    <aside className="flex w-[212px] shrink-0 flex-col gap-5 overflow-y-auto border-r border-line bg-sunk p-3">
      <div className="flex items-center gap-2.5 px-1.5">
        <span className="grid size-[26px] place-items-center rounded-[7px] bg-accent text-[13px] font-bold text-white">
          햇
        </span>
        <b className="text-[14.5px] font-semibold">운영 콘솔</b>
      </div>

      <div>
        <p className="mb-1.5 px-2 text-[10.5px] font-semibold uppercase tracking-[0.12em] text-faint">
          에이전트
        </p>
        <nav className="flex flex-col gap-px">{agents.map(item)}</nav>
      </div>

      <div>
        <p className="mb-1.5 px-2 text-[10.5px] font-semibold uppercase tracking-[0.12em] text-faint">
          기록
        </p>
        <nav className="flex flex-col gap-px">{records.map(item)}</nav>
      </div>

      <div className="mt-auto flex items-center gap-2.5 border-t border-line px-2 pt-3">
        <span className="grid size-7 shrink-0 place-items-center rounded-full bg-accent-wash text-[11.5px] font-bold text-accent-ink">
          {session.name.slice(0, 2)}
        </span>
        <div className="min-w-0">
          <b className="block truncate text-[13px] font-semibold">{session.name}</b>
          <small className="block text-[11px] leading-tight text-faint">
            {ROLE_LABEL[session.role]}
          </small>
        </div>
        <button
          type="button"
          onClick={onSignOut}
          className="ml-auto text-[11px] text-faint hover:text-muted"
        >
          나가기
        </button>
      </div>
    </aside>
  );
}
