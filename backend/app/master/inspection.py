"""inspection.py — **물류 점검 칸을 마스터가 부른다** (2026-09-12 · #628 Commit 2).

```text
… → 입고 → **물류 점검 #1(AFTER_INBOUND)** → 채권 → 수금 → [장부 관문]
  → 매입 판단 → 매입 승인 → 판매 판단 → 판매 승인 → 출고
  → **물류 점검 #2(AFTER_OUTBOUND)** → 마감
```

★★ **없어서 아무도 «안 팔리는 재고가 있다» 고 말하지 않았다.** 실측:
  `SIM-WALK-2026-V4` 입고 39.5t 전량, `SIM-CHAIN-V8` 은 35.8t 중 9.2t 이 신선도
  만료로 폐기됐고 그 사이 그 사실을 **문제로 세운 행이 한 줄도 없었다.**

---

🔴 **업무 판단을 여기서 다시 적지 않는다.**

  무엇이 문제이고 무엇이 해소인지는 물류가 정했다 (`logistics/agent/detect.py` —
  신선도 압박 · 용량 압박 · 중복 방지 · 해소 갈래). 이 파일이 지는 것은
  **커넥션 · 트랜잭션 · 어휘**뿐이다. 신선도 식도 용량 식도 임계 비교도 여기 없다.

  ★ `maintenance.py` 와 **같은 모양이다.** 저쪽도 무엇을 버릴지 안 고르고
    트랜잭션과 사유·행위자 어휘만 진다.

🔴 **트랜잭션의 주인이 이 파일이다.** `detect_logistics_exceptions` 는 커밋도
   롤백도 안 한다고 못박았다 — 그 약속의 반대편이 여기다.

  ★ **한 점검이 한 커밋이다.** Exception 마다 커밋하지 않는다. 한 번의 탐지가
    «연 것 · 갱신한 것 · 닫은 것» 을 함께 내는데 중간에 커밋하면, 터진 날 장부에
    **절반만 재탐지된 상태**가 남는다.

🔴 **터져도 하루는 계속 간다.** 예외를 값으로 옮긴다 (`_stage` · `run_auto_maintenance`
   와 같은 태도). 문제를 못 세운 것과 하루를 못 산 것은 다른 사실이다.

  ⚠️ 그래서 표(`logistics_exceptions`)가 아직 없는 DB 에서도 걷기가 돈다 —
    점검 두 칸이 `FAILED` 한 줄을 남기고 나머지 열세 칸은 그대로 간다.

🔴 **LLM 이 여기 없다.** 걷기가 하는 것은 Observe · Assess · Detect · Resolve
   결정론뿐이다 (상세설계 §17). 조사·제안은 사람이 부르는 별도 요청이고, 그래야
   `--reset` 뒤 같은 걷기가 같은 결과를 낸다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from app.logistics.agent.detect import detect_logistics_exceptions
from app.logistics.agent.schemas import DetectOut, DetectPhase
from app.logistics.db import get_connection

__all__ = [
    "AFTER_INBOUND",
    "AFTER_OUTBOUND",
    "InspectionOut",
    "run_logistics_inspection",
]

#: 입고 직후. **그날 점유가 뛴 자리**라 용량 압박이 여기서 잡힌다. 신선도도 함께 보는
#: 이유는 하루가 지났기 때문이다 — 그날 매입 판단이 그 사실을 볼 수 있어야 한다.
AFTER_INBOUND: DetectPhase = "AFTER_INBOUND"
#: 출고 직후. **그날 조건을 없앤 사건이 다 끝난 자리**라 해소(RESOLVED)가 여기서 난다.
AFTER_OUTBOUND: DetectPhase = "AFTER_OUTBOUND"


@dataclass(frozen=True)
class InspectionOut:
    """물류 점검 한 칸의 결과. **예외 대신 이것을 돌려준다.**

    ```text
    RAN           문제를 열었거나 갱신했거나 닫았다
    NOTHING_DUE   확인했고 손댈 것이 없었다 — 🟢 정상이다
    FAILED        보려다 터졌다 — 아무것도 안 바뀌었다
    ```

    ★ **어휘를 새로 만들지 않았다.** 셋 다 `MaintenanceOut.status` 그대로이고, 칸을
      안 탄 날의 `NOT_ATTEMPTED` 도 `DayRunOutcome` 이 이미 쓴다.

    🔴 **`NOTHING_DUE` 와 `FAILED` 를 접지 않는다.** *"문제가 없었다"* 와 *"문제가
       있었는지조차 못 물어봤다"* 는 다르고, 접으면 표가 없는 DB 에서 이 칸이 조용해진다.
    """

    as_of: date
    phase: str
    status: Literal["RAN", "NOTHING_DUE", "FAILED"]
    reason: str = ""
    #: 물류가 낸 값 **그대로**. 🔴 **접지 않는다** — 무엇을 열고 갱신하고 닫았는지의
    #: 주인은 `DetectOut` 하나다.
    result: DetectOut | None = None

    @property
    def counts(self) -> Mapping[str, int]:
        """연 것 · 갱신 · 닫은 것. **여기서 다시 세지 않는다.**"""
        return {} if self.result is None else self.result.counts


def run_logistics_inspection(
    as_of: date,
    *,
    sim_run_id: str,
    phase: DetectPhase,
    connect: Any = None,
    detect_fn: Callable[..., DetectOut] = detect_logistics_exceptions,
) -> InspectionOut:
    """그날 창고를 한 번 본다. 🔴 **예외를 밖으로 내지 않는다.**

    ★ **`receive_arrivals` · `run_auto_maintenance` 와 같은 모양이다** — `as_of` 를
      받고, 상태를 값으로 돌려주고, 하루의 진행을 자기 성공에 걸지 않는다.

    🔴 **무엇이 문제인지 여기서 안 고른다.** 탐지기도 임계도 `detect_fn` 의 것이다.

    🔴 **커밋이 여기 있다.** 물류가 안 하겠다고 못박은 그 일이다. 터지면 롤백하고
       `FAILED` 로 돌아선다 — 절반만 재탐지된 장부를 남기지 않는다.

    :param sim_run_id: 어느 실행의 창고인가. 🔴 **이번 실행 축으로만 돈다.**
    :param phase: 하루의 어느 자리인가. 🔴 **닫는 것은 `AFTER_OUTBOUND` 뿐이다** —
        그 규칙의 주인은 물류이고 여기서는 받은 값을 흘려보낸다.
    :param detect_fn: 물류 경계. 🔴 **기본값이 실제 함수 자체다** — `None` 을 안 받는다
        (`clock.py` · `maintenance.py` 와 같은 규율).
    """
    open_connection = get_connection if connect is None else connect
    try:
        conn = open_connection()
    except Exception as exc:  # noqa: BLE001 - 연결 실패가 그날을 통째로 세우면 안 된다.
        return InspectionOut(as_of=as_of, phase=phase, status="FAILED", reason=f"연결 실패: {exc}")

    try:
        try:
            result = detect_fn(conn, sim_run_id=sim_run_id, as_of=as_of, phase=phase)
        except Exception as exc:  # noqa: BLE001 - 점검이 터져도 하루는 계속 간다.
            conn.rollback()
            return InspectionOut(
                as_of=as_of,
                phase=phase,
                status="FAILED",
                reason=f"물류 점검이 터졌다: {type(exc).__name__}: {exc}",
            )

        # 🔴 **한 점검이 한 커밋이다.** 연 것과 닫은 것이 같은 재탐지의 두 면이라
        #    나눠 적으면 그 사이에 끊긴 날의 장부를 아무도 설명할 수 없다.
        conn.commit()
        return InspectionOut(
            as_of=as_of,
            phase=phase,
            status=result.status,
            reason=f"{result.reason}{_불확실(result)}",
            result=result,
        )
    finally:
        conn.close()


def _불확실(result: DetectOut) -> str:
    """🔴 **못 본 것을 사유에서 지우지 않는다.** 없으면 빈 문자열이다.

    ★ *"확인했고 문제 없음"* 과 *"기준이 없어 못 쟀다"* 가 요약에서 같아 보이면,
      정책 미등재가 안전 신호로 둔갑한다.
    """
    if not result.uncertainties:
        return ""
    return f" · 미확인 {list(result.uncertainties)}"
