"""부서 `DeptMeta` 가 맞춰야 하는 **검사 id 의 소유자는 마스터**다.

🔴 **실측 2026-09-01 — 제가 물류에 틀린 것을 물었다.**

> *"물류 회신의 `checks[]` 중 밴드 필드를 채우는 검사가 몇 개입니까?"*

**물류는 `checks[]` 를 내지 않는다.** `critic_bridge._replies_in` 이 부서 payload 에서
부서당 하나씩 **합성한다.** 답이 제 코드에 있었다.

그래서 이름의 주인이 마스터이고, 부서는 그 이름에 맞춰야 한다.

```python
used = dm.inputs_used.get(chk.check_id, ())      # 이름이 다르면 빈 튜플
leaked = FORBIDDEN_SCENARIO_INPUTS & set(used)   # 위반 없음 → 조용히 통과
```

**틀려도 에러가 안 난다.** 그래서 문자열을 양쪽에 두지 않고 `DEPT_CAP_CHECK_ID` 를
공개했다 — `MAX_PURCHASE_ATTEMPTS` 와 같은 처리다 ([[test_retry_cap_ownership]]).
"""

from __future__ import annotations

import json
from datetime import date

from app.master import critic_bridge as bridge
from app.master.critic_bridge import DEPT_CAP_CHECK_ID
from tests.master.test_critic_bridge import CONSTRAINTS, EVIDENCES, _proposal


def _request(observations=None):
    return bridge.build_request(
        as_of=date(2025, 12, 31),
        item="배추",
        proposal=_proposal(),
        constraints=CONSTRAINTS,
        evidences=EVIDENCES,
        observations=observations or {},
    )


def _dept_meta(dept: str, check_id: str, inputs: list[str], produced: list[str]) -> str:
    return json.dumps(
        {
            "observation_type": f"{dept}_dept_meta",
            "inputs_used": {check_id: inputs},
            "produced_fields": produced,
        }
    )


def test_합성된_검사_id_가_공개_상수와_같다():
    """부서가 이 상수를 import 해서 쓰면 이름이 갈릴 수 없다."""
    synthesized = {reply.dept: [c.check_id for c in reply.checks] for reply in _request().replies}

    for dept, check_id in DEPT_CAP_CHECK_ID.items():
        assert synthesized.get(dept) == [check_id], f"{dept} 합성 결과가 상수와 다르다"


def test_부서는_checks_를_내지_않는다():
    """★ 회신 payload 에 `checks` 가 없어도 합성이 된다 — 그게 소유자가 마스터인 근거."""
    for payload in CONSTRAINTS.values():
        assert "checks" not in payload

    assert [reply.dept for reply in _request().replies] == ["finance", "inventory"]


def test_물류_검사는_밴드를_채운다():
    """`E-SCENARIO-LEAK` 이 물류에 **실제로 걸리는** 근거.

    `_fills_band` 는 밴드 필드 중 하나라도 값이 있으면 참이다. 물류 합성 검사는
    `cap_total_kg`·`cap_by_date_kg` 를 채운다 — 자문 검사가 아니라 밴드 검사다.
    """
    from app.critic.critic_v0_4 import _fills_band

    inventory = next(r for r in _request().replies if r.dept == "inventory")
    check = inventory.checks[0]

    assert _fills_band(check), "밴드 검사가 아니면 E-SCENARIO-LEAK 이 안 걸린다"
    assert check.cap_total_kg is not None or check.cap_by_date_kg


def test_이름이_맞으면_입력이_검사에_닿는다():
    """★ **검사가 공허하지 않다는 증명 (맞는 쪽).**"""
    from app.critic.service import run_critic_procurement

    observations = {
        "inventory": (
            _dept_meta(
                "inventory",
                DEPT_CAP_CHECK_ID["inventory"],
                ["scenarios"],  # 밴드 검사가 매입 시나리오를 읽었다 — 위반
                ["warehouse_free_kg"],
            ),
        )
    }
    verdict = run_critic_procurement(_request(observations))

    assert any("E-SCENARIO-LEAK" in f.check_id for f in verdict.findings), verdict.findings


def test_이름이_틀리면_같은_위반이_조용히_통과한다():
    """🔴 **이 테스트가 상수를 공개한 이유다.**

    입력도 위반도 위와 **완전히 같은데** 키 하나가 달라서 아무 일도 안 일어난다.
    에러도 경고도 없다 — 그래서 부서가 문자열을 베끼면 안 된다.
    """
    from app.critic.service import run_critic_procurement

    observations = {
        "inventory": (
            _dept_meta(
                "inventory",
                "warehouse_cap_check",  # ← 한 글자가 아니라 접미사 하나 차이
                ["scenarios"],
                ["warehouse_free_kg"],
            ),
        )
    }
    verdict = run_critic_procurement(_request(observations))

    assert not any("E-SCENARIO-LEAK" in f.check_id for f in verdict.findings), (
        "이름이 틀렸는데 검사가 걸렸다 — 이 테스트의 전제가 바뀌었다"
    )
