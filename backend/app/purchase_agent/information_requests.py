"""「못 판정했다」를 **구조화해 내보낸다** — 부족 정보 요청 (E3-11 · 규칙 · LLM 아님).

🔴 **왜 만드나.** 지금도 *"…검사 보류 — 물류 입고 소요일이 미확정이라…"* 같은 문장이
안의 ``risks`` 로 매일 나간다. 사람은 읽지만 **마스터는 기계적으로 못 받는다** — 누구에게
무엇을 청해야 풀리는지가 문장 안에 녹아 있어서다. 같은 판정에서 구조를 하나 더 낸다.

★★ **정본은 하나다.** 판정은 여기서 **한 번만** 하고, 그 결과가 둘을 낳는다::

    MissingInfo  ──┬──▶ risk 문장   (기존과 **바이트가 같다** — 문면을 안 바꾼다)
                   └──▶ InformationRequest  (구조화 · 플래그가 켜질 때만 실린다)

판정 코드를 두 벌로 두면 한쪽만 고치는 날 조용히 갈린다. 그것을 ``test_information_requests``
의 짝 검사와 변이가 잠근다.

🔴 **노드를 import 하지 않는다.** 부르는 쪽이 ③ ``draft_plan`` 이라 여기서 그쪽을 되짚으면
순환이다. 그래서 이 모듈은 **판정 입력을 받아서** 받는다 — 값을 만드는 것은 부르는 쪽이고,
여기가 하는 일은 *"그 사실이 누구에게 무엇을 청하는 것인가"* 를 붙이는 것뿐이다.

⚠️ **1차 범위는 「봉투 필드」 넷뿐이다.** 아래 둘은 일부러 뺐다.

``신선도 상한 — 품목 보관한계 미확정``
    🔴 **요청으로 안 낸다.** 그 협의(`#390`)는 물류가 *"현재 보관한계 값이 그 판단을 실제로
    자르지 않는 범위라 이번 단계에서 추가 변경하지 않겠다"* 로 **종료**했고 우리도 청하지
    않기로 정했다. 요청으로 내보내면 **닫은 협의가 매일 되살아난다.** 기존 risk 로 남긴다.

``문서를 못 읽었다``
    🔴 봉투 **필드**가 아니라 운영 저장소/역량이다. 같은 칸에 섞으면 ``required_field`` 가
    「경로」와 「역량」 두 뜻을 갖는다. 소비자가 둘 다 다룰 준비가 되면 그때 연다.

⚠️ **「누가 주면 풀리는 것」만 요청이다.** 예컨대 *"그날 시세에 상·중 등급이 없었다"* 는
**시장의 사실**이라 아무도 줄 수 없다 — 요청으로 내보내면 못 받을 것을 매일 청하게 된다.
"""

from dataclasses import dataclass
from typing import Literal

#: 값을 줄 수 있는 쪽. 🔴 ``purchase`` 가 없다 — 자기에게 청하는 요청은 요청이 아니다.
RequestedFrom = Literal["logistics", "finance", "sales", "ml", "master"]

#: 🔴 **표시 문구가 아니라 봉투 경로다.** *"품목 보관한계"* 같은 말로 두면 받는 쪽이
#: 어느 칸을 채워야 하는지 매번 사람에게 물어야 한다.
RequiredField = Literal[
    "constraints.inventory.cap_by_date",
    "constraints.inventory.inbound_lead_days",
    "constraints.finance.purchase_payment_days",
    "inventory.inventory_by_item[].available_qty_kg",
]

#: 그 값이 없어서 **못 한 검사**의 이름.
BlockedCheck = Literal[
    "ARRIVAL_CAPACITY",
    "SPLIT_ENTRY",
    "ARRIVAL_DATE",
    "HOLDINGS_WINDOW",
    "HOLDINGS_DEDUCTION",
    "PAYMENT_SCHEDULE",
]

ReasonCode = Literal[
    "ARRIVAL_CAPACITY_NOT_CHECKED",
    "FREE_STOCK_NOT_RECEIVED",
    "INBOUND_LEAD_NOT_DECLARED",
    "PAYMENT_LEAD_NOT_DECLARED",
]

#: 🔴 **지금 가능한 것은 이것뿐이다.** 마스터는 부분 재진입을 하지 않는다 — 값이 오면
#: 에이전트를 **처음부터** 다시 부른다 (``graph`` 머리말: *"마스터가 통째로 다시 부른다
#: → 그래프는 ①부터 새로 돈다"*). ``resume_from: self_check`` 처럼 **못 하는 동작을
#: 계약에 적지 않는다** — 적으면 받는 쪽이 있는 기능으로 읽는다.
RERUN_SCOPE = "FULL_AGENT"

#: 입고 소요일이 미결일 때 나가는 문장 둘. 🔴 **문면 정본이 여기다** — 기존 ③이 들고
#: 있던 것을 글자 그대로 옮겼고, ③은 이제 이 목록을 렌더링한다.
INBOUND_LEAD_SENTENCE = (
    "입고일 기준 창고 점유 검사 보류 — 물류 입고 소요일이 미확정이라 "
    "회차별 도착일을 계산하지 않는다"
)
#: 차감이 **실제로 걸린 날**에만 얹는다. 안 깎인 날에 내면 없는 일에 사과하는 고지가 된다.
HOLDINGS_WINDOW_SENTENCE = (
    "보유 재고를 뺀 창과 매입이 덮는 창이 맞는지 확인 보류 — 물류 입고 "
    "소요일이 미확정이라 매입분 도착일을 놓지 못한다"
)
PAYMENT_LEAD_SENTENCE = (
    "지급일 기준 현금 검사 보류 — 재무 대금 지급 소요일이 미확정이라 "
    "회차별 지급일을 계산하지 않는다"
)


@dataclass(frozen=True)
class _Routing:
    """사유 하나가 **누구에게 어느 칸을 청하고 무엇을 막았나**."""

    required_field: RequiredField
    requested_from: RequestedFrom
    blocked_checks: tuple[BlockedCheck, ...]


#: 🔴 **선언 순서가 곧 정렬 순서다** (아래 ``to_requests``). dict 순회에 안 기대려고
#: 순서를 여기 한 곳에서만 정한다.
ROUTING: dict[str, _Routing] = {
    "ARRIVAL_CAPACITY_NOT_CHECKED": _Routing(
        # ★ 같은 누락(``cap_by_date``)이 **두 검사를 막는다** — ①의 분할 진입 게이트와
        #   ⑦의 도착일 수용량. 사유를 둘로 쪼개면 받는 쪽이 **두 번 청해야 하는 줄로 읽는다.**
        required_field="constraints.inventory.cap_by_date",
        requested_from="logistics",
        blocked_checks=("ARRIVAL_CAPACITY", "SPLIT_ENTRY"),
    ),
    "INBOUND_LEAD_NOT_DECLARED": _Routing(
        required_field="constraints.inventory.inbound_lead_days",
        requested_from="logistics",
        blocked_checks=("ARRIVAL_DATE", "HOLDINGS_WINDOW"),
    ),
    "FREE_STOCK_NOT_RECEIVED": _Routing(
        required_field="inventory.inventory_by_item[].available_qty_kg",
        requested_from="logistics",
        blocked_checks=("HOLDINGS_DEDUCTION",),
    ),
    "PAYMENT_LEAD_NOT_DECLARED": _Routing(
        required_field="constraints.finance.purchase_payment_days",
        requested_from="finance",
        blocked_checks=("PAYMENT_SCHEDULE",),
    ),
}


@dataclass(frozen=True)
class MissingInfo:
    """판정 하나. **문장과 요청이 여기서 같이 나온다.**

    ``risk_sentences`` 가 복수인 이유: 입고 소요일 미결은 한 원인인데 **고지가 둘**이다
    (도착일·보유 차감 창). 원인이 하나라 요청은 하나이고, 문장만 둘이다.
    """

    reason_code: str
    risk_sentences: tuple[str, ...]


def missing_information(
    *,
    split_entry_unknown: str | None,
    free_stock_unknown: str | None,
    inbound_lead_missing: bool,
    holdings_deducted: bool,
    payment_lead_missing: bool,
) -> tuple[MissingInfo, ...]:
    """「못 판정했다」를 **한 번만** 판정한다.

    🔴 **순서가 계약이다.** 아래 순서 그대로 ③이 ``risks`` 에 싣는다 — 바꾸면 기존
    산출물과 문장 순서가 달라지고, 그건 문면 변경이다 (이 판이 안 하기로 한 일).

    ⚠️ ``split_entry_unknown`` 은 **도착일을 아는 날에만** 넘어온다. N4 가 미결이면
    입고 소요일 가지가 이미 그 사실을 말하므로, 한 원인을 두 문장으로 내지 않는다
    (③이 원래 지키던 규율이고 그대로 둔다).
    """
    infos: list[MissingInfo] = []
    if split_entry_unknown is not None:
        infos.append(
            MissingInfo("ARRIVAL_CAPACITY_NOT_CHECKED", (split_entry_unknown,))
        )
    if free_stock_unknown is not None:
        infos.append(MissingInfo("FREE_STOCK_NOT_RECEIVED", (free_stock_unknown,)))
    if inbound_lead_missing:
        문장 = [INBOUND_LEAD_SENTENCE]
        if holdings_deducted:
            문장.append(HOLDINGS_WINDOW_SENTENCE)
        infos.append(MissingInfo("INBOUND_LEAD_NOT_DECLARED", tuple(문장)))
    if payment_lead_missing:
        infos.append(
            MissingInfo("PAYMENT_LEAD_NOT_DECLARED", (PAYMENT_LEAD_SENTENCE,))
        )
    return tuple(infos)


def risk_sentences(infos: tuple[MissingInfo, ...]) -> list[str]:
    """사람이 읽는 쪽. **판정 순서를 그대로 편다.**"""
    return [sentence for info in infos for sentence in info.risk_sentences]


def to_requests(infos: tuple[MissingInfo, ...]) -> list[dict]:
    """마스터가 읽는 쪽. **중복을 합치고 선언 순서로 세운다.**

    🔴 **결정적이어야 한다** — 같은 입력이면 같은 순서다. 집합 순회나 해시 순서에 기대면
    같은 날의 같은 사실이 실행마다 다른 순서로 나가고, 그러면 봉투 대조가 못 쓰게 된다.

    ⚠️ 중복 키는 ``(requested_from, required_field, reason_code)`` 다. 지금은 한 사유가
    한 번만 서지만, 라벨별로 모으면 같은 사유가 여러 번 들어온다 — 그때 합쳐진다.
    """
    본 = []
    for code, 길 in ROUTING.items():  # 🔴 선언 순서
        if not any(info.reason_code == code for info in infos):
            continue
        본.append(
            {
                "requested_from": 길.requested_from,
                "required_field": 길.required_field,
                "reason_code": code,
                "blocked_checks": list(길.blocked_checks),
                "rerun_scope": RERUN_SCOPE,
            }
        )
    return 본
