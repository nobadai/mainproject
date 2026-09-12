"""Detect — 관측을 **지속되는 문제**로 바꾼다. 전부 결정론이다.

```text
① observe                     관측 한 벌 (읽기만)
② 탐지기를 돈다               순수 함수: (관측) → 지금 참인 조건들
③ dedupe 축으로 맞댄다        (sim_run_id, code, subject_type, subject_id)
     없으면  INSERT  status=OPEN · opened_as_of=as_of
     있으면  UPDATE  evidence · severity · last_detected_as_of   🔴 status 는 안 건드린다
④ 이번에 안 잡힌 살아 있는 행  RESOLVED  (AFTER_OUTBOUND 에서만)
```

🔴 **임계 비교를 두 벌 적지 않는다.** 신선도 경계는 `rules.count_freshness_risk_lots`
   (`ratio <= threshold`), 용량 경계는 `rules.evaluate_procurement_business_signals` 가
   `CAPACITY_TIGHT` 를 세우는 그 비교(`usage >= tight_ratio`)다. 같은 창고 상태를 두고
   회신은 *"위험 신호 있음"* 인데 Exception 은 0 건인 날이 오면 안 된다.

🔴 **LLM 이 없다.** Commit 2 는 탐지·해소까지다 — 걷기가 매일 돌기 때문에, 여기에
   모델 호출이 들어가면 `--reset` 뒤 같은 걷기가 다른 결과를 낸다 (상세설계 §17).

⚠️ **기준이 없으면 «문제 없음» 이 아니라 «안 쟀다» 다.** 임계 정책이 없거나 사용률을
   못 셈한 날, 그 탐지기는 조건을 내지도 않고 **기존 행을 닫지도 않는다** — 닫으면
   정책 미등재가 *"해결됐다"* 로 장부에 남는다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app.logistics.agent.exceptions import (
    exception_id_for,
    open_exception,
    previous_exception_id_for,
    resolve_exception,
    touch_exception,
)
from app.logistics.agent.observe import (
    CAPACITY_WINDOW_USAGE_UNRESOLVED,
    observe,
)
from app.logistics.agent.schemas import (
    CAPACITY_PRESSURE,
    FRESHNESS_PRESSURE,
    POLICY_OBSERVED_AS_OF,
    WAREHOUSE_SUBJECT_ID,
    DetectedCondition,
    DetectOut,
    DetectPhase,
    ExceptionCode,
    ExceptionEvidence,
    ExceptionRow,
    ObservedLot,
    Severity,
    WarehouseObservation,
    derive_observed_as_of,
)
from app.logistics.rules import (
    CAPACITY_TIGHT_POLICY_UNRESOLVED,
    FRESHNESS_PRESSURE_POLICY_UNRESOLVED,
    count_freshness_risk_lots,
)

__all__ = [
    "CAPACITY_DETECTOR_VERSION",
    "CAPACITY_HIGH_RATIO",
    "COMMITTED",
    "DETECTORS",
    "ESCALATED_FRESHNESS_EXPIRED",
    "FRESHNESS_CRITICAL_REMAINING_DAYS",
    "FRESHNESS_DETECTOR_VERSION",
    "LOT_EMPTY",
    "REDETECT",
    "DetectorOutcome",
    "detect_capacity_pressure",
    "detect_freshness_pressure",
    "detect_logistics_exceptions",
]

#: 가용재고로 인정하는 Lot 상태. `tools._AVAILABLE_LOT_STATUS` 와 **같은 값이어야
#: 한다** — 저쪽이 판매 가용을 가르는 기준이고, 여기서 다르게 읽으면 «팔 수 있다고
#: 센 재고» 와 «위험하다고 센 재고» 가 서로 다른 집합이 된다.
_ACTIVE = "ACTIVE"

#: 🟡 **Simulation Assumption 이다.** 정책 표에 이 경계를 담은 칸이 없다 — 지어내서
#: `agent_policy_config` 에 넣지 않고 탐지기 상수로 둔다. 정책으로 승격하는 날
#: `detector_version` 이 오른다 (상세설계 §26).
FRESHNESS_CRITICAL_REMAINING_DAYS = 1
#: 🟡 **Simulation Assumption.** 0.90(정책값) 이상이 MEDIUM, 여기부터 HIGH.
CAPACITY_HIGH_RATIO = Decimal("0.95")
#: 🟡 **Simulation Assumption.** 보장 용량을 다 쓴 상태.
#: ⚠️ `calculate_window_capacity_usage` 는 점유가 보장치를 넘으면 사용률을 1 에서
#:    멈춘다(cap 이 0 으로 클램프된다) — 그래서 이 구간은 «정확히 1» 이다.
CAPACITY_CRITICAL_RATIO = Decimal("1.00")

FRESHNESS_DETECTOR_VERSION = "v1"
CAPACITY_DETECTOR_VERSION = "v1"

#: 무엇이 닫았나 (`logistics_exceptions.resolved_by`).
#:
#: ⚠️ **원장 이동 ID(`MOVE-…` · `ALC-…`)를 적지 않는다.** Commit 2 는 Lot 별 이동을
#:    읽지 않고 관측 결과만 본다 — 없는 ID 를 지어내는 대신 **무엇이 닫았는지의 갈래**를
#:    적는다. 이동 ID 까지 적는 것은 전/후 비교를 만드는 Commit 6 의 일이다.
REDETECT = "REDETECT"
LOT_EMPTY = "LOT_EMPTY"
COMMITTED = "COMMITTED"
ESCALATED_FRESHNESS_EXPIRED = "ESCALATED:FRESHNESS_EXPIRED"


@dataclass(frozen=True)
class DetectorOutcome:
    """탐지기 하나가 이번에 한 일. 🔴 **`ran` 이 거짓이면 그 코드는 닫지도 않는다.**

    ```text
    ran=True   재 봤다 — 여기 없는 살아 있는 행은 «조건이 사라진 것»
    ran=False  못 쟀다 — 기준·입력이 없었다. 기존 행을 건드리지 않는다
    ```
    """

    code: ExceptionCode
    ran: bool
    conditions: tuple[DetectedCondition, ...] = ()
    skipped: str = ""


# ---------------------------------------------------------------------------
# ① FRESHNESS_PRESSURE — Core 메인
# ---------------------------------------------------------------------------


def detect_freshness_pressure(observation: WarehouseObservation) -> DetectorOutcome:
    """아직 팔 수 있는데 **시간이 얼마 안 남은** Lot. Lot 단위다.

    ```text
    status == 'ACTIVE'                              팔 수 있는 재고만
    remaining_freshness_days > 0                    🔴 만료는 이 문제가 아니다 (§7.1 E)
    remaining / effective_limit <= 0.30             기존 신호와 **같은 비교**
    uncommitted_kg > 0                              이미 팔린 Lot 을 또 팔라고 하지 않는다
    ```

    🔴 **`uncommitted_kg` 조건이 이 탐지기의 핵심이다.** 없으면 이미 판매 확정·할당된
       Lot 까지 *"빨리 파세요"* 로 올라오고, 사람이 매일 같은 제안을 지우게 된다.
       ⚠️ 그 값이 `None` 인 Lot 은 **셈을 못 한 것**이라 조건을 세우지 않는다 —
       예약 축을 못 읽었거나(관측의 `uncertainties`), 애초에 판매 가용이 아닌 Lot 이다.

    ★ **비율의 분모는 유효 한계다** (`중` 등급 계수 반영). 원값을 쓰면 갓 입고된
      중 등급이 즉시 임박으로 잡힌다 — `tools.collect_freshness_lot_census` 가 같은
      이유로 같은 분모를 쓴다.
    """
    임계 = observation.policy.freshness_pressure_ratio
    if 임계 is None:
        return DetectorOutcome(
            code=FRESHNESS_PRESSURE, ran=False, skipped=FRESHNESS_PRESSURE_POLICY_UNRESOLVED
        )

    조건들: list[DetectedCondition] = []
    for lot in observation.lots:
        if lot.status != _ACTIVE:
            continue
        잔여 = lot.remaining_freshness_days
        if 잔여 is None or 잔여 <= 0:
            continue
        비율 = lot.freshness_remaining_ratio
        if 비율 is None:
            continue
        # 🔴 **경계(`<=`)의 주인은 `count_freshness_risk_lots` 하나다.** 여기서
        #    `비율 <= 임계` 를 다시 적으면, 그 함수의 경계가 바뀌는 날 *"위험 Lot 3건"*
        #    이라고 답한 회신과 Exception 이 서로 다른 수를 세게 된다.
        if count_freshness_risk_lots([비율], 임계) == 0:
            continue
        미확정 = lot.uncommitted_kg
        if 미확정 is None or 미확정 <= 0:
            continue
        조건들.append(_freshness_condition(lot, ratio=비율, threshold=임계, remaining=잔여))
    return DetectorOutcome(code=FRESHNESS_PRESSURE, ran=True, conditions=tuple(조건들))


def _freshness_severity(*, remaining: int, sell_priority_remaining_days: int | None) -> Severity:
    """얼마나 급한가. 🟡 **구간이 Simulation Assumption 이다.**

    ```text
    remaining <= 1                              CRITICAL  내일이면 못 판다
    remaining <= sell_priority_remaining_days   HIGH      회전 정책이 «우선 팔라» 고 한 구간
    그 밖의 압박 구간                            MEDIUM
    ```

    ★ **HIGH 의 경계만 정책 표에서 온다** (`item_turnover_policies` · 배추 3 · 무 4 ·
      양파 7). 🔴 정책이 없는 품목(실측 5 중 2)은 그 경계를 **지어내지 않고** MEDIUM 에
      머문다 — 모르는 것을 더 급하다고도 덜 급하다고도 말하지 않는다.
    """
    if remaining <= FRESHNESS_CRITICAL_REMAINING_DAYS:
        return "CRITICAL"
    if sell_priority_remaining_days is not None and remaining <= sell_priority_remaining_days:
        return "HIGH"
    return "MEDIUM"


def _freshness_condition(
    lot: ObservedLot, *, ratio: Decimal, threshold: Decimal, remaining: int
) -> DetectedCondition:
    한계 = lot.effective_freshness_limit_days
    assert 한계 is not None  # 비율이 섰다는 것이 곧 한계가 있다는 뜻이다
    근거 = [
        ExceptionEvidence(
            fact="remaining_freshness_days",
            value=Decimal(remaining),
            unit="일",
            source="inventory_lots+item_storage_policies",
            source_id=lot.lot_id,
            observed_as_of=lot.observed_as_of,
        ),
        ExceptionEvidence(
            fact="effective_freshness_limit_days",
            value=Decimal(한계),
            unit="일",
            source="item_storage_policies",
            source_id=lot.item,
            observed_as_of=POLICY_OBSERVED_AS_OF,
        ),
        ExceptionEvidence(
            fact="freshness_remaining_ratio",
            value=ratio,
            unit="비율",
            source="tool_calc:collect_freshness_lot_census",
            source_id=lot.lot_id,
            observed_as_of=POLICY_OBSERVED_AS_OF,
        ),
        ExceptionEvidence(
            fact="freshness_pressure_ratio",
            value=threshold,
            unit="비율",
            source="agent_policy_config",
            source_id="freshness_pressure_ratio",
            observed_as_of=POLICY_OBSERVED_AS_OF,
        ),
        ExceptionEvidence(
            fact="remaining_qty_kg",
            value=lot.remaining_qty_kg,
            unit="kg",
            source="inventory_lots",
            source_id=lot.lot_id,
            observed_as_of=lot.observed_as_of,
        ),
        ExceptionEvidence(
            fact="uncommitted_kg",
            value=lot.uncommitted_kg if lot.uncommitted_kg is not None else Decimal(0),
            unit="kg",
            source="tool_calc:_sellable_lot_contributions",
            source_id=lot.lot_id,
            observed_as_of=POLICY_OBSERVED_AS_OF,
        ),
    ]
    if lot.sell_priority_remaining_days is not None:
        근거.append(
            ExceptionEvidence(
                fact="sell_priority_remaining_days",
                value=Decimal(lot.sell_priority_remaining_days),
                unit="일",
                source="item_turnover_policies",
                source_id=lot.item_id or lot.item,
                observed_as_of=POLICY_OBSERVED_AS_OF,
            )
        )
    return DetectedCondition(
        code=FRESHNESS_PRESSURE,
        subject_type="LOT",
        subject_id=lot.lot_id,
        severity=_freshness_severity(
            remaining=remaining, sell_priority_remaining_days=lot.sell_priority_remaining_days
        ),
        detector_version=FRESHNESS_DETECTOR_VERSION,
        evidence=tuple(근거),
        observed_as_of=derive_observed_as_of([one.observed_as_of for one in 근거]),
        note=f"{lot.item} {lot.lot_id} 잔여 {remaining}일 · 미확정 {lot.uncommitted_kg}kg",
    )


# ---------------------------------------------------------------------------
# ② CAPACITY_PRESSURE — Core 보조
# ---------------------------------------------------------------------------


def detect_capacity_pressure(observation: WarehouseObservation) -> DetectorOutcome:
    """창고가 빡빡하다. **창고 단위 하나**다.

    ```text
    calculate_window_capacity_usage >= capacity_tight_ratio
        기존 CAPACITY_TIGHT 와 **같은 함수 · 같은 비교**
    ```

    🔴 **새 용량 계산기를 만들지 않는다.** 창 구성(`as_of + lead` 부터 18일)도 사용률
       (`1 − min(cap)/guaranteed`)도 `tools` 소유이고, 이 탐지기는 **그 값을 받아
       임계와 견주기만** 한다.
    """
    사용률 = observation.capacity.window_usage_ratio
    임계 = observation.policy.capacity_tight_ratio
    if 임계 is None:
        return DetectorOutcome(
            code=CAPACITY_PRESSURE, ran=False, skipped=CAPACITY_TIGHT_POLICY_UNRESOLVED
        )
    if 사용률 is None:
        return DetectorOutcome(
            code=CAPACITY_PRESSURE, ran=False, skipped=CAPACITY_WINDOW_USAGE_UNRESOLVED
        )
    if 사용률 < 임계:
        return DetectorOutcome(code=CAPACITY_PRESSURE, ran=True)

    근거 = [
        ExceptionEvidence(
            fact="capacity_window_usage_ratio",
            value=사용률,
            unit="비율",
            source="tool_calc:calculate_window_capacity_usage",
            source_id=observation.sim_run_id,
            observed_as_of=POLICY_OBSERVED_AS_OF,
        ),
        ExceptionEvidence(
            fact="capacity_tight_ratio",
            value=임계,
            unit="비율",
            source="agent_policy_config",
            source_id="capacity_tight_ratio",
            observed_as_of=POLICY_OBSERVED_AS_OF,
        ),
        ExceptionEvidence(
            fact="used_capacity_kg",
            value=observation.capacity.used_kg,
            unit="kg",
            source="inventory_lots",
            source_id=observation.sim_run_id,
            # 🔴 물리 점유는 Lot 들이 만든 값이라 **Lot 축의 관측일**을 따라간다.
            observed_as_of=observation.observed_as_of,
        ),
    ]
    if observation.capacity.guaranteed_kg is not None:
        근거.append(
            ExceptionEvidence(
                fact="guaranteed_capacity_kg",
                value=observation.capacity.guaranteed_kg,
                unit="kg",
                source="agent_policy_config",
                source_id="guaranteed_capacity_kg",
                observed_as_of=POLICY_OBSERVED_AS_OF,
            )
        )
    조건 = DetectedCondition(
        code=CAPACITY_PRESSURE,
        subject_type="WAREHOUSE",
        subject_id=WAREHOUSE_SUBJECT_ID,
        severity=_capacity_severity(사용률),
        detector_version=CAPACITY_DETECTOR_VERSION,
        evidence=tuple(근거),
        observed_as_of=derive_observed_as_of([one.observed_as_of for one in 근거]),
        note=f"창 사용률 {사용률} (임계 {임계})",
    )
    return DetectorOutcome(code=CAPACITY_PRESSURE, ran=True, conditions=(조건,))


def _capacity_severity(usage: Decimal) -> Severity:
    """🟡 **구간이 Simulation Assumption 이다** — 정책 표에는 임계 하나(0.90)뿐이다."""
    if usage >= CAPACITY_CRITICAL_RATIO:
        return "CRITICAL"
    if usage >= CAPACITY_HIGH_RATIO:
        return "HIGH"
    return "MEDIUM"


#: 도는 순서. 🔴 **메인이 먼저다** — 요약 한 줄을 읽는 사람이 먼저 볼 것이 신선도다.
DETECTORS: tuple[Callable[[WarehouseObservation], DetectorOutcome], ...] = (
    detect_freshness_pressure,
    detect_capacity_pressure,
)


# ---------------------------------------------------------------------------
# 탐지 한 번 — 표에 옮긴다
# ---------------------------------------------------------------------------


def detect_logistics_exceptions(
    conn: Any,
    *,
    sim_run_id: str,
    as_of: date,
    phase: DetectPhase,
    observe_fn: Callable[..., WarehouseObservation] = observe,
) -> DetectOut:
    """물류 점검 한 칸. 🔴 **커밋도 롤백도 안 한다 — 트랜잭션 주인은 마스터다.**

    ```text
    AFTER_INBOUND    입고로 점유가 뛴 직후 · 하루 경과 반영 → 열거나 갱신한다
    AFTER_OUTBOUND   그날 출고·할당·폐기가 끝난 뒤        → 열고 갱신하고 **닫는다**
    ```

    ★ **왜 닫는 자리가 출고 뒤 하나인가.** 그날 조건을 없앤 사건(예약·출고 OUT·폐기)이
      다 끝난 뒤에 닫아야 *"N일째"* 가 정확하다. 입고 직후에 닫으면 그날 나갈 재고를
      보기도 전에 «해결됐다» 고 적게 된다.

    :raises Exception: 그대로 올린다. 하루를 계속 살리는 것은 `master/inspection.py`
        의 일이고, 여기서 삼키면 **반쯤 쓴 트랜잭션이 커밋된다.**
    """
    observation = observe_fn(conn, sim_run_id=sim_run_id, as_of=as_of)
    outcomes = [detector(observation) for detector in DETECTORS]

    조건들: dict[tuple[str, str, str], DetectedCondition] = {}
    for outcome in outcomes:
        for 조건 in outcome.conditions:
            조건들[조건.dedupe_key] = 조건
    돈코드 = {outcome.code for outcome in outcomes if outcome.ran}

    살아있는 = {row.dedupe_key: row for row in observation.open_exceptions}
    연것: list[str] = []
    갱신: list[str] = []
    닫힌것: list[str] = []

    for key, 조건 in 조건들.items():
        기존 = 살아있는.get(key)
        if 기존 is not None:
            touch_exception(
                conn,
                exception_id=기존.exception_id,
                severity=조건.severity,
                evidence=조건.evidence,
                last_detected_as_of=as_of,
                observed_as_of=조건.observed_as_of,
            )
            갱신.append(기존.exception_id)
            continue
        연것.append(_open(conn, sim_run_id=sim_run_id, as_of=as_of, condition=조건))

    if phase == "AFTER_OUTBOUND":
        for key, row in 살아있는.items():
            if key in 조건들 or row.code not in 돈코드:
                # 🔴 **못 잰 코드는 닫지 않는다.** 기준이 없어 안 본 것을
                #    *"해결됐다"* 로 적으면 그 문제는 아무도 다시 못 찾는다.
                continue
            사유, 비고 = _resolution(row, observation)
            resolve_exception(
                conn, exception_id=row.exception_id, as_of=as_of, resolved_by=사유, note=비고
            )
            닫힌것.append(row.exception_id)

    건너뛴 = tuple(f"{outcome.code}:{outcome.skipped}" for outcome in outcomes if not outcome.ran)
    손댔나 = bool(연것 or 갱신 or 닫힌것)
    return DetectOut(
        as_of=as_of,
        phase=phase,
        status="RAN" if 손댔나 else "NOTHING_DUE",
        opened=tuple(연것),
        updated=tuple(갱신),
        resolved=tuple(닫힌것),
        reason=(
            f"연 것 {len(연것)} · 갱신 {len(갱신)} · 닫은 것 {len(닫힌것)}"
            if 손댔나
            else f"확인했고 손댈 것이 없었다 (Lot {len(observation.lots)})"
        ),
        uncertainties=tuple(dict.fromkeys([*observation.uncertainties, *건너뛴])),
    )


def _open(conn: Any, *, sim_run_id: str, as_of: date, condition: DetectedCondition) -> str:
    """새 문제 한 줄. **재발이면 이전 행을 가리킨다** (재오픈하지 않는다)."""
    이전 = previous_exception_id_for(
        conn,
        sim_run_id=sim_run_id,
        code=condition.code,
        subject_type=condition.subject_type,
        subject_id=condition.subject_id,
    )
    exception_id = exception_id_for(
        conn,
        sim_run_id=sim_run_id,
        code=condition.code,
        subject_id=condition.subject_id,
        opened_as_of=as_of,
    )
    open_exception(
        conn,
        row=ExceptionRow(
            exception_id=exception_id,
            sim_run_id=sim_run_id,
            code=condition.code,
            subject_type=condition.subject_type,
            subject_id=condition.subject_id,
            severity=condition.severity,
            status="OPEN",
            opened_as_of=as_of,
            last_detected_as_of=as_of,
            observed_as_of=condition.observed_as_of,
            evidence=condition.evidence,
            detector_version=condition.detector_version,
            previous_exception_id=이전,
            note=condition.note or None,
        ),
    )
    return exception_id


def _resolution(row: ExceptionRow, observation: WarehouseObservation) -> tuple[str, str | None]:
    """**무엇이 닫았나.** 🔴 원장 이동 ID 를 지어내지 않는다 — 갈래만 적는다.

    ```text
    LOT_EMPTY                     그 Lot 이 관측에서 사라졌다 (출고 · 폐기로 잔량 0)
    ESCALATED:FRESHNESS_EXPIRED   잔량은 남았는데 신선도가 다했다 (§7.1 E)
    COMMITTED                     할당이 잔량을 다 덮었다 — «위험 관리 상태» (§7.1 C)
    REDETECT                      그 밖 (용량 회복 등 · 조건이 그냥 거짓이 됐다)
    ```

    ⚠️ **`ESCALATED:` 가 후속 Exception 을 만들지 않는다.** `FRESHNESS_EXPIRED` 탐지기는
       Commit 2 에 없다 — 넘어갔다는 **사실만** 남기고, 잔량이 남은 만료 재고를 실제로
       다루는 것은 기존 자동 유지보수(`auto_maintenance`)와 사람의 폐기 확정이다.
    """
    if row.code != FRESHNESS_PRESSURE:
        return REDETECT, None
    lot = next((one for one in observation.lots if one.lot_id == row.subject_id), None)
    if lot is None:
        return LOT_EMPTY, "관측에서 사라졌다 — 잔량 0"
    잔여 = lot.remaining_freshness_days
    if 잔여 is not None and 잔여 <= 0:
        return ESCALATED_FRESHNESS_EXPIRED, f"잔여 {잔여}일 · 잔량 {lot.remaining_qty_kg}kg 남음"
    if lot.uncommitted_kg is not None and lot.uncommitted_kg <= 0:
        return COMMITTED, "살아 있는 할당이 잔량을 다 덮었다"
    return REDETECT, None
