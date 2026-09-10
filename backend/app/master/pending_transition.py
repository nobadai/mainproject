"""pending_transition.py — **승인은 났는데 원장에 안 닿은 것을 다시 세운다** (2026-09-11).

```text
개장 → **미적용 전이 재시도** → 입고 → 채권 → 수금 → [장부 관문] → …
```

★★ **왜 이 단계가 생겼나 — 모든 승인이 같은 모양으로 실패했다.**

  ```text
  실측 2026-09-11 · SIM-WALK-2026-APPROVED · 2026-01-01 ~ 01-09
    종료코드  E1_APPROVED 15
    승인어휘  RECORDED 15
    🔴 원장    purchases 0행 · purchase_items 0행 · inventory_lots 0행
  ```

  승인 하나를 골라 전이를 그대로 재현하면 이렇게 선다.

  ```text
  약정 조립   buildable=True
  전이        FAILED
    전이 적재 실패: 갱신할 물류 runtime fixture 행이 없다
    (sim_run_id=…, as_of=2026-01-10, usage_scope=AGENT_MVP_DEMO)
  ```

  🔴 **하루가 어긋난다.** 승인이 선 날은 01-09 이고 상태가 설 날은 01-10 인데
  (`transition._target_state_date` — 승인일 다음 날), 걷기는 날짜 순으로 돌므로
  **01-10 행은 다음 차례에** 열린다. 전이는 **언제나 하루 앞을 본다.**

  🟢 **물류가 그 행을 미리 안 만드는 것은 옳다.** `evidence_grade` · `approved_by` ·
  나머지 두 status 는 물류의 주장이고, 전이가 지어내면 **없는 근거가 물류 표에
  앉는다** (`logistics/transition.py` 의 `LogisticsFixtureMissing` 원문).

  ★★ **그래서 고칠 자리는 물류도 승인도 아니라 「언제 다시 부르나」다.** 도착일이
  열린 날 — 즉 그 날 개장이 그 행을 세운 **직후** — 에 다시 한 번 세우면 된다.

---

🔴 **개장 바로 뒤, 입고 앞이다.**

  그날 도착 행이 방금 열렸고, **입고가 그 도착을 잡아야** 하기 때문이다. 뒤로
  가면 그날 도착이 하루 더 밀린다 (`scheduler.run_scheduled_day` 의 하루 순서).

🔴 **미적용 목록을 새 표에 들고 있지 않는다.**

  ```text
  🟢 이미 있는 사실로 찾는다   승인된 결정 중 **원장에 안 닿은 것**
  ✗  미적용 목록을 새 표에
  ```

  새 표를 만들면 같은 사실의 주인이 둘이 되고, 한쪽만 고치는 날 갈린다. 승인은
  `master_decisions` 에 있고 원장은 `purchases` 에 있으니 **둘을 맞대면 답이 나온다**
  (`pending_transition_repository` 가 두 목록을 가져오고, 맞대는 것은 아래
  `pending_approvals` 다).

🔴 **재시도가 하루를 죽이지 않는다.** 또 실패하면 **세어서 요약에 올리고 하루는
   계속 간다.** `apply_approval` 이 이미 예외 대신 값을 돌려주고 (승인한 사실이
   전이 실패로 지워지면 안 되니까), 이 파일도 같은 태도다.

⚠️ **`apply_approval` 의 태도를 바꾸지 않는다.** 여기서 예외로 올리면 그 규율이
  이 자리 하나만 안 지나게 된다.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from app.master.commitment import ApprovedCommitment
from app.master.decision_service import current_approved_commitment
from app.master.pending_transition_repository import approved_decisions, ledger_purchase_ids
from app.master.transition import TransitionOut, apply_approval, purchase_id_prefix_for

__all__ = [
    "PendingApproval",
    "RetriedTransition",
    "RetryOut",
    "RetryOutcome",
    "RetryStatus",
    "pending_approvals",
    "retry_pending_transitions",
]


RetryOutcome = Literal["APPLIED", "NOT_APPLIED", "FAILED", "NOT_BUILDABLE"]
"""미적용 승인 하나를 다시 세운 결과. 🔴 **넷을 접지 않는다.**

```text
APPLIED         원장에 닿았다 — 이 판이 노리는 값이다
NOT_APPLIED     아직 쓸 것이 없다 (어댑터 미등록 · 회차 금액 미기재 · 도착분 없음)
FAILED          쓰려다 터졌다 — 아무것도 안 바뀌었다
NOT_BUILDABLE   약정을 못 만들어 전이를 부를 수조차 없었다
```

★ **앞의 셋은 `TransitionOut.status` 그대로다** — 여기서 새 이름을 붙이지 않는다.

🔴 **`NOT_BUILDABLE` 을 `FAILED` 로 접지 않는다.** 앞은 *"승인 응답에서 안을 못
  찾았다 · 리드타임이 없다"* 처럼 **전이 앞에서 끝난 일**이고 뒤는 *"장부를 바꾸려다
  터졌다"* 다 — 고칠 곳이 서로 다르다.
"""

RetryStatus = Literal["RAN", "NOTHING_DUE", "FAILED"]
"""단계 하나가 어떻게 됐나. 🔴 **`RetryOutcome` 과 축이 다르다** — 저쪽은 승인 하나다.

```text
RAN           미적용을 찾아 다시 세웠다 — **전부 성공했다는 뜻이 아니다**
NOTHING_DUE   확인했고 미적용이 없었다 — 🟢 정상이다
FAILED        찾다가 터졌다 (조회가 안 됐다) — 미적용이 있었는지조차 모른다
```

★ **어휘를 새로 만들지 않았다.** 셋 다 하루 순서가 이미 쓰는 말이다
  (`InboundOut` · `CollectionOut` · `DayRunOutcome`).

🔴 **`NOTHING_DUE` 와 `FAILED` 를 접지 않는다.** *"미적용이 없다"* 와 *"미적용이
  있었는지 못 물어봤다"* 는 다르고, 접으면 조회가 죽은 날 이 단계가 조용해진다.
"""


@dataclass(frozen=True)
class PendingApproval:
    """승인은 났는데 **매입 원장에 안 닿은** 결정 하나."""

    request_id: str
    decision_seq: int
    #: 승인이 선 날 (실행 이력 행의 `as_of`).
    as_of: date
    #: 어느 실행의 장부인가. 🔴 **실행 이력 행이 정본이다** — 여기서 짓지 않는다.
    sim_run_id: str


@dataclass(frozen=True)
class RetriedTransition:
    """미적용 하나를 다시 세운 결과. **터진 것도 값으로 남는다.**"""

    request_id: str
    decision_seq: int
    as_of: date
    outcome: RetryOutcome
    #: 왜 그 결과가 됐나. `APPLIED` 에는 없다.
    reason: str = ""


@dataclass(frozen=True)
class RetryOut:
    """재시도 한 번의 결과. **예외 대신 이것을 돌려준다.**"""

    status: RetryStatus
    #: 단계가 왜 그렇게 됐나. `_stage` 가 note 로 싣는 자리다.
    reason: str = ""
    #: 다시 세워 본 승인마다 하나씩. **본 순서 그대로.**
    retried: tuple[RetriedTransition, ...] = field(default_factory=tuple)

    @property
    def outcomes(self) -> Mapping[str, int]:
        """결과 분포. **넷을 그대로 센다** — 새 이름을 안 붙인다.

        ★ `BackfillOut.outcomes` 와 같은 모양이다. 요약이 이 값을 그대로 싣는다.
        """
        counted: dict[str, int] = {}
        for one in self.retried:
            counted[one.outcome] = counted.get(one.outcome, 0) + 1
        return counted


def pending_approvals(
    *,
    decisions: Iterable[Mapping[str, Any]],
    purchase_ids: Iterable[str],
    before: date,
) -> tuple[PendingApproval, ...]:
    """승인 목록과 원장 ID 목록을 **맞대어** 미적용을 고른다. 🔴 **순수 함수다.**

    ★★ **어떻게 찾았는지가 이 함수 세 줄이다.**

      ```text
      승인 하나가 원장에 남기는 행의 ID    PUR-{request_id}-D{decision_seq}-S{회차}
      그 앞머리                            purchase_id_prefix_for(request_id, seq)
      앞머리로 시작하는 purchase_id 가
        하나라도 있다   → 닿았다 (다시 안 한다)
        하나도 없다     → 🔴 미적용이다
      ```

    🔴 **앞머리를 여기서 짓지 않는다.** `transition.purchase_id_prefix_for` 를 부른다 —
       ID 규칙의 주인은 그쪽 하나이고, 여기 문자열을 복사하면 규칙이 바뀌는 날 이
       함수가 **에러 없이 늘 0건**을 돌려준다. 그러면 이 단계는 있으나 마나가 된다
       (`tests/master/test_pending_transition.py` 가 그 짝을 잠근다).

    ★ **회차 번호를 안 본다.** 회차는 약정을 조립해야 나오는데, 조립하기 전에
      먼저 걸러야 한다 — 그래서 앞머리까지만으로 판정한다. 회차 하나라도 서 있으면
      그 승인은 원장에 **닿은** 것이고, 반쪽만 선 승인은 이 단계가 아니라
      `apply_approval` 의 한 트랜잭션이 막는 자리다.

    :param before: 이 날보다 **앞선 날**의 승인만 본다. 🔴 **오늘 것을 빼는 이유.**
        상태가 설 날은 승인일 **다음 날**이라(`transition._target_state_date`) 오늘
        승인은 오늘 세울 자리가 없다. 그리고 같은 범위를 **다시 걸을 때** 이 줄이
        없으면 첫날 재시도가 **아직 오지 않은 날의 승인**까지 장부에 밀어 넣는다.
    """
    applied = tuple(purchase_ids)
    found: list[PendingApproval] = []
    for row in decisions:
        as_of = row["as_of"]
        if as_of >= before:
            continue
        request_id = row["request_id"]
        decision_seq = int(row["decision_seq"])
        prefix = purchase_id_prefix_for(request_id, decision_seq)
        if any(one.startswith(prefix) for one in applied):
            # 🔴 **이미 닿았다. 다시 안 한다** — 멱등이 이 한 줄이다. 다시 하면
            #    같은 승인이 두 번 앉고, 되돌리는 경로가 저장소에 없다.
            continue
        found.append(
            PendingApproval(
                request_id=request_id,
                decision_seq=decision_seq,
                as_of=as_of,
                sim_run_id=row["sim_run_id"],
            )
        )
    return tuple(found)


def retry_pending_transitions(
    as_of: date,
    *,
    sim_run_id: str,
    decisions_of: Callable[..., Sequence[Mapping[str, Any]]] = approved_decisions,
    purchase_ids_of: Callable[..., Sequence[str]] = ledger_purchase_ids,
    commitment_of: Callable[[str], ApprovedCommitment | None] = current_approved_commitment,
    apply_fn: Callable[..., TransitionOut] = apply_approval,
) -> RetryOut:
    """미적용 전이를 **다시 세운다.** 🔴 **예외를 밖으로 내지 않는다.**

    ```text
    1. 두 목록을 가져온다   승인 · 원장 ID
    2. 맞대어 미적용을 고른다 (pending_approvals)
    3. 하나씩 약정을 재조립하고 apply_approval 을 다시 부른다
    4. 결과를 세어 값으로 돌려준다 — **하루는 계속 간다**
    ```

    🔴 **하나가 터져도 나머지를 계속 세운다.** 하나 때문에 멈추면 그 뒤의 승인이
       전부 미적용으로 남고, 다음 날 같은 자리에서 또 멈춘다.

    🔴 **약정을 새로 적재하지 않는다.** `decision_service.current_approved_commitment`
       이 **승인 시점의 실행으로 재조립**한다 — 같은 입력·같은 코드라 승인 응답에
       실렸던 것과 같은 값이 나온다.

    ★ **축을 지어내지 않는다.** 승인 행이 든 `sim_run_id` 를 그대로 넘긴다 — 조회가
      이미 축으로 좁혔으므로 받은 값과 같지만, 값의 주인은 실행 이력 행이다.

    :param as_of: 오늘. 🔴 **오늘보다 앞선 날의 승인만 본다** (`pending_approvals`
        의 `before`). 오늘 승인은 상태가 설 날이 내일이라 오늘 세울 자리가 없고,
        같은 범위를 다시 걸을 때 미래의 승인을 끌어오지 않게 막는 줄이기도 하다.
    :param commitment_of: 약정을 재조립하는 자리. 🔴 **기본값이 실제 함수 자체다**
        (`clock.py` · `verifier.py` 와 같은 규율) — `None` 을 안 받는다.
    :param apply_fn: 전이 경계. 기본이 `apply_approval` 자체다.
    """
    try:
        found = pending_approvals(
            decisions=decisions_of(sim_run_id=sim_run_id),
            purchase_ids=purchase_ids_of(sim_run_id=sim_run_id),
            before=as_of,
        )
    except Exception as exc:  # noqa: BLE001 - 조회가 터져도 하루는 계속 간다.
        # 🔴 **`NOTHING_DUE` 로 접지 않는다.** 미적용이 없는 것과 있었는지 못 물어본
        #    것은 다르고, 접으면 조회가 죽은 날 이 단계가 조용해진다.
        return RetryOut(
            status="FAILED",
            reason=f"미적용을 못 찾았다: {type(exc).__name__}: {exc}",
        )

    if not found:
        # 🟢 **정상이다.** 승인이 다 닿았거나 아직 승인이 없다.
        return RetryOut(status="NOTHING_DUE", reason="미적용 전이가 없다")

    retried = tuple(
        _retry_one(one, commitment_of=commitment_of, apply_fn=apply_fn) for one in found
    )
    return RetryOut(
        status="RAN",
        reason=f"미적용 {len(retried)}건을 다시 세웠다",
        retried=retried,
    )


def _retry_one(
    pending: PendingApproval,
    *,
    commitment_of: Callable[[str], ApprovedCommitment | None],
    apply_fn: Callable[..., TransitionOut],
) -> RetriedTransition:
    """미적용 하나. **예외를 값으로 옮긴다** (`scheduler._stage` 와 같은 태도)."""

    def 결과(outcome: RetryOutcome, reason: str = "") -> RetriedTransition:
        return RetriedTransition(
            request_id=pending.request_id,
            decision_seq=pending.decision_seq,
            as_of=pending.as_of,
            outcome=outcome,
            reason=reason,
        )

    try:
        commitment = commitment_of(pending.request_id)
    except Exception as exc:  # noqa: BLE001 - 하나가 터져도 나머지는 계속 선다.
        return 결과("FAILED", f"약정 재조립이 터졌다: {type(exc).__name__}: {exc}")
    if commitment is None:
        # ⚠️ **`FAILED` 가 아니다.** 약정을 못 만든 것은 전이 **앞에서** 끝난 일이고,
        #   그 사유는 `current_commitment` 의 `reason` 이 든다.
        return 결과("NOT_BUILDABLE", "승인이 현재 결정이 아니거나 약정을 못 만들었다")
    try:
        out = apply_fn(commitment, sim_run_id=pending.sim_run_id)
    except Exception as exc:  # noqa: BLE001 - 전이 실패가 적재된 결정을 지우면 안 된다.
        # ★ `apply_approval` 은 예외를 안 내겠다고 적어 뒀지만 여기서 한 번 더 잡는다 —
        #   하루의 진행이 그 약속에 걸리면 안 된다.
        return 결과("FAILED", f"전이가 터졌다: {type(exc).__name__}: {exc}")
    # ⚠️ **어휘를 접지 않고 그대로 적는다** — 무엇이 왜 안 닿았는지가 이 줄이다.
    return 결과(str(out.status), out.reason)  # type: ignore[arg-type]
