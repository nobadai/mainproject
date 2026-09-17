"""plan_state.py — 매입안이 **실제로 어느 상태인가**. 화면 둘이 같이 쓴다.

╔══════════════════════════════════════════════════════════════════════════╗
║  🔴 **규칙의 주인이 하나다.**                                              ║
║                                                                          ║
║  이 판정은 대시보드(`api/dashboard/query.py`)가 먼저 세웠고, 이제 매입 탭  ║
║  (`api/purchase/query.py`)도 같은 낱말을 싣는다. 두 벌로 짜면 한쪽만       ║
║  고치는 날이 오고, 그날 같은 안이 화면마다 다른 상태로 뜬다.               ║
╚══════════════════════════════════════════════════════════════════════════╝

★ **여기서 DB 를 읽지 않는다.** 읽어 온 값(`RecordedTotals`)을 받아 낱말로 가리기만
  한다 — 표를 읽는 자리는 마스터 한 곳이다
  (`master/purchase_record_repository.recorded_totals_by_plan`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.contracts.core import ITEMS

if TYPE_CHECKING:
    from app.master.purchase_record_repository import RecordedTotals

#: 매입안이 **실제로 어느 상태인가**. 화면이 쓰는 낱말은 이 넷뿐이다 (2026-09-16).
#:
#: .. code-block:: text
#:
#:     결정 없음             후보
#:     승인 · 기록 없음       승인됨
#:     승인 · 실매입 기록됨    매입 기록됨
#:     반려                  반려
#:
#: 🔴 **상태 코드를 화면에 쓰지 않는다.** `APPROVED` · `AWAITING_PURCHASE_RECORD` 같은
#:    것은 API 안쪽 어휘다 (`master/decision.py`). 사람이 읽는 자리에는 사람 말만 쓴다.
#: 🔴 **낱말을 늘리지 않는다.** 늘리는 순간 같은 사실을 화면마다 다른 이름으로 부른다.
CANDIDATE = "후보"
APPROVED = "승인됨"
RECORDED = "매입 기록됨"
REJECTED = "반려"
PLAN_STATES = (CANDIDATE, APPROVED, RECORDED, REJECTED)


def plan_item(key: str) -> str | None:
    """안의 품목. 🔴 매입 `Plan` 스키마에 품목 칸이 없다 (2026-09-14 확인).

    매입 `_plan()` 이 `key=f"{item} · {label}"` 로 품목을 이름 앞에 넣는다. 그 앞자리를
    **계약 품목(`ITEMS`)과 맞춰** 읽는다 — 이름을 코드에 박지 않고, 계약 밖이면 공란.
    매입 스키마에 품목 칸이 서는 날 이 함수를 그 칸 읽기로 바꾼다.
    """
    return next((item for item in ITEMS if key.startswith(f"{item} · ")), None)


def plan_label(key: str) -> tuple[str, str] | None:
    """안 이름을 `(품목, 안 이름)` 으로 가른다 — 실매입 기록을 맞출 열쇠다.

    ★ `plan_item` 과 **같은 규칙**을 쓴다 (`key=f"{item} · {label}"`). 계약 밖 품목이면
      `None` 이고, 그러면 기록도 안 맞춘다 — 지금 사는 품목이 아니다.
    """
    item = plan_item(key)
    return None if item is None else (item, key[len(item) + len(" · ") :])


def recorded_for(
    records: dict[tuple[str, str], RecordedTotals], key: str
) -> RecordedTotals | None:
    """이 안에 적힌 실매입. 🔴 **열쇠가 `(품목, 안 이름)` 둘 다**여야 한다.

    품목만 맞추면 같은 품목의 다른 안(보수 · 기본 · 공격)에 **엉뚱한 기록**이 붙는다.
    """
    pair = plan_label(key)
    return None if pair is None else records.get(pair)


def state_of(*, approved: bool, recorded: RecordedTotals | None) -> str:
    """안이 **실제로 어느 상태인가** 를 사람 말로 가린다.

    🔴 종전에는 `승인 대기` 아니면 **`후보`** 였다. `pending` 이 거짓이라는 것은 «결정이
       났다» 는 뜻인데 화면에는 「후보」가 찍혔다 — 사람이 읽으면 **사실과 정반대**다.

    .. code-block:: text

        실측  dev@1df31f8 · SIM-CHECK-HOLIDAY-0916 · 2026-04-13
          배추 574 × 491  281,834   "후보"   🔴 승인 + 실매입 기록 완료
          무   403 × 196   78,988   "후보"   🔴 승인 + 실매입 기록 완료
        같은 응답의 「이번 주 확정 매입액」 353,988 은 그 둘의 **기록값**이었다

    ★ **`pending` 이 아니라 `approved` 로 가른다.** 🔴 둘은 **서로 반대가 아니다**
      (2026-09-17 · `#813`) — `pending` 은 **요청** 단위(같은 요청에 결정이 없나)이고
      `approved` 는 **안** 단위(이 안이 승인됐나)다. 같은 요청에서 다른 안이 승인되면
      고르지 않은 형제 안은 **둘 다 거짓**이다. 이 칸이 말하려는 것은 **이 안의 승인 여부**다.

    .. code-block:: text

        결정 없는 요청의 안            pending 참     approved 거짓   → 후보
        승인된 안                      pending 거짓   approved 참     → 승인됨 · 매입 기록됨
        같은 요청에서 고르지 않은 안    pending 거짓   approved 거짓   → 후보

        실측  REH-0914 08-31 · 배추 · 기본 (보수가 승인된 요청)   pending 거짓 · approved 거짓

    ⚠️ **「반려」를 지금은 아무도 안 낸다.** 거절(`REJECT_ALL`)은 `scenario_label` 이 NULL
      이라(`master_decisions` CHECK) 안 하나에 붙지 않고, 매입 탭이 주는 `Plan` 에는 그
      사실을 실을 칸이 없다. 지어내지 않고 「후보」로 둔다 — 낱말만 어휘에 세워 둔다.
    """
    if not approved:
        return CANDIDATE
    return APPROVED if recorded is None else RECORDED
