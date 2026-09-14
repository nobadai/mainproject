"""분할 회차의 **비율·오프셋·수량 투영** — 노드가 공유하는 순수 함수층.

🔴 **왜 모듈을 따로 뒀나 — 역 import 가 순환이기 때문이다.**

노드 사이의 의존 방향이 이미 한쪽이다 (실측 · ``dev@2b0c270``)::

    split_plan          → classify_situation                     (그것뿐)
    package_scenarios   → split_plan · allocate_sourcing · draft_plan · classify_situation
    self_check          → split_plan · draft_plan · collect_context

그래서 ④ ``split_plan`` 이 ⑥ ``package_scenarios`` 나 ⑦ ``self_check`` 의 함수를 쓰려고
그쪽을 import 하면 **순환**이 된다. ④가 «비율 후보를 만들어 놓고 그 후보가 실제로 설 수
있는지» 를 미리 보려면 ⑥의 투영 산술이 필요한데, 그 자리를 노드 밖으로 뺀 것이 여기다.

★ **그래서 이 모듈은 노드를 하나도 import 하지 않는다.** 이 규율이 깨지면 순환이 돌아오고,
  그것을 검사가 잠근다 (``test_allocation.py``).

🟢 **이 판은 옮기기만 한다 — 동작 변경 0.** 아래 셋은 몸통도 주석도 원래 자리의 것을
그대로 가져왔고, 원래 모듈은 여기서 import 해 **같은 이름으로 계속 내보낸다**::

    equal_ratios      ← nodes/split_plan.py
    split_offsets     ← nodes/package_scenarios.py
    split_quantities  ← nodes/package_scenarios.py

⚠️ **아래 셋의 docstring 은 한 글자도 안 고쳤다.** 그래서 ``split_offsets`` 안의
*"날짜를 ④가 아니라 **여기서** 만드는 이유"* 의 「여기」는 **원래 살던 ⑥ ``package_scenarios``**
를 가리킨다 — 지금도 그 함수를 부르는 것은 ⑥이다. 옮기면서 문면을 손보면 *"몸통이 같다"* 를
AST 로 증명할 수 없게 되고, 「옮긴 것」과 「고친 것」이 한 커밋에 섞인다.

⚠️ **``round_offsets`` 는 안 옮겼다.** 그쪽은 ``execution_calendar`` 봉투를 읽어 회차일을
미는 함수라 «순수 산술» 이 아니고, 부르는 곳도 ⑥ 하나다. 여기로 끌어오면 이 모듈이
봉투 모양을 알게 된다.
"""


def equal_ratios(rounds: int) -> list[float]:
    """균등 비율. 마지막을 ``1 − Σ앞``으로 **구성**한다.

    각자 계산한 ``1/n``을 n번 더하면 부동소수점 합이 1에서 밀려 ⑥의 합계 검사(1e-9)에
    걸릴 수 있다 — E3-1에서 등급 비율에 쓴 것과 같은 장치다.
    """
    head = [1 / rounds] * (rounds - 1)
    return [*head, 1.0 - sum(head)]


def split_offsets(coverage_days: int, rounds: int) -> list[int]:
    """회차별 **매입 실행일** 오프셋 = ``round(i × D / rounds)``.

    첫 회차는 항상 0(= as_of)이다 — IO명세 §2 "seq 1의 date = as_of".
    날짜를 ④가 아니라 여기서 만드는 이유: 안마다 D가 다르다 (§4-④ E3-3 확정 4).
    보수(D=2)와 공격(D=12)에 같은 날짜를 박으면 보수안의 2회차가 커버 구간 밖으로 나간다.

    ⚠️ 이 date는 **도착일이 아니다.** 도착일 = ``date + N4``이고 N4가 NULL이라 계산하지
    않는다 (§5.5 · 규칙 3).
    """
    return [round(index * coverage_days / rounds) for index in range(rounds)]


def split_quantities(total_qty_kg: int, chosen: list[dict]) -> list[int]:
    """회차별 수량. **마지막 회차가 잔량을 흡수한다** — 반올림이 총량을 흔들면
    사중 일치가 깨진다."""
    remaining = total_qty_kg
    quantities = []
    for index, part in enumerate(chosen, start=1):
        qty = remaining if index == len(chosen) else round(total_qty_kg * part["ratio"])
        quantities.append(qty)
        remaining -= qty
    return quantities
