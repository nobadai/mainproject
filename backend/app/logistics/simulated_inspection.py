"""simulated_inspection.py — MVP 시뮬레이션 검수 사실을 만든다 (`#333`).

```text
as_of · DueInbound  →  ScenarioSimulatedInspectionProvider.provide  →  InspectionFact
                                                                        verdict  = PASS
                                                                        accepted = 도착 수량
```

★ **`inbound_execution.InspectionProvider` 의 첫 구현체다.** 그 Protocol 이 계약이고
  이 파일은 값을 채우기만 한다 — 조립 순서도, 상태 분기도, 검수 항등식 검증도 전부
  남의 것이다 (`inbound_execution._receive_one` · `inspections.validate_outcome` ·
  `inspections.record_inspection`).

🔴 **품질 모델이 아니다.** *"실제 농산물이 늘 100% 정상이다"* 라는 주장이 아니라,
   **이번 MVP 가 품질손실 축을 아직 쓰지 않는다**는 명시적 가정이다. 저장소 어디에도
   합격률·손실률을 정한 규칙이 없고(실측), 없는 규칙을 여기서 만들면 아무도 정한 적
   없는 비율이 곧 업무 사실이 되어 원가·폐기·판매 판단으로 흘러간다.

   ```text
   95% PASS / 3% HOLD / 2% REJECT   ❌  아무도 정한 적 없는 비율이다
   품목별 · 등급별 손실률            ❌  근거가 되는 표가 없다
   random · seed                     ❌  같은 실행을 다시 돌리면 값이 달라진다
   LLM 품질판정                      ❌  provider 는 잠금을 쥔 채 불린다
   ```

🔴 **`purchase_detail` 을 판정 근거로 쓰지 않는다.** Protocol 이 넘겨 주지만
   `grade` · `unit_price_krw_per_kg` · `quantity_kg` 중 어느 것도 이번 판정을 바꾸지
   않는다 — *"등급이 수용률을 정한다"* 는 규칙이 저장소에 없기 때문이다. 그렇다고
   인자를 지우지도 않는다: 서명의 주인은 Protocol 이고, 근거가 생기는 날 값만 읽으면
   된다.

🔴 **검수 수량의 출처는 `inbound.item.quantity_kg` 다.** *"운송 중이던 그 물건이
   도착해 검수됐다"* 가 이 수량의 뜻이라, 매입 줄 수량(`ordered_qty_kg` 가 되는 값)과
   **다른 축**이다 — DB 주석도 *"도착량과 검수량의 차이는 `inbound_receipts` 쪽
   축이다"* 라고 적는다.

   ★ `InTransitItem.quantity_kg` 는 `Field(gt=0)` 이라 `inspected_qty_kg > 0` 이
     **구조적으로 보장된다.** `purchase_items.quantity_kg` 는 CHECK 가 `>= 0` 이라
     0 이 합법이고, 그 값을 쓰면 `validate_outcome` 이 정상 입고를 예외로 뒤집는다.

🔴 **시계를 읽지 않는다.** `datetime.now()` · `utcnow()` · `date.today()` 를 부르지
   않는다 — 같은 시뮬레이션을 다시 돌리면 같은 값이 나와야 한다.

   ```text
   inspected_at = as_of 00:00 UTC
   ```

   ★ **`as_of` 다. `expected_arrival_date` 가 아니다.** 검수는 **처리한 날** 한 일이라
     연체분에서 둘이 갈린다. 도착일의 주인은 `receipts.create_arrived_receipt` 이고
     그것이 `arrived_at` 에 이미 적는다 — 같은 사실을 두 칸에 적지 않는다.

     ```text
     도착 예정  2026-09-08     arrived_at    2026-09-08          ← receipts 가 정한다
     실제 처리  2026-09-09     inspected_at  2026-09-09 00:00 UTC
     ```

   ⚠️ **`00:00` 은 업무 시각 주장이 아니다.** `inspected_at` 이 `TIMESTAMPTZ` 라 tz 를
      단 값이 필요할 뿐이고(naive 는 `record_inspection` 이 거부한다), 우리가 아는
      시간 사실은 **날짜 하나**다. 시·분을 지어내면 없는 업무 규칙이 하나 생긴다.

   ⚠️ **UTC 인 이유.** `00:00 UTC` 는 KST 로 **같은 날 09:00** 이라 어느 쪽으로 읽어도
      날짜가 안 밀린다. 반대로 `00:00 KST` 는 UTC 로 **전날 15:00** 이다.

🔴 **`inspector` 에 사람 이름을 짓지 않는다.** 저장소에 시스템 행위자 규약이 없고
   (실측: 운영 코드 상수 0건 · 씨앗 0건), 이 검수를 한 것은 사람이 아니라 이 코드다.
   그 사실을 그대로 적는다.

🔴 **`None` 을 내지 않는다.** Protocol 의 `None` 은 *"이 입고의 검수 사실을 모른다"*
   이고, 이 provider 는 정상 입력에서 늘 만들 수 있다. 그 계약 자체는 그대로 남는다 —
   검수 원천이 아직 없는 배선은 여전히 `None` 을 내는 provider 를 꽂으면 되고, 그때
   그날 도착분이 `INSPECTION_FACT_UNAVAILABLE` 로 보이는 길도 그대로다.

★ **순수 계산이다.** 커넥션·SQL·commit·rollback·HTTP·LLM·파일 I/O·난수가 없다.
  Protocol docstring 이 요구하는 제약이고, 이 함수가 **두 잠금(도착 전역 advisory ·
  그날 fixture 행 `FOR UPDATE`)을 쥔 채** 불리기 때문이다 — 여기서 느리면 도착 처리
  전체가 그동안 직렬화된 채 멈춘다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal

from app.logistics.arrival import DueInbound
from app.logistics.inbound_execution import InspectionFact
from app.logistics.inspections import InspectionOutcome, InspectionVerdict
from app.logistics.purchase_detail import PurchaseDetail

__all__ = ["ScenarioSimulatedInspectionProvider"]


#: 🔴 **사람이 아니다.** `inbound_inspections.inspector` 는 NOT NULL 인데 저장소에
#: 시스템 행위자 규약이 없다 — 없는 사람을 짓는 대신 **이 사실을 만든 것이 무엇인가**
#: 를 적는다.
#:
#: ⚠️ **`fact_source` 와 글자가 같지만 다른 칸이다.** 그쪽은 `record_inspection` 이
#:    하드코딩하고(`inspections._FACT_SOURCE_SIMULATED`), 이 칸은 호출자 몫이다.
#:    여기서 `fact_source` 를 넘기지 않는 이유가 그것이다 — 인자가 아니다.
_INSPECTOR = "SCENARIO_SIMULATED"

#: 🔴 **손실률이 아니라 "그 축을 아직 안 쓴다" 는 뜻이다.** 이 0 에서 품질 정책을
#: 역으로 읽지 않는다.
_NO_LOSS = Decimal(0)

#: 🔴 `ck_inbound_inspections_verdict` 어휘 그대로다. 이 판정일 때 DB CHECK 와
#: `validate_outcome` 이 함께 `hold = 0 · reject = 0` 을 요구하고, 그래서
#: `accepted == inspected` 가 **선택이 아니라 계약**이 된다.
_VERDICT_PASS: InspectionVerdict = "PASS"


class ScenarioSimulatedInspectionProvider:
    """도착 수량을 **그대로 통과시키는** 검수 provider. **상태가 없다.**

    ★ 생성 인자가 없다 — 정할 것이 없기 때문이다. 합격률·손실률·시드 같은 것을
      인자로 열면 그 순간 *"누군가 그 값을 정해야 한다"* 가 되고, 정할 근거가 없다.

    ⚠️ **배선 자리에서 눈에 보이게 고른다.** `LogisticsInboundExecution` 이 provider
       를 필수 인자로 두는 이유가 그것이라, 이 클래스가 있다고 해서 그것이 기본값이
       되지는 않는다.
    """

    def provide(
        self,
        *,
        as_of: date,
        inbound: DueInbound,
        purchase_detail: PurchaseDetail,
    ) -> InspectionFact:
        """이 입고의 검수 사실 하나. **순수 계산이고 결정론이다.**

        ```text
        inspected_qty_kg = inbound.item.quantity_kg
        accepted_qty_kg  = inbound.item.quantity_kg
        hold_qty_kg      = 0
        reject_qty_kg    = 0
        verdict          = PASS
        inspected_at     = as_of 00:00 UTC
        inspector        = SCENARIO_SIMULATED
        ```

        🔴 **항등식을 여기서 검증하지 않는다.** `inspections.validate_outcome` 이
           `record_inspection` 안에서 같은 규칙을 걸고, DB CHECK 가 마지막 그물이다 —
           같은 규칙을 세 번째로 적으면 어휘가 바뀌는 날 이쪽만 옛 규칙을 들고 남는다.

        🔴 **`None` 을 내는 경로가 없다.** 이 provider 가 모르는 입고는 없다.

        :param as_of: 이번 도착 처리를 실행한 **달력일**. 검수 시각의 날짜가 된다.
        :param inbound: `arrival.select_due_inbound` 이 `due` 로 가른 행. 수량의 출처다.
        :param purchase_detail: 매입 원장이 확정한 사실. **이번 판정을 바꾸지 않는다** —
            등급·단가가 수용률을 정한다는 규칙이 저장소에 없다.
        """
        # 🔴 **운송 중이던 그 수량이다.** 매입 줄 수량으로 다시 세지 않는다 — 둘은
        #    다른 축이고, `gt=0` 이 보장되는 쪽은 이쪽뿐이다.
        quantity = inbound.item.quantity_kg
        return InspectionFact(
            # ★ 날짜는 `as_of` 에서 오고, 시·분은 **만들지 않는다.**
            inspected_at=datetime.combine(as_of, time.min, tzinfo=UTC),
            inspector=_INSPECTOR,
            outcome=InspectionOutcome(
                verdict=_VERDICT_PASS,
                # ★ 두 칸에 **같은 값**이 들어가는 것이 PASS 의 뜻이다.
                inspected_qty_kg=quantity,
                accepted_qty_kg=quantity,
                hold_qty_kg=_NO_LOSS,
                reject_qty_kg=_NO_LOSS,
            ),
        )
