"""
closing.py — **하루가 닫히는 자리.** 일곱째 등록소.

🔴 **`daily_closings` 에 INSERT 하는 코드가 저장소 전체에 0곳이다** (실측 2026-09-10).

```text
INSERT 하는 코드   **0곳**
읽는 곳            master/ledger_repository · finance/dashboard · api/_agent_docs
행 30 · 2025-12-02 ~ 12-31   번인 시드 (sim_run_id = SIM-BURNIN-202512)
걷기 구간(2026-01~)          **0행**
```

★★ **걷기가 손익을 한 줄도 안 남긴다.** 하루를 닫는 자리가 없어서다. `#150` 손익
  곡선이 여기에 막혀 있다.

⚠️ `register_inbound` 호출이 0건이던 것(`#337`) · `register_collection` 이 0건이던
  것(`#427`) · `register_receivable` 이 0건이던 것(`#489`)과 **같은 모양**이다. 다만
  **한 가지가 다르다** — 저 셋은 구현이 서 있는데 부르는 자리가 없었고, 여기는
  **구현 자체가 아직 없다.** 그래서 이 판이 끝나도 곡선은 여전히 빈다 (아래 §한계).

---

★ **마감은 재무 원장 계산이다. 마스터가 하지 않는다.**

```text
마스터가 하는 것   "그날을 닫아 달라" 고 부르고, 답을 어휘로 받아 적는다
재무가 하는 것     그날 숫자와 daily_closings 적재
어댑터가 없으면    **미등록** — "오늘 마감이 안 돈다" 로 정직하게 보인다
```

🔴 **이 모듈은 숫자를 계산하지 않는다. 잔액을 추정하지 않는다.
   `daily_closings` 에 직접 쓰지 않는다.**

  ⚠️ 마스터가 계산하면 **조정자가 부서를 겸한다** — 등록소를 여섯 개나 나눈 이유가
    정확히 그것이다. `purchase_cash_out_krw` 하나만 여기서 세어도 그 값의 주인이
    둘이 되고, 재무가 세는 값과 갈리는 날 **에러 없이 손익만 틀린다.**

  ★ **그래서 `ClosingPartOut` 에 금액 칸이 하나도 없다.** 무엇이 닫혔는지의 **키**만
    나른다 — 금액의 권위 사실은 `daily_closings` 한 곳이다
    (`ReceivablePartOut.issued` 가 `receivable_id` 만 나르는 것과 같은 규율).

  🔴 **`tests/master/test_closing.py` 가 원문과 AST 로 그것을 잠근다** — 이 파일에
    산술 연산이 하나라도 생기면 검사가 그 자리에서 빨개진다.

---

## 🔴 한계 — 이 판이 끝나도 곡선은 빈다

```text
경계    app/master/closing.py         🟢 이 판
배선    register_closing("finance", …) 🔴 **어댑터가 없어서 못 적는다**
구현    daily_closings 적재            🔴 재무 몫 — 청하는 문서를 냈다
```

  ★ **그것이 맞는 상태다.** 지금은 매일 `NOTHING_DUE` + `missing=["finance"]` 로
    나가고, 그것은 *"오늘 마감이 안 돈다"* 를 **정직하게 보이게** 하는 값이다.
    붙는 날 `bootstrap` 한 줄이면 된다.

  ⚠️ **"다 됐다" 로 읽히면 안 된다.** 이 판이 만든 것은 **자리**이지 숫자가 아니다.

---

## 1. 하루 순서 — **맨 끝**

```text
지금  개장 → 입고 → 채권 → 수금 → [장부 관문] → 판단 → 출고
후    …  →  출고 → **마감**
```

🔴 **출고 뒤다.** 그날 움직임이 다 끝난 뒤에 닫는다. `scheduler.py` 가
  *"출고는 장부 관문 뒤, 판단 뒤"* 라고 적어 둔 그 결을 그대로 잇는다 — 출고가
  재고를 움직이므로 **출고 앞에서 닫으면 그날 재고가 마감 뒤에 바뀐다.**

  ⚠️ 그러면 `inventory_qty_kg` 가 그날 장부와 안 맞고, **에러는 안 난다.**

---

## 2. 어휘 — 채권 등록소와 **같은 다섯**

```text
CLOSED         닫았다
NOTHING_DUE    닫을 것이 없다 (그날 움직임이 없었다) — 또는 미등록이다
BLOCKED        **장부가 안 서서 못 닫는다**
NOT_OPENED     그날이 안 열렸다
FAILED         부르다 터졌다 — **아무것도 안 바뀌었다**
```

🔴 **새 어휘를 짓지 않았다.** `ReceivableOut.status` 의 다섯을 그대로 가져왔다.

🔴 **«마감이 없다» 와 «마감이 막혔다» 는 다르다.**

```text
NOTHING_DUE   그날 닫을 움직임이 실제로 없었다 — 정상이다
BLOCKED       **장부가 안 서서** 못 닫았다 — 사람이 봐야 한다
NOT_ATTEMPTED 단계를 아예 안 탔다 (`DayRunOutcome` 이 든다) — 안 돈 날이다
```

  ⚠️ 접으면 **장부가 안 선 날의 빈 곡선**과 **아무 일도 없던 날의 빈 곡선**이 화면에서
    같아 보인다. 앞은 고칠 것이 있고 뒤는 없다.

---

## 3. 🔴 반드시 지키는 것

```text
① 마감이 실패해도 **그날 걷기 결과를 안 바꾼다**
   try_save_run 이 `try_` 인 이유와 같다 — 이력 때문에 운영이 멈추면 안 된다
② 장부 관문이 막은 날은 **BLOCKED** 다 — 안 닫는다
③ 같은 날을 두 번 걸어도 두 벌이 안 쌓인다
④ sim_run_id 를 인자로 받아 흘린다 — **상수로 박지 않는다**
```

🔴 **`③` 의 근거는 표의 키다** (실측 2026-09-10).

```text
database/10_domain_schema.sql:4251
  ADD CONSTRAINT daily_closings_pkey PRIMARY KEY (sim_run_id, close_date)
```

  ★ **그래서 이 모듈이 어댑터에게 주는 것이 정확히 그 둘이다** — `as_of` 와
    `sim_run_id`. 마스터가 `closing_id` 같은 것을 지어내면 **같은 날이 두 벌 쌓인다.**

  ⚠️ **`master_agent_runs` 에서 이미 겪었다.** 그 표는 UNIQUE 가 있는데도 `run_id` 가
    PK 라 같은 `request_id` 를 두 번 넣어도 안 막혔다. **제약이 있다는 말과 그 제약이
    내가 넣는 키를 막는다는 말은 다르다** — 그래서 여기서는 키를 확인하고 적었고,
    검사가 그 DDL 줄을 다시 읽는다.

  🔴 **그리고 이 모듈은 어댑터를 파트마다 정확히 한 번만 부른다.** 두 번 부르고
    어댑터의 멱등에 기대지 않는다 — 그것이 `master_agent_runs` 에서 틀렸던 그 기대다.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from app.finance.db import get_connection
from app.master.day_gate import check_day_gate

__all__ = [
    "PARTS",
    "ClosingOut",
    "ClosingPart",
    "ClosingPartOut",
    "ClosingPort",
    "close_day",
    "missing",
    "register_closing",
    "registered",
    "reset",
]

ClosingPart = Literal["finance"]

#: 하루를 닫는 파트.
#:
#: ★ **재무 하나다.** `daily_closings` 는 매입·판매·물류·현금·미수·재고를 **집계한**
#:   결과이고(DDL 주석), 그 집계의 주인은 재무 원장이다. 물류가 재고를 알고 매입이
#:   현금유출을 알지만, **그 셋을 한 줄로 만드는 것은 재무 계산**이다.
#:
#: ⚠️ **하나짜리 등록소가 과한 것이 아니다.** 이것이 있어야 *"마감 구현이 없다"* 와
#:    *"그날 닫을 움직임이 없었다"* 를 가를 수 있다. 둘은 다른 사실이고, 뭉치면
#:    구현이 안 붙은 채로 **매일 조용히 아무 일도 안 일어난다** — 지금이 그 상태다.
PARTS: tuple[ClosingPart, ...] = ("finance",)


class ClosingPort(Protocol):
    """그날을 닫는 방법. **재무가 소유한다.**

    🔴 **마스터는 `as_of` 와 `sim_run_id` 만 준다.** 그날 얼마가 나가고 얼마가
      들어왔는지, 잔액이 얼마인지는 **전부 재무 사실**이다 — `CollectionSource` ·
      `ReceivableSource` · `InboundExecution` 과 같은 이유다.

    ★ **그 둘이 곧 `daily_closings` 의 PK 다** (`(sim_run_id, close_date)`).
      마스터가 줄 것이 그 둘뿐인 이유가 그것이다 — 여기에 마스터가 지어낸 id 를
      더하면 같은 날이 두 벌 쌓인다.

    ★ **`conn` 은 받기만 한다.** commit·rollback·close 를 하지 않는다 — 트랜잭션
      경계는 마스터가 쥔다.

    🔴 **멱등이어야 한다.** 같은 날을 두 번 걸어도 마감 행이 하나여야 한다.
      표의 PK 가 그것을 받쳐 주지만, **믿는 것과 검사로 박는 것은 다르다** —
      `tests/master/test_closing.py` 가 하루를 두 번 걸어 잰다.

      ⚠️ 그리고 **마스터는 그 멱등에 기대지 않는다.** 파트마다 정확히 한 번만
        부른다 — 두 번 부르고 `ON CONFLICT` 가 받아 주기를 기대하는 것이
        `master_agent_runs` 에서 틀렸던 그 기대다.

    :param as_of: 닫을 날. **달력일이다** — 실행일 달력으로 밀지 않는다.
    :param sim_run_id: 어느 실행의 장부인가. **마스터가 정하고 흘려 준다.**
    :returns: 무엇이 닫혔는지. 그날 움직임이 없었으면 `NOTHING_DUE` 이고 정상이다.
    """

    def close(self, conn: Any, *, as_of: date, sim_run_id: str) -> ClosingPartOut: ...


class ClosingPartOut(BaseModel):
    """한 파트의 마감 결과.

    ★ **`NOTHING_DUE` 를 `CLOSED` 로 접지 않는다.** *"그날 닫을 움직임이 없었다"* 와
      *"닫았다"* 는 다른 사실이고, 뭉치면 **하루가 안 닫히는 버그**가 매일 성공으로
      보인다.
    """

    part: str
    status: Literal["CLOSED", "NOTHING_DUE", "BLOCKED"]
    reason: str = ""
    #: **그날에 대해 서 있는 마감 행.** `daily_closings` 의 키다.
    #:
    #: 🔴 **금액을 여기 싣지 않는다.** 그날 얼마가 나가고 잔액이 얼마인지의 권위
    #: 사실은 `daily_closings` 한 곳이 갖는다 — 여기에 복사해 두면 **같은 사실의
    #: 주인이 둘**이 되고, 롤백된 날 이 목록만 살아남는다.
    #:
    #: ⚠️ **그것이 이 판이 안 하는 것의 전부다.** 칸 하나만 열어 둬도 다음 판이
    #: 거기에 값을 채우고, 그러면 마스터가 손익을 계산하기 시작한다.
    #:
    #: ★ **이번 호출에서 새로 만든 것만이 아니다.** 멱등이라 이미 있으면 안 만들고,
    #: 그래도 마감은 서 있다. 새로 적은 건수는 `created` 가 따로 나른다.
    closed: list[str] = Field(default_factory=list)
    #: 🔴 **이번 호출에서 실제로 새로 적은 건수.**
    #:
    #: ⚠️ **`closed` 와 한 값으로 접으면 안 된다.** 접으면 *"이미 닫혀 있어서 안
    #: 적었다"* 와 *"닫을 것이 없었다"* 가 같아 보이고, 멱등 재실행이 매일
    #: *"아무것도 안 했다"* 로 읽힌다. `CLOSED` 인데 `created == 0` 인 것이
    #: **정상 상태**다.
    #:
    #: 🔴 **어댑터가 낸 값을 그대로 적는다.** 여기서 `len(closed)` 로 다시 세지
    #: 않는다 — 세는 순간 마스터가 계산을 시작하고, 두 번째 걸음이 매일 새 행을
    #: 적은 것처럼 보인다.
    created: int = 0


class ClosingOut(BaseModel):
    """마감 1회의 결과. **`ReceivableOut` 과 같은 다섯 갈래다.**

    ```text
    CLOSED       그날을 닫았다 (새로 적은 건수는 `created` 가 나른다)
    NOTHING_DUE  **그날 닫을 움직임이 없다** — 또는 미등록이다
    BLOCKED      **장부가 안 서서 못 닫는다**
    NOT_OPENED   **그날 장부가 안 열렸다** — 닫을 것이 있는지조차 묻지 않았다
    FAILED       닫아 보다 터졌다 — **아무것도 안 바뀌었다**
    ```

    🔴 **`NOTHING_DUE` 를 `BLOCKED` 로 접지 않는다** (`inbound.py` 의 그 규칙 그대로 ·
       물류 지적 2026-09-06 에서 배운 것이다).

      ```text
      NOTHING_DUE   실제로 그날 닫을 움직임이 없음
      BLOCKED       움직임은 있었는데 장부가 안 서서 못 닫음
      ```

      ⚠️ 접으면 **막힌 마감을 화면이 정상으로 오해한다.** 손익 곡선에 그 날이 빈
        칸으로 남는데, *"그날은 아무 일도 없었다"* 와 **똑같이** 보인다.

    🔴 **`NOT_OPENED` 를 `BLOCKED` 로 접지 않는다.**

      ```text
      BLOCKED      **장부가 안 섰다** — 입고·채권·수금 중 무엇이 막았는지가 사유에 있다
      NOT_OPENED   **아직 아무것도 안 봤다** — 하루가 안 열려 물어보지도 못했다
      ```

      ⚠️ 접으면 *"어제 입고가 막혔다"* 와 *"어제 개장을 안 돌렸다"* 가 같은 문장으로
        나간다. **고칠 곳이 완전히 다른데** 화면은 같아 보인다.

      ★ **다음에 할 일도 다르다** — 그래서 `next_action` 을 같이 싣는다.

    ★ **파트가 `BLOCKED` 면 전체도 `BLOCKED` 다.** 한 파트가 닫았더라도 그렇다 —
      *"닫을 게 있었는데 못 닫았다"* 가 *"닫았다"* 보다 먼저 알려야 하는 사실이다.
    """

    as_of: date
    status: Literal["CLOSED", "NOTHING_DUE", "BLOCKED", "NOT_OPENED", "FAILED"]
    reason: str = ""
    parts: list[ClosingPartOut] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    #: 🔴 **키가 항상 있고 막히지 않았으면 `None` 이다.** `ReceivableOut.next_action`
    #: 과 같은 모양이다. `NOT_OPENED` 일 때 개장 Gate 가 준 값을 **해석하지 않고
    #: 그대로** 옮긴다.
    next_action: str | None = None


# ── 등록소 ──────────────────────────────────────────────────────────────
#
# 🔴 **일곱 번째 등록소다.** 전이(승인이 장부를 바꾸는 방법) · 하루 넘김(하루가
#    넘어가는 방법) · 취소(승인을 물리는 방법) · 입고(도착분을 받는 방법) ·
#    수금(돈이 들어오는 방법) · 채권(판매 확정이 채권을 만드는 방법) ·
#    여기(**하루가 닫히는 방법**).
#
# ⚠️ **앞의 여섯 중 아무 데도 합치지 않는다.** 합치면 *"채권은 서는데 하루는 안 닫히는"*
#    상태를 표현할 수 없고, **지금이 정확히 그 상태다.**

_CLOSINGS: dict[ClosingPart, Any] = {}


def register_closing(part: ClosingPart, impl: Any) -> None:
    """마감 구현을 등록한다. 배선(`bootstrap.py`)이 부른다.

    🔴 **오늘은 아무도 이 함수를 안 부른다.** 재무 마감 어댑터가 아직 없어서다 —
      붙는 날 `bootstrap.wire_registries` 에 한 줄이면 된다. 그때까지
      `close_day` 는 매일 `NOTHING_DUE` + `missing=["finance"]` 를 낸다.
    """
    if part not in PARTS:
        raise ValueError(f"마감 파트가 아니다: {part!r}. 가능: {', '.join(PARTS)}")
    _CLOSINGS[part] = impl


def registered() -> Mapping[ClosingPart, Any]:
    """지금 등록된 마감 구현. **읽기용 사본**이다."""
    return dict(_CLOSINGS)


def missing() -> tuple[str, ...]:
    """아직 마감 구현이 없는 파트. **`PARTS` 순서를 지킨다.**"""
    return tuple(part for part in PARTS if part not in _CLOSINGS)


def reset() -> None:
    """등록을 비운다. 검사용이다."""
    _CLOSINGS.clear()


# ── 경계 ────────────────────────────────────────────────────────────────


def close_day(
    as_of: date,
    *,
    sim_run_id: str,
    ledger_gap: str = "",
    connect: Any = None,
) -> ClosingOut:
    """`as_of` 를 **한 트랜잭션으로** 닫는다. **숫자는 재무가 낸다.**

    ★ **하루의 맨 끝이다.** 개장 → 입고 → 채권 → 수금 → [장부 관문] → 판단 →
      출고 **→ 마감**. 출고가 재고를 움직이므로 그 앞에서 닫으면 그날 재고가
      마감 뒤에 바뀐다.

    ★ **예외를 밖으로 내지 않는다.** `receive_arrivals` · `collect_receipts` ·
      `issue_receivables` 와 같다 — **마감 실패가 그날 걷기 결과를 바꾸면 안 된다.**
      `try_save_run` 이 `try_` 인 이유와 같은 이유다: 이력 때문에 운영이 멈추면 안 된다.

      ⚠️ **다만 `sim_run_id` 가 비면 예외를 낸다** (`read_procurement_boundary` 와 같은
        태도). 빈 축은 그날의 사실이 아니라 **배선 사고**이고, 조용히 전체로 바꾸면
        남의 실행 장부에 이 날의 마감이 앉는다. 부르는 쪽(`scheduler._stage`)이
        그것을 `FAILED` 로 옮기므로 걷기는 여전히 안 멈춘다.

    🔴 **판정 순서가 계약이다.**

      ```text
      ① 개장 Gate 가 BLOCKED   →  NOT_OPENED   닫을 것이 있는지조차 안 묻는다
      ② ledger_gap 이 있다      →  BLOCKED      **장부가 안 서서 못 닫는다**
      ③ 미등록                  →  NOTHING_DUE  + missing
      ④ 그 밖                   →  어댑터에게 묻는다
      ```

      🔴 **`①` 이 `②` 보다 앞이다.** 하루가 안 열린 날은 장부 관문에 오지도 않는다 —
        순서를 뒤집으면 *"안 열린 날"* 이 *"장부가 안 섰다"* 로 접힌다.

      ★ **`check_day_gate` 는 열지 않는다. 묻기만 한다.** 여기서 `open_day` 를 부르면
        마감이 개장의 부작용이 된다.

      ⚠️ **미등록은 PASS 다** (`day_gate` 계약).

    :param sim_run_id: 어느 실행의 장부를 닫는가. 🔴 **인자다. 여기에 상수를 박지
        않는다** — 박으면 실행이 둘이 되는 날 이 파일을 고쳐야 하고, 값의 주인이
        `ledger_repository.BURN_IN_SIM_RUN_ID` 하나라는 사실이 깨진다.
    :param ledger_gap: 장부 관문이 막았으면 **그 사유**. 빈 문자열이면 안 막혔다는
        뜻이다. 🔴 **여기서 문장을 다시 짓지 않는다** — 부르는 쪽
        (`scheduler._ledger_gap_note`)이 이미 만든 그 문장을 그대로 받는다. 두 벌이
        되면 한쪽만 고치는 날 화면과 이력이 갈린다.
    """
    if not sim_run_id.strip():
        raise ValueError(
            "sim_run_id 없이 하루를 닫을 수 없다 — 어느 실행의 장부인지 없으면"
            " 마감 행이 어디에 앉을지가 정해지지 않는다"
        )

    gate = check_day_gate(as_of, connect=connect)
    if gate.gate == "BLOCKED":
        return ClosingOut(
            as_of=as_of,
            status="NOT_OPENED",
            reason=gate.reason,
            # ★ **해석하지 않고 옮긴다.** 무엇을 해야 하는지는 개장이 아는 사실이다.
            next_action=gate.next_action,
        )

    if ledger_gap:
        # 🔴 **장부가 안 선 날은 닫지 않는다.** 입고가 안 들어오고 채권이 안 서고
        #    수금이 안 반영된 채로 그날을 닫으면, **그 틀린 숫자가 손익 곡선의
        #    확정값으로 앉는다.** 판단을 막는 이유가 그대로 마감을 막는 이유다.
        #
        # ★ **어댑터를 부르지 않는다.** 부르고 나서 결과를 버리는 것이 아니라
        #   **아예 안 묻는다** — 재무 쪽에 반쯤 적힌 행이 남으면 안 된다.
        #
        # ⚠️ **`NOTHING_DUE` 가 아니다.** *«마감이 없다»* 와 *«마감이 막혔다»* 는
        #   다르고, 접으면 손익 곡선의 빈 칸 두 종류가 화면에서 같아 보인다.
        return ClosingOut(as_of=as_of, status="BLOCKED", reason=ledger_gap)

    absent = missing()
    if absent:
        # ★ **미등록은 오류가 아니다.** 그 파트가 아직 하루를 안 닫는다는 뜻이고,
        #   *"그날 닫을 움직임이 없다"* 와 다른 사실이다 — `missing` 이 그것을 가른다.
        #
        # 🔴 **오늘이 이 길이다.** 재무 마감 어댑터가 아직 없다.
        return ClosingOut(
            as_of=as_of,
            status="NOTHING_DUE",
            reason=f"마감 미등록: {', '.join(absent)}",
            missing=list(absent),
        )

    adapters = registered()
    open_connection = get_connection if connect is None else connect
    conn = open_connection()
    try:
        # 🔴 **파트마다 정확히 한 번이다.** 두 번 부르고 어댑터의 멱등에 기대지
        #    않는다 — `master_agent_runs` 에서 틀렸던 그 기대다.
        results = [adapters[part].close(conn, as_of=as_of, sim_run_id=sim_run_id) for part in PARTS]
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - 마감 실패가 그날 걷기 결과를 바꾸면 안 된다.
        conn.rollback()
        return ClosingOut(as_of=as_of, status="FAILED", reason=f"마감 실패: {exc}")
    finally:
        conn.close()

    return _aggregate(as_of, results)


def _aggregate(as_of: date, parts: list[ClosingPartOut]) -> ClosingOut:
    """파트 결과를 전체 어휘로 취합한다. **값을 다시 세지 않는다.**

    ```text
    BLOCKED 가 하나라도  → BLOCKED     닫을 게 있었는데 못 닫았다
    CLOSED 가 하나라도   → CLOSED
    전부 NOTHING_DUE     → NOTHING_DUE
    ```

    🔴 **순서가 계약이다.** `BLOCKED` 를 먼저 보는 이유는 그것이 **사람이 봐야 하는
       사실**이기 때문이다 — `CLOSED` 뒤로 밀면 *"오늘 닫았다"* 로 지나간다.

    🔴 **파트 결과를 그대로 싣는다.** `created` 를 `len(closed)` 로 다시 세거나
       금액을 합치지 않는다 — 세는 순간 마스터가 계산을 시작한다.
    """
    blocked = [part for part in parts if part.status == "BLOCKED"]
    if blocked:
        return ClosingOut(
            as_of=as_of,
            status="BLOCKED",
            reason=f"닫을 것이 있는데 막혔다: {', '.join(part.part for part in blocked)}",
            parts=parts,
        )
    if any(part.status == "CLOSED" for part in parts):
        return ClosingOut(as_of=as_of, status="CLOSED", parts=parts)
    return ClosingOut(as_of=as_of, status="NOTHING_DUE", parts=parts)
