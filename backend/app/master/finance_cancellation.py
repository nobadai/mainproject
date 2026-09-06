"""
finance_cancellation.py — 마스터 `ApprovalCancellation` 을 재무 취소 함수에 잇는 배선.

🔴 **왜 `app/finance/` 가 아니라 여기인가.**

  처음에 `app/finance/cancellation_adapter.py` 로 썼더니 재무의 경계 검사 둘이
  울었다 —

  ```text
  test_finance_touches_master_only_through_shared_contract_modules
    재무가 마스터에서 아는 것은 app.master.envelope · app.master.critic_bridge 뿐이다
    이 어댑터가 app.master.commitment 를 임포트한다        🔴
  ```

  ★ **재무가 옳다.** 어댑터는 *"마스터가 재무를 어떻게 부르는가"* 이지 재무 지식이
    아니다. **배선은 조율자 몫**이고, 재무 모듈이 마스터를 알기 시작하면 화살표가
    양방향이 된다.

  ⚠️ 물류 쪽(`app/logistics/cancellation.py`)은 다른 자리에 둔 이유가 있다 — 그쪽은
    *"어떻게 걷는가"* 라는 **물류 도메인 규칙**을 담는다(마스터가 대신 썼을 뿐이다).
    여기는 **이름 하나를 옮기는 것**이 전부다.

★ **재무가 그렇게 하겠다고 적었다** (회신 2026-09-06 `§5`) —
  *"취소 관통 구현 시 Adapter 경계만 새 Protocol 에 맞추겠습니다."*

🔴 **이 파일이 하는 일은 이름 하나를 옮기는 것뿐이다.**

```text
마스터 Protocol   cancel(conn, *, commitment, cancelled_on, target_state_date, purchase_ids)
재무 함수         cancel_finance_payables(conn, *, purchase_ids, as_of, target_state_date)

cancelled_on  →  as_of        재무 내부의 as_of 는 **취소 사건일** 의미다 (회신 §1)
purchase_ids  →  values()     재무는 Sequence[str] 를 받는다 (seq 는 안 본다)
```

⚠️ **`commitment.as_of` 를 넘기지 않는다.** 그것은 **원 승인일**이고, 재무가 그 값을
  취소일로 받으면 **과거 상태를 고치게 된다** — 그 날에는 실제로 미지급이 있었다.
  재무가 직접 짚어 준 자리다 (회신 `§4`).

⚠️ **`target_state_date - 1` 로 역산하지 않는다.** 지금은 그 뺄셈이 맞지만 규칙이
  바뀌는 날 취소일이 조용히 따라 틀린다 — 같은 사실을 두 곳에서 만들지 않는다.

★ **결과를 버린다.** `FinanceCancellationResult` 는 *"이번에 실제로 물린 금액"* 을
  말하는데, 마스터 `CancellationOut` 은 지금 그 칸이 없다. **버리는 것을 적어 둔다** —
  나중에 화면이 *"3,063,298원이 물렸습니다"* 를 쓰려면 여기서 위로 올려야 한다.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from app.finance.cancellation import cancel_finance_payables
from app.master.commitment import ApprovedCommitment

__all__ = ["FinanceCancellationAdapter"]


class FinanceCancellationAdapter:
    """`ApprovalCancellation` 구현. **생성 인자가 없다.**

    ★ 재무 취소는 `purchase_id` 로 행을 찾으므로 실행 정체성이 필요 없다 —
      `LogisticsCancellationAdapter` 가 `sim_run_id` 를 받는 것과 다른 점이다.
    """

    def cancel(
        self,
        conn: Any,
        *,
        commitment: ApprovedCommitment,
        cancelled_on: date,
        target_state_date: date,
        purchase_ids: Mapping[int, str],
        financing_mode: str,
    ) -> None:
        """🟡 **`financing_mode` 를 받되 아직 안 넘긴다.**

        재무가 *"Master 가 전달한 축으로 Finance cancellation 도 exact lookup 하겠다"*
        고 확정했지만(2026-09-06), `cancel_finance_payables` 의 시그니처가 아직 그
        인자를 안 받는다. **재무 파일이라 마스터가 안 고친다.**

        ★ **받는 자리를 먼저 뚫어 둔다.** 재무가 여는 날 이 줄 하나만 켜면 되고, 그때까지
          마스터 호출부는 안 바뀐다 — 물류가 `purchase_ids=None` 을 기본값으로 열어
          두었던 것과 같은 순서다 (`#311` → `#313`).

        ⚠️ **안 쓰는 인자를 조용히 버리지 않는다.** 이 docstring 이 그 사실이고,
          검사가 *"받기는 받는다"* 를 잠근다.
        """
        ids = [purchase_ids[seq] for seq in sorted(purchase_ids)]
        if not ids:
            # ★ 회차 일정이 없던 약정도 승인은 살아 있다 — 물릴 채무가 **없다**는 것은
            #   정상 상태다. 빈 목록을 재무에 넘기면 재무가 *"요청 집합이 비었다"* 로
            #   판단할 자리를 만들게 되므로 여기서 멈춘다.
            return
        cancel_finance_payables(
            conn,
            purchase_ids=ids,
            # 🔴 **취소 사건일이다.** commitment.as_of(승인일)가 아니다.
            as_of=cancelled_on,
            target_state_date=target_state_date,
            # 🟡 재무가 받는 쪽을 열면 여기에 financing_mode=financing_mode 한 줄
        )
