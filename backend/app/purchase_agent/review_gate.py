"""근거 자기 검토의 **대상 고르기** (E3-10) — 순수 규칙.

🔴 **모든 안을 보내지 않는다.** 걷기 한 번에 안이 수백이라 전수로 보내면 비용과 지연이
그만큼 붙는다. 그런데 아무 신호나 쓰면 못 가른다 — 실측에서 *"기준등급 대체"* 는
231안 중 218건, *"근거 등급이 ASSUMED"* 는 **231건 전부**였다. 그런 신호는 «전수» 와 같다.

★ 그래서 **가르는 신호만** 골랐다 (`SIM-CHAIN-V13` 231안 실측)::

    E  ⑤ 판단자가 실제로 골랐다          0건    (지금 0이지만 열릴 축이다)
    B  원안에서 깎였다                  59건
    C  지급일이 재무 집중일과 겹친다        7건
    D  전략축 라벨과 실체가 다르다         51건
    ─ 합집합 69 / 231 (30%) · 봉투당 최대 3

🔴 **상한은 실행당이다.** 에이전트는 read-only 무상태라 걷기 전체를 못 센다 (규칙 2) —
   그걸 세려면 DB 를 읽어야 하고 그건 읽기 전용을 깨는 일이다. 걷기 전체 예산은 마스터 몫이다.

⚠️ **순서가 계약이다.** 같은 입력이면 같은 안이 같은 순서로 뽑혀야 한다. 시각·난수·집합
   순회에 기대면 같은 날의 같은 사실이 실행마다 다르게 검토된다.
"""

from dataclasses import dataclass

#: 실행당 검토 상한. 🔴 **3 이다.**
#:
#: 실측에서 봉투당 대상은 0건 83 · 1건 38 · 2건 14 · 3건 1 로 **최대가 3** 이다. 그래서
#: 이 값은 운영에서 한 번도 안 걸린다 — 정상 대상을 일부러 버리지 않는다는 뜻이다.
#: ``SKIPPED_BUDGET`` 경로는 **대상 4개 이상인 합성 입력**으로 검증한다.
#:
#: ⚠️ 상한을 2로 낮춰 「경로가 실제로 돈다」를 만드는 것은 제품 근거가 아니다.
PER_RUN_LIMIT = 3

#: 🔴 **선언 순서가 곧 우선순위다** (E > B > C > D). dict·집합 순회에 안 기댄다.
#:
#: 순서의 뜻: ⑤ 가 실제로 판단한 안이 가장 볼 값이 크고(사람이 아니라 모델이 고른 자리),
#: 그다음이 «원안과 실제가 다른» 안, 그다음이 현금 축 충돌, 마지막이 라벨과 실체의 어긋남이다.
SIGNAL_ORDER = (
    "MIX_APPLIED",
    "QUANTITY_CLIPPED",
    "PAYMENT_CONFLICT",
    "LABEL_BODY_MISMATCH",
)


@dataclass(frozen=True)
class ScenarioSignals:
    """안 하나에 켜진 신호. **부르는 쪽이 구조에서 뽑아 준다.**

    🔴 이 모듈은 상태를 안 읽는다 — 읽으면 노드를 import 하게 되고 순환이 된다.
    """

    label: str
    mix_applied: bool = False
    quantity_clipped: bool = False
    payment_conflict: bool = False
    label_body_mismatch: bool = False
    #: 🔴 이 안의 ``risks`` 가 **전부** 보류·못읽음류인가.
    #:
    #: ⚠️ *"보류가 하나라도 있으면 제외"* 로 읽으면 **전수가 제외된다** — 실측에서
    #:   보류 문장이 0인 안이 **하나도 없었다** (1개 83 · 2개 105 · 3개 12 · 4개 31).
    #:   전부일 때만 뺀다.
    only_deferred_risks: bool = False

    @property
    def score(self) -> int:
        """켜진 신호 수. 많을수록 먼저 본다."""
        return sum(getattr(self, _FIELD[name]) for name in SIGNAL_ORDER)

    @property
    def vector(self) -> tuple[bool, ...]:
        """우선순위 벡터 — 같은 개수일 때 **선언 순서**로 가른다."""
        return tuple(getattr(self, _FIELD[name]) for name in SIGNAL_ORDER)


_FIELD = {
    "MIX_APPLIED": "mix_applied",
    "QUANTITY_CLIPPED": "quantity_clipped",
    "PAYMENT_CONFLICT": "payment_conflict",
    "LABEL_BODY_MISMATCH": "label_body_mismatch",
}

#: 라벨의 **선언 순서**. 같은 신호 조합일 때 이 순서로 가른다 (``ScenarioLabel`` 과 같다).
LABEL_ORDER = ("보수", "기본", "공격")


@dataclass(frozen=True)
class GateResult:
    """누구를 보고 누구를 왜 안 봤나. **둘 다 남긴다.**

    🔴 안 본 안을 목록에서 그냥 빼면 *"봤는데 깨끗했다"* 와 구분되지 않는다.
    """

    selected: tuple[str, ...]
    skipped_by_gate: tuple[str, ...]
    skipped_by_budget: tuple[str, ...]


def choose(signals: list[ScenarioSignals], *, limit: int = PER_RUN_LIMIT) -> GateResult:
    """검토할 안을 **결정적으로** 고른다.

    ``선택 = 신호가 하나라도 켜진 안``이고, 같은 입력이면 같은 순서다::

        ① 켜진 신호 수 내림차순
        ② 우선순위 벡터 (E > B > C > D)
        ③ 라벨 선언 순서 (보수 · 기본 · 공격)

    ⚠️ 상한을 넘긴 안은 **``skipped_by_budget`` 으로 따로 적는다** — 「문제 없음」이 아니라
      「못 봤다」이고, 그 둘이 한 값이 되면 검토율이 거짓이 된다.
    """
    대상 = [s for s in signals if s.score > 0 and not s.only_deferred_risks]
    제외 = [s.label for s in signals if s not in 대상]
    순서 = sorted(
        대상,
        key=lambda s: (
            -s.score,
            tuple(not flag for flag in s.vector),
            LABEL_ORDER.index(s.label) if s.label in LABEL_ORDER else len(LABEL_ORDER),
        ),
    )
    return GateResult(
        selected=tuple(s.label for s in 순서[:limit]),
        skipped_by_gate=tuple(제외),
        skipped_by_budget=tuple(s.label for s in 순서[limit:]),
    )
