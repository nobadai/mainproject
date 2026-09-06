"""inbound_execution.py — 도착 예정을 **실제 입고로 실행한다** (3-B4-J).

```text
runtime in_transit
  → select_due_inbound            도착 자격 판정 (순수 계산)
  → fetch_purchase_detail          매입 줄 — 등급·단가의 권위 출처
  → check_receipt_state            **어디서부터 이어갈지**를 여는 열쇠
  → create_arrived_receipt         (없을 때만) ARRIVED
  → record_inspection              (검수 전일 때만) → INSPECTED
  → materialize_inspected_inbound  Lot · 원장 IN · PUTAWAY_DONE · 일정 정리
  → RECEIVED · NOTHING_DUE · BLOCKED
```

★ **이 파일에는 업무가 없다.** WMS 규칙은 전부 위 모듈들이 이미 소유하고 있고,
  여기서 하는 일은 **순서와 분기와 어휘**뿐이다 (`master.transition.apply_approval`
  이 재무·물류를 감싸는 것, `inbound_stock` 이 Lot·원장을 감싸는 것과 같은 결).

🔴 **재구현 금지 목록.** 아래는 전부 남의 모듈이 이미 한다.

  ```text
  도착일 규칙(<= as_of)        arrival.select_due_inbound
  매입 참조 해석               purchase_detail.fetch_purchase_detail
  Receipt 정체성 · 멱등        receipts (receipt_id 결정론 + advisory lock)
  검수 항등식 · 상태 마감      inspections.record_inspection
  Lot · 원장 IN · 일정 정리    inbound_stock.materialize_inspected_inbound
  ```

  ⚠️ 여기에 SQL 이 한 줄도 없는 것이 그 규율의 증거다.

🔴 **`ALREADY_EXISTS` 를 "다 됐다" 로 읽지 않는다.**

  ```text
  Receipt 가 ARRIVED 로 있다 · 검수 없음 · Lot 없음 · 원장 IN 없음
  ⇒ 행은 있지만 **재고는 안 들어왔다**
  ```

  그래서 `check_receipt_state` 가 함께 주는 `receipt_status` 로 갈라
  **마지막 성공 단계 다음부터** 이어간다 (`_receive_one` 표 참조). 존재 여부만 보고
  건너뛰면 Receipt 만 남고 재고가 안 들어온 채 영구 고착된다.

🔴 **검수 결과를 지어내지 않는다 — provider 를 주입받는다.**

  저장소 어디에도 *"자동 시뮬레이션에서 몇 %가 PASS 인가"* 를 정한 규칙이 없다
  (`inspections.py` 모듈 docstring 의 실측). 그래서 이 파일은 판정도 수량도
  검수자도 검수시각도 **만들지 않고**, `InspectionProvider` 가 주는 사실을 그대로
  `record_inspection` 에 넘긴다.

  ```text
  자동 PASS                        ❌
  accepted = ordered_qty_kg        ❌
  inspector = "SYSTEM"             ❌  저장소에 시스템 행위자 규약이 없다
  inspected_at = datetime.now()    ❌  같은 실행을 다시 돌리면 값이 달라진다
  ```

  ⚠️ **기본 provider 를 두지 않는다.** 생성 인자를 필수로 두면 *"검수 사실의 주인이
     누구인가"* 를 배선 자리(`app/main.py`)에서 눈에 보이게 정하게 된다. 기본값을
     두면 그 기본값이 곧 업무 규칙이 되고, 아무도 그것을 정한 적이 없다.

🔴 **BLOCKED 와 예외를 가른다.**

  ```text
  BLOCKED   처리 대상은 있는데 **권위 있는 입력이 없어** 못 간다
            → 값으로 돌려준다. 다른 입고는 계속 처리한다
  예외      실행·무결성이 깨졌다
            → 밖으로 올린다. 마스터가 통째로 롤백하고 FAILED 로 만든다
  ```

  ⚠️ **`except Exception` 으로 뭉뚱그리지 않는다.** 무결성 위반을 BLOCKED 로 삼키면
     *"데이터를 주세요"* 로 나가고, 깨진 장부 위에서 다음 날이 계속 걷는다.

🔴 **`FAILED` 를 물류가 만들지 않는다.** `InboundPartOut.status` 어휘는
   `RECEIVED · NOTHING_DUE · BLOCKED` 셋뿐이고, `FAILED` 는 파트가 예외를 올렸을 때
   `master.inbound.receive_arrivals` 가 롤백과 함께 만든다. 마스터 계약은 마스터 것이다.

🔴 **커밋도 롤백도 하지 않고 커넥션을 새로 열지 않는다.** 커넥션은 마스터가 주고
   커밋은 마스터가 한 번 한다 — 여기서 커밋하면 뒤이어 터졌을 때 **반쪽만 들어온
   입고**가 남는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal, Protocol

from app.logistics.arrival import (
    ArrivalBlockReason,
    ArrivalUnresolvedReason,
    DueInbound,
    select_due_inbound,
)
from app.logistics.inbound_stock import (
    load_in_transit_for_receiving,
    materialize_inspected_inbound,
)
from app.logistics.inspections import InspectionOutcome, record_inspection
from app.logistics.purchase_detail import (
    PurchaseDetail,
    PurchaseDetailAmbiguous,
    PurchaseDetailMissing,
    fetch_purchase_detail,
)
from app.logistics.receipts import ReceiptStatus, check_receipt_state, create_arrived_receipt
from app.logistics.transition import USAGE_SCOPE
from app.master.inbound import InboundPartOut

__all__ = [
    "InboundBlockReason",
    "InboundExecutionError",
    "InspectionFact",
    "InspectionProvider",
    "LogisticsInboundExecution",
    "UnknownReceiptStage",
]

#: 이 파트 이름. `master.inbound.PARTS` 의 값과 같아야 한다.
_PART = "logistics"

#: 도착 처리를 막은 사유.
#:
#: ★ **앞 둘은 새 어휘가 아니다.** `arrival` 이 이미 쓰는 값을 그대로 실어 나른다 —
#:   같은 사실에 두 어휘를 두지 않는다 (`Literal` 중첩은 PEP 586 이 평탄화한다).
InboundBlockReason = Literal[
    ArrivalBlockReason,
    ArrivalUnresolvedReason,
    #: 그날 운송 중 목록 자체를 **확인한 적이 없다** (`in_transit_json IS NULL`).
    #: 🔴 *"오늘 받을 것이 없다"* 가 아니다 — 뭉치면 모르는 것을 아는 것처럼 다룬다.
    "IN_TRANSIT_UNRESOLVED",
    #: 매입 참조는 있는데 그 줄이 없다 (`PurchaseDetailMissing`).
    "PURCHASE_DETAIL_MISSING",
    #: 한 `purchase_id` 에 매입 줄이 둘 이상이다 (`PurchaseDetailAmbiguous`).
    "PURCHASE_DETAIL_AMBIGUOUS",
    #: 검수 provider 가 이 입고의 사실을 주지 못했다.
    #: 🔴 그럴 때 대신 만들지 않는다 — 그것이 이 파일이 막으려는 일이다.
    "INSPECTION_FACT_UNAVAILABLE",
]

#: 아직 검수 사실이 없는 Receipt 상태. **여기서만 provider 를 부른다.**
#:
#: 🔴 `inspections._BEFORE_INSPECTION` 과 **같아야 한다.** 갈리면 검수를 못 적는
#:    상태에서 provider 를 부르거나(그 값이 버려진다) 부를 자리를 건너뛴다.
#:    테스트가 두 집합을 대조해 잠근다.
_NEEDS_INSPECTION: frozenset[str] = frozenset({"ARRIVED", "INSPECTING"})

#: 검수 단계가 이미 끝난 Receipt 상태. **provider 를 부르지 않는다.**
#:
#: 🔴 여기서 provider 를 부르면 DB 에 적힌 검수 사실을 이번 판정으로 덮으려 들고,
#:    `record_inspection` 이 `InspectionConflict` 로 정상 재실행을 실패로 뒤집는다.
_INSPECTION_SETTLED: frozenset[str] = frozenset({"INSPECTED", "PUTAWAY_DONE", "CLOSED"})


class InboundExecutionError(RuntimeError):
    """이 모듈이 내는 실패의 조상 (`inbound_stock.InboundStockError` 와 같은 결)."""


class UnknownReceiptStage(InboundExecutionError, ValueError):
    """Receipt 상태를 **어느 단계로도 못 읽는다.**

    🔴 **아는 단계로 접어 읽지 않는다.** 어휘가 늘었는데 이 파일이 안 따라온 상태라,
       검수 전으로 보면 이미 적힌 검수를 덮으려 들고 검수 후로 보면 검수를 건너뛴 채
       재고를 만든다. 둘 다 **에러 없이 틀리는** 쪽이다.

    ⚠️ `check_receipt_state` 가 DB CHECK 어휘 밖 값을 이미 막으므로, 여기 오는 것은
       **어휘가 늘었다는 뜻**이다.
    """


@dataclass(frozen=True)
class InspectionFact:
    """한 입고의 검수 사실. **물류가 만들지 않고 받는다.**

    ★ 세 값이 `record_inspection` 의 세 인자와 **그대로 짝**이다. 여기서 가공하지
      않는다 — 가공하면 provider 가 준 사실과 DB 에 적힌 사실이 갈린다.

    :param inspected_at: 검수 시각. **tz 를 단 값이어야 한다** (`TIMESTAMPTZ`).
        🔴 물류가 시계를 읽어 채우지 않는다 — 같은 시뮬레이션을 다시 돌리면 같은 값이
        나와야 한다. 검증은 `inspections.record_inspection` 이 한다.
    :param inspector: 검수자. NOT NULL 이고 **저장소에 시스템 행위자 규약이 없다.**
    :param outcome: 판정과 네 수량. 항등식 검증은 `inspections.validate_outcome` 이 한다.
    """

    inspected_at: datetime
    inspector: str
    outcome: InspectionOutcome


class InspectionProvider(Protocol):
    """검수 사실의 **권위 출처**. 물류 밖에서 온다.

    🔴 **저장소에 구현이 없는 것이 지금의 정직한 상태다.** 자동 검수 규칙(합격률·
       등급별 판정·수량 배분)을 정한 문서도 코드도 씨앗 데이터도 없다. 여기에 기본
       구현을 놓으면 **아무도 정한 적 없는 비율이 곧 업무 사실이 되어** 원가·폐기·
       판매 판단으로 흘러간다.

    ★ **`None` 은 실패가 아니라 부재다.**

      ```text
      InspectionFact  이 입고의 검수 사실을 안다
      None            이 입고의 검수 사실을 **모른다** → BLOCKED
      ```

      ⚠️ **`None` 으로 오류를 숨기지 않는다.** provider 안에서 조회가 터졌다면 그것은
         예외이지 부재가 아니다 — 올리면 마스터가 롤백하고 `FAILED` 로 만든다.

    ★ **항상 `None` 을 내는 provider 는 정당한 배선이다.** 검수 원천이 아직 없는 날
      *"받을 것은 있는데 검수 사실이 없다"* 가 `BLOCKED` 로 매일 보이는 것이 맞다.
      그것을 기본값으로 코드에 숨기지 않고 배선 자리에서 고르게 두는 것이 요점이다.

    🔴 **잠금을 쥔 채 불린다 — 구현이 지켜야 할 제약이 여기서 나온다.**

    ```text
    도착 전역 advisory (20260905, 2)
    → 그날 fixture 행 FOR UPDATE
    → provide()          ★ 여기. 두 잠금이 이미 걸려 있다
    → materialize
    → 마스터가 commit
    ```

       ```text
       MVP provider 는 빠르고 결정론적인 **사실 조회**여야 한다
       외부 HTTP · LLM · 장시간 I/O 를 하지 않는다
       commit · rollback · 커넥션 수명을 소유하지 않는다
       ```

       ⚠️ 여기서 느리면 **도착 처리 전체가 그동안 직렬화된 채 멈춘다** — 그 트랜잭션이
          끝날 때까지 다른 도착 처리도, 승인 전이(`persist_inventory`)의 그날 행 갱신도
          함께 기다린다. 그리고 비결정론이면 같은 시뮬레이션을 다시 돌린 결과가 달라진다.

    :param as_of: 처리 기준일. **달력일**이다.
    :param inbound: `arrival.select_due_inbound` 이 `due` 로 가른 행.
    :param purchase_detail: 매입 원장이 확정한 사실 (품목·등급·수량·단가).
    """

    def provide(
        self,
        *,
        as_of: date,
        inbound: DueInbound,
        purchase_detail: PurchaseDetail,
    ) -> InspectionFact | None: ...


class LogisticsInboundExecution:
    """`app.master.inbound.InboundExecution` 의 물류 구현.

    🔴 **`sim_run_id` 는 생성 인자다.** *"어느 실행의 장부인가"* 는 물류 사실이 아니라
       실행 정체성이고, Protocol 의 `receive(conn, *, as_of)` 는 그 값을 안 나른다 —
       `LogisticsTransitionAdapter` · `LogisticsCancellationAdapter` ·
       `LogisticsDayOpening` 과 **같은 자리**다. 모듈 상수로 박으면 실행이 둘이 되는
       날 물류 코드를 고쳐야 한다.

       ⚠️ **`None` 을 받지 않는다.** `day_open` 은 조회 경로라 `None` 을 받고 둘 이상
          보이면 멈추는 길이 있었지만, 이쪽은 **쓰기 경로**다. 실행을 모르는 채로
          Receipt · Lot · 원장을 만들면 남의 장부에 적을 수 있다.

    🔴 **`inspection_provider` 도 필수다.** 기본값을 두는 순간 그 기본값이 검수 정책이
       된다 (`InspectionProvider` 참조).

    :param sim_run_id: 이 도착 처리가 앉을 시뮬레이션 실행. **마스터가 소유한 값**이고
        `app/master/ledger_repository.BURN_IN_SIM_RUN_ID` 가 그 주인이다.
    :param inspection_provider: 검수 사실의 권위 출처.
    :param usage_scope: 조회·갱신 대상 범위. 기본값이 곧 현재 계약이다.
    """

    def __init__(
        self,
        *,
        sim_run_id: str,
        inspection_provider: InspectionProvider,
        usage_scope: str = USAGE_SCOPE,
    ) -> None:
        # ★ 빈 문자열은 **주입은 했는데 값이 안 실린 것**이다. 조용히 넘기면 조회가
        #   0건이 되고 그 0건은 "그날 행이 없다" 로 읽힌다 (`LogisticsDayOpening` 과
        #   같은 규율).
        if not sim_run_id or not sim_run_id.strip():
            raise ValueError(
                f"도착 처리에 쓸 수 없는 sim_run_id 다: {sim_run_id!r}."
                " 어느 실행의 장부인지 없이 Receipt · Lot · 원장을 만들 수 없다."
            )
        if not usage_scope or not usage_scope.strip():
            raise ValueError(f"도착 처리에 쓸 수 없는 usage_scope 다: {usage_scope!r}.")
        # 🔴 **배선 오류는 배선 시점에 터져야 한다.** `None` 을 그대로 들고 있으면 객체는
        #    멀쩡히 서고, **실제로 도착할 물건이 생긴 날** `provide` 에서 AttributeError 로
        #    늦게 터진다 — 그때는 마스터가 그 예외를 `FAILED` 로 바꿔 그날 입고를 통째로
        #    롤백하므로, 배선 실수가 **운영 장애의 모습**으로 나타난다.
        #
        # ⚠️ **Protocol 준수 여부는 검사하지 않는다.** `isinstance` 로 `provide` 서명까지
        #    보려 들면 대역·부분구현이 정당한 자리에서 막힌다 — 여기서 막는 것은
        #    *"주입을 안 했다"* 하나뿐이다.
        if inspection_provider is None:
            raise ValueError(
                "inspection_provider 가 없다. 검수 사실의 주인 없이 도착 처리를 세울 수 없다 —"
                " 물류가 판정을 지어내지 않기 때문이다."
                " 검수 원천이 아직 없으면 항상 None 을 내는 provider 를 배선하면 된다:"
                " 그러면 그날 도착분이 INSPECTION_FACT_UNAVAILABLE 로 보인다."
            )
        self._sim_run_id = sim_run_id
        self._provider = inspection_provider
        self._usage_scope = usage_scope

    def receive(self, conn: Any, *, as_of: date) -> InboundPartOut:
        """`as_of` 까지 도착 자격을 얻은 입고를 **전부** 처리한다.

        ```text
        ① 그날 내 실행의 in_transit 을 잠그고 읽는다
        ② select_due_inbound 으로 네 갈래로 가른다   ← 순수 계산, 여기서 규칙을 안 만든다
        ③ due 를 한 건씩 처리한다                     ← 한 건이 막혀도 나머지는 계속 간다
        ④ 어휘를 취합한다
        ```

        🔴 **당일만 처리하지 않는다.** `select_due_inbound` 이 `eta <= as_of` 로 가르므로
           연체분(overdue)도 함께 온다 — 어느 날 이 실행이 안 돌았어도 다음 날이 밀린
           것을 받는다. 그 판정을 여기서 다시 쓰지 않는다.

        🔴 **`blocked` 가 하나라도 있으면 전체가 `BLOCKED` 다.** 받은 것이 있어도
           그렇다 — *"받을 게 있었는데 못 받았다"* 가 *"받았다"* 보다 먼저 알려야 하는
           사실이다 (`master.inbound._aggregate` 가 파트 사이에서 하는 판단과 같다).

        ⚠️ **`received` 에는 재고화까지 끝난 건만 담는다.** Receipt 만 서고 검수에서
           막힌 건은 담지 않는다 — 담으면 *"받았다"* 가 거짓이 된다.

        :param conn: 마스터가 소유한 커넥션. commit · rollback · close 를 하지 않는다.
        :param as_of: 받는 날. **달력일**이다 (토·일·공휴일 포함).
        """
        # ── ① 잠그고 읽는다 (도착 전역 → fixture 행 FOR UPDATE) ────────
        in_transit = load_in_transit_for_receiving(
            conn,
            sim_run_id=self._sim_run_id,
            as_of=as_of,
            usage_scope=self._usage_scope,
        )

        # ── ② 판정은 순수 계산이 한다 ─────────────────────────────────
        selection = select_due_inbound(in_transit, as_of=as_of)

        blocked_reasons: list[str] = []
        if selection.source_status == "UNRESOLVED":
            # 🔴 네 목록이 다 비지만 `CONFIRMED_ZERO` 와 **다른 사실**이다.
            blocked_reasons.append(_format_block_reason(None, "IN_TRANSIT_UNRESOLVED"))
        for blocked in selection.blocked:
            # ★ 사유를 **전부** 남긴다. 하나만 적으면 그것을 고친 뒤 또 막힌다.
            blocked_reasons.extend(
                _format_block_reason(blocked.item.inbound_id, reason) for reason in blocked.reasons
            )
        for unresolved in selection.unresolved:
            blocked_reasons.append(
                _format_block_reason(unresolved.item.inbound_id, unresolved.reason)
            )

        # ── ③ 한 건씩 — 막힌 건이 나머지를 세우지 않는다 ──────────────
        received: list[str] = []
        for inbound in selection.due:
            block_reason = self._receive_one(conn, as_of=as_of, inbound=inbound)
            if block_reason is None:
                received.append(inbound.inbound_id)
            else:
                blocked_reasons.append(_format_block_reason(inbound.inbound_id, block_reason))

        # ── ④ 어휘 ────────────────────────────────────────────────────
        if blocked_reasons:
            return InboundPartOut(
                part=_PART,
                status="BLOCKED",
                reason="; ".join(blocked_reasons),
                # ★ 막힌 것이 있어도 **받은 것은 받은 것이다.** 지우면 그 사실이 사라진다.
                received=received,
            )
        if received:
            return InboundPartOut(part=_PART, status="RECEIVED", received=received)
        return InboundPartOut(part=_PART, status="NOTHING_DUE")

    def _receive_one(
        self, conn: Any, *, as_of: date, inbound: DueInbound
    ) -> InboundBlockReason | None:
        """입고 한 건을 **마지막 성공 단계 다음부터** 이어 처리한다.

        ```text
        Receipt 상태            이 함수가 부르는 것
        ─────────────────────  ────────────────────────────────────────────────
        (없음)                 create_arrived_receipt → provider → record_inspection
                               → materialize
        ARRIVED · INSPECTING    provider → record_inspection → materialize
        INSPECTED               materialize                       ★ provider 안 부른다
        PUTAWAY_DONE · CLOSED   materialize                       ★ provider 안 부른다
                                (기존 Lot · Move 를 **읽어서 검증**하고 남은 일정만 걷는다)
        ```

        ⚠️ **`materialize` 는 어느 경로에서도 부른다.** `PUTAWAY_DONE` 이어도 일정이
           안 걷힌 반쪽 상태가 있을 수 있고, 그 마무리가 정확히 그 함수의 일이다.
           재고를 다시 만들지는 않는다 — 없으면 무결성 오류로 멈춘다.

        :returns: 막혔으면 그 사유, 끝까지 갔으면 `None`.
        """
        # ── 매입 참조 해석 ────────────────────────────────────────────
        try:
            purchase_detail = fetch_purchase_detail(conn, purchase_id=inbound.purchase_id)
        except PurchaseDetailMissing:
            # ★ **부재다.** 승인이 만든 매입 줄 없이 물건이 왔다는 뜻이라 진행할 수 없고,
            #   시세·평균원가로 대신 채우지 않는다.
            return "PURCHASE_DETAIL_MISSING"
        except PurchaseDetailAmbiguous:
            # ★ 어느 줄이 이 입고의 것인지 **고르지 않는다.** 고른 단가가 로트 원가로 굳는다.
            return "PURCHASE_DETAIL_AMBIGUOUS"
        # 🔴 `InvalidPurchaseIdentity` 는 잡지 않는다. `select_due_inbound` 이 빈 참조를
        #    이미 `blocked` 로 걸렀으므로, 여기 오면 그것은 계약이 깨진 것이다.

        # ── 어디서부터 이어갈지 ───────────────────────────────────────
        existing = check_receipt_state(
            conn, sim_run_id=self._sim_run_id, inbound_id=inbound.inbound_id
        )
        if existing.status == "NEW":
            written = create_arrived_receipt(
                conn,
                sim_run_id=self._sim_run_id,
                inbound=inbound,
                purchase_detail=purchase_detail,
            )
            receipt_id = written.receipt_id
            receipt_status: ReceiptStatus = written.receipt_status
        else:
            # ★ 타입 좁히기 — `ALREADY_EXISTS` 면 두 값이 다 있다 (`check_receipt_state`
            #   이 비거나 어휘 밖인 값을 이미 막는다).
            assert existing.receipt_id is not None
            assert existing.receipt_status is not None
            # 🔴 **DB 에 적힌 id 를 쓴다.** 우리가 지은 값이 아니라 그 행이 진짜다.
            receipt_id = existing.receipt_id
            receipt_status = existing.receipt_status

        # ── 검수 — **검수 전 상태에서만** ─────────────────────────────
        if receipt_status in _NEEDS_INSPECTION:
            fact = self._provider.provide(
                as_of=as_of, inbound=inbound, purchase_detail=purchase_detail
            )
            if fact is None:
                # 🔴 **대신 만들지 않는다.** 여기서 PASS 를 지어내면 그 수량이 그대로
                #    가용재고가 되고, 아무도 그 판정을 한 적이 없다.
                return "INSPECTION_FACT_UNAVAILABLE"
            # ★ 받은 세 값을 **그대로** 넘긴다. 항등식·시간대·검수자 검증은 저쪽 일이다.
            record_inspection(
                conn,
                receipt_id=receipt_id,
                inspected_at=fact.inspected_at,
                inspector=fact.inspector,
                outcome=fact.outcome,
            )
        elif receipt_status not in _INSPECTION_SETTLED:
            # 🔴 아는 단계로 접어 읽지 않는다 — 어휘가 늘었다는 뜻이다.
            raise UnknownReceiptStage(
                f"Receipt 상태를 검수 전으로도 후로도 읽을 수 없다: {receipt_status!r}"
                f" (receipt_id={receipt_id!r}, inbound_id={inbound.inbound_id!r})."
                f" 검수 전: {sorted(_NEEDS_INSPECTION)} · 검수 후: {sorted(_INSPECTION_SETTLED)}."
            )

        # ── 재고화 · 일정 정리 ────────────────────────────────────────
        # ★ Lot · 원장 IN · PUTAWAY_DONE · 일정 두 칸 정리가 **저 함수 안에 다 있다.**
        #   여기서 다시 조립하지 않는다.
        materialize_inspected_inbound(
            conn,
            as_of=as_of,
            receipt_id=receipt_id,
            purchase_detail=purchase_detail,
            usage_scope=self._usage_scope,
        )
        return None


def _format_block_reason(inbound_id: str | None, reason: InboundBlockReason) -> str:
    """막힌 사유 한 줄. **어느 건인지와 왜인지를 함께 남긴다.**

    ★ `inbound_id` 가 없는 것도 사실이다 (`ARRIVAL_INBOUND_ID_MISSING` 인 행). 빈
      문자열로 적으면 사유 목록에서 그 행이 사라진 것처럼 보인다.
    """
    return f"{inbound_id}: {reason}" if inbound_id else reason
