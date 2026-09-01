"""커버리지 숫자와 생략 문구가 **실제로 돈 것**을 말해야 한다.

🔴 **실측 2026-09-01 — 둘 다 실제보다 후하게 나가고 있었다.**

```text
[재무만  제출]   coverage 21/56   L1 (7,13)
[재무+물류 제출]  coverage 21/56   L1 (7,13)     ← 같았다
```

`l1_ran += 2 if meta else 0` 이 *"아무 부서나 하나 냈나"* 를 세었다. 물류가 생략돼도
숫자가 같았고, 그 사실은 `skipped` 줄에만 남았다.

그리고 그 `skipped` 줄도 물류에게는 **틀린 이름 하나를 적고 맞는 이름 하나를
빠뜨렸다** — `E-GRADE-LEAK` 은 재무 전용이고 `E-SCENARIO-LEAK` 이 빠져 있었다.

★ 부분 수행을 반올림해 주면 *"못 한 것을 통과로 치지 않는다"* 가 무너진다.
"""

from __future__ import annotations

import json
from datetime import date

from app.critic.service import run_critic_procurement
from app.master import critic_bridge as bridge
from app.master.critic_bridge import DEPT_CAP_CHECK_ID
from tests.master.test_critic_bridge import CONSTRAINTS, EVIDENCES, _proposal


def _meta(dept: str) -> str:
    return json.dumps(
        {
            "observation_type": f"{dept}_dept_meta",
            "inputs_used": {DEPT_CAP_CHECK_ID[dept]: ["on_hand_kg"]},
            "produced_fields": ["warehouse_free_kg"],
        }
    )


def _verdict(observations):
    request = bridge.build_request(
        as_of=date(2025, 12, 31),
        item="배추",
        proposal=_proposal(),
        constraints=CONSTRAINTS,
        evidences=EVIDENCES,
        observations=observations,
    )
    return run_critic_procurement(request)


def _l1(verdict) -> tuple[int, int]:
    return verdict.coverage["L1"]


def test_한_부서만_내면_완주로_치지_않는다():
    """🔴 전에는 재무만 내도 2를 다 세었다."""
    only_finance = _l1(_verdict({"finance": (_meta("finance"),)}))
    nobody = _l1(_verdict({}))

    assert only_finance == nobody, f"부분 제출에 가산이 붙었다: {only_finance} vs {nobody}"


def test_전부_내면_완주로_친다():
    both = _l1(_verdict({"finance": (_meta("finance"),), "inventory": (_meta("inventory"),)}))
    nobody = _l1(_verdict({}))

    assert both[0] == nobody[0] + 2, f"전원 제출인데 가산이 없다: {both} vs {nobody}"


def test_숫자가_실제로_움직인다():
    """★ **검사가 공허하지 않다는 증명.** 세 상태가 서로 달라야 한다."""
    nobody = _l1(_verdict({}))[0]
    partial = _l1(_verdict({"finance": (_meta("finance"),)}))[0]
    full = _l1(_verdict({"finance": (_meta("finance"),), "inventory": (_meta("inventory"),)}))[0]

    assert nobody == partial < full, f"{nobody} · {partial} · {full}"


def test_생략_문구가_부서마다_맞는_검사를_적는다():
    skipped = _verdict({}).skipped
    lines = {line.split(":")[0]: line for line in skipped if "DeptMeta" in line}

    assert "E-SCENARIO-LEAK" in lines["inventory"], "전 부서 공통 검사가 빠졌다"
    assert "E-GRADE-LEAK" not in lines["inventory"], "재무 전용 검사를 물류에 적었다"

    assert "E-SCENARIO-LEAK" in lines["finance"]
    assert "E-GRADE-LEAK" in lines["finance"], "재무에는 이 검사가 실제로 돈다"


def test_낸_부서는_생략_줄에_안_남는다():
    skipped = _verdict({"finance": (_meta("finance"),)}).skipped
    dept_meta_lines = [line for line in skipped if "DeptMeta" in line]

    assert len(dept_meta_lines) == 1
    assert dept_meta_lines[0].startswith("inventory:")
