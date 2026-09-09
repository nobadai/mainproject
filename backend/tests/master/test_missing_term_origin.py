"""누락한 상업조건의 **위치** — 호출에서 안 실린 것 · 정말 조건이 없는 것.

`#457` 이 후보 필수조건을 넣었다. 그런데 그것은 **무엇이 없는지**만 말했다.

```text
missing_terms == ("delivery_date",)
  → 화면에 채울 칸이 있는데 안 채운 것인가
  → 판매가 값을 못 만든 것인가
  둘을 가릴 수 없어 사람이 코드를 읽어야 했다
```

🔴 **이 파일이 잡으려는 여섯.**

```text
① 늘 REQUEST_MISSING        판매가 못 만든 것까지 화면 잘못이 된다
② 늘 TERMS_UNRESOLVED       화면이 안 보낸 것까지 판매를 보러 간다
③ CONTRACT_FULFILLMENT 무시  계약값을 쓰는 경로에 "안 보냈다" 를 씌운다 — 거짓말이다
④ preferred_* 대응 어긋남     보냈는데 "안 보냈다" 가 된다
⑤ payment_days 0            "당일 수금" 요청이 조용히 "안 보냈다" 가 된다
⑥ 손으로 나열한 어휘          REQUIRED_COMMERCIAL_TERMS 가 늘어도 어휘가 안 는다
```

★ **부서·DB 를 안 세운다.** 재는 것은 `CandidateVerdict` 판정 하나와 그 사유 문장이다.

🔴 **자기 생존 검사가 아래 ⑦에 있다.** 아래 검사들이 *"0건을 세고 초록"* 이 되지
  않는다는 것을 그 자리가 보증한다 — 상업조건이 다 있으면 어휘가 한 건도 안 나오고,
  그러면 *"REQUEST_MISSING 이 안 나온다"* 를 재는 검사가 전부 공짜로 통과한다.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.master import sales_approval
from app.master.sales_flow import CandidateVerdict

납품일 = "2026-09-20"


class _없음:
    """`_scenario(delivery_date=_없음)` — 칸 자체를 빼는 표시."""


def _scenario(**덮어쓰기: Any) -> dict[str, Any]:
    """상업조건이 다 있는 후보. 덮어써서 하나씩 뺀다."""
    base: dict[str, Any] = {
        "scenario_id": "SCN-1",
        "item": "배추",
        "delivery_date": 납품일,
        "payment_days": 30,
    }
    base.update(덮어쓰기)
    return {k: v for k, v in base.items() if v is not _없음}


def _판정(
    *,
    user_request: dict[str, Any] | None = None,
    business_mode: str | None = "SPOT_SALES",
    **덮어쓰기: Any,
) -> CandidateVerdict:
    return CandidateVerdict(
        scenario=_scenario(**덮어쓰기),
        validations={"FINANCIAL_VALIDATION": {"runtime_status": "READY", "business_status": "ok"}},
        user_request=user_request,
        business_mode=business_mode,
    )


# ---------------------------------------------------------------------------
# ① 호출에서 안 실렸다 — 고칠 사람은 화면·걷기·API 호출자
# ---------------------------------------------------------------------------

_필수 = ("delivery_date", "payment_days")


@pytest.mark.parametrize("field", _필수)
def test_요청에_안_실렸으면_REQUEST_MISSING_이다(field: str):
    """🔴 **① 을 잡는 자리.** 사용자가 아무 조건도 안 말했고 그 안에 값이 없다 —
    판매에 물어봐야 소용이 없다. 채울 곳은 **화면**이다.

    ⚠️ 두 필드 각각에 대해 돈다. 하나만 재면 다른 하나가 손으로 나열된 채 남는다.
    """
    후보 = _판정(user_request={}, **{field: None})

    assert 후보.missing_terms == (f"REQUEST_MISSING_{field}",)
    assert 후보.passed is False


@pytest.mark.parametrize("field", _필수)
def test_보냈는데_결과에_없으면_TERMS_UNRESOLVED_다(field: str):
    """🔴 **② 를 잡는 자리.** 사용자가 말했는데 후보에 안 실렸다 — 화면은 할 일을 했다.
    **정한 사람이 없는 것**이고 볼 곳은 판매·계약이다.
    """
    보냄 = {sales_approval.preferred_request_field(field): (납품일 if "date" in field else 30)}
    후보 = _판정(user_request=보냄, **{field: None})

    assert 후보.missing_terms == (f"TERMS_UNRESOLVED_{field}",)


def test_두_원인이_한_판정에_같이_나온다():
    """★ 한 후보에 둘이 섞일 수 있다. 한쪽으로 접으면 절반이 틀린 사람을 가리킨다."""
    후보 = _판정(
        user_request={"preferred_delivery_date": 납품일},
        delivery_date=None,
        payment_days=None,
    )

    assert 후보.missing_terms == (
        "TERMS_UNRESOLVED_delivery_date",
        "REQUEST_MISSING_payment_days",
    )


# ---------------------------------------------------------------------------
# ③ 계약 이행 경로 — 안 보내는 것이 정상이다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", _필수)
def test_CONTRACT_FULFILLMENT_에서는_REQUEST_MISSING_이_안_나온다(field: str):
    """🔴 **③ 을 잡는 자리.** 계약 이행은 **계약에 적힌 값**을 쓴다 — 마스터가
    `preferred_*` 를 안 보내는 것이 정상이다.

    ⚠️ 여기서 `REQUEST_MISSING` 을 내면 **없는 잘못을 화면에 씌운다.** 화면은 보낼
      것이 없었고, 없는 것은 계약값이다.
    """
    후보 = _판정(user_request={}, business_mode="CONTRACT_FULFILLMENT", **{field: None})

    assert 후보.missing_terms == (f"TERMS_UNRESOLVED_{field}",)
    assert not any(o.startswith("REQUEST_MISSING_") for o in 후보.missing_terms)


def test_다른_영업모드는_계약_이행으로_안_읽힌다():
    """★ 위 검사의 짝. `business_mode` 가 있기만 하면 계약 이행으로 접는 구현이면
    여기가 빨개진다 — `CONTRACT_PROPOSAL_NEW` 는 조건을 새로 정하는 경로다."""
    후보 = _판정(user_request={}, business_mode="CONTRACT_PROPOSAL_NEW", delivery_date=None)

    assert 후보.missing_terms == ("REQUEST_MISSING_delivery_date",)


# ---------------------------------------------------------------------------
# ④ preferred_* 대응 · ⑤ 0 은 실린 값이다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", _필수)
def test_요청_칸_이름은_preferred_접두다(field: str):
    """🔴 **④ 를 잡는 자리.** 대응이 어긋나면 **보냈는데 "안 보냈다"** 가 된다.

    ```text
    delivery_date  ↔  user_request["preferred_delivery_date"]
    payment_days   ↔  user_request["preferred_payment_days"]
    ```

    ★ 그 대응은 마스터가 실제로 싣는 칸 이름이다 (`service._sales_user_request`).
    """
    assert sales_approval.preferred_request_field(field) == f"preferred_{field}"


def test_preferred_payment_days_0_은_실린_값이다():
    """🔴 **⑤ 를 잡는 자리.** `0` 은 *"당일 수금을 원한다"* 는 **정해진 요청**이다.

    ⚠️ `falsy` 로 세면 그 요청이 조용히 *"안 보냈다"* 가 되고, 판매가 값을 못 만든
      잘못이 화면 잘못으로 뒤집힌다.
    """
    후보 = _판정(user_request={"preferred_payment_days": 0}, payment_days=None)

    assert 후보.missing_terms == ("TERMS_UNRESOLVED_payment_days",)


def test_빈_문자열은_안_실린_것으로_센다():
    """★ JSON 왕복에서 날짜가 `""` 로 오는 경우가 있다 — `missing_commercial_terms`
    와 같은 셈법이어야 두 답이 안 갈린다."""
    후보 = _판정(user_request={"preferred_delivery_date": ""}, delivery_date=None)

    assert 후보.missing_terms == ("REQUEST_MISSING_delivery_date",)


# ---------------------------------------------------------------------------
# ⑥ 어휘는 REQUIRED_COMMERCIAL_TERMS 에서 나온다
# ---------------------------------------------------------------------------


def test_어휘는_필수조건_목록에서_나온다():
    assert sales_approval.missing_term_origin_vocabulary() == (
        "REQUEST_MISSING_delivery_date",
        "TERMS_UNRESOLVED_delivery_date",
        "REQUEST_MISSING_payment_days",
        "TERMS_UNRESOLVED_payment_days",
    )


def test_필수조건이_늘면_어휘도_같이_는다(monkeypatch):
    """🔴 **⑥ 을 잡는 자리.** 어휘를 손으로 나열하면 상수가 는 날 새 칸만 원인 없이
    나간다 — 그러면 그 칸은 *"누가 고쳐야 하나"* 를 영원히 못 말한다.
    """
    monkeypatch.setattr(
        sales_approval,
        "REQUIRED_COMMERCIAL_TERMS",
        ("delivery_date", "payment_days", "carrier"),
    )

    assert "REQUEST_MISSING_carrier" in sales_approval.missing_term_origin_vocabulary()
    assert "TERMS_UNRESOLVED_carrier" in sales_approval.missing_term_origin_vocabulary()
    assert _판정(user_request={}, carrier=None).missing_terms == (
        "REQUEST_MISSING_carrier",
    )


@pytest.mark.parametrize("field", _필수)
def test_칸_이름을_접두에서_되꺼낼_수_있다(field: str):
    """★ 화면이 필드 이름만 필요할 수도 있다. 문자열을 화면이 직접 자르기 시작하면
    접두를 바꾸는 날 조용히 틀린 이름이 뜬다."""
    for prefix in ("REQUEST_MISSING_", "TERMS_UNRESOLVED_"):
        assert sales_approval.term_of_origin(f"{prefix}{field}") == field


# ---------------------------------------------------------------------------
# 사유 문장 — 두 경우를 갈라 말한다
# ---------------------------------------------------------------------------


def test_사유가_두_경우를_갈라_말한다():
    """★ 문장의 뜻은 그대로다 (*"없는 값을 지어내지 않는다"*). 원인이 덧붙었을 뿐이다.

    ⚠️ 문장을 두 벌로 만들면 한쪽만 고치는 날이 온다 — 한 함수가 둘을 다 낸다.
    """
    후보 = _판정(
        user_request={"preferred_delivery_date": 납품일},
        delivery_date=None,
        payment_days=None,
    )
    사유 = 후보.detail

    assert "없는 값을 지어내지 않는다" in 사유
    assert "요청에 안 실렸다: 수금 유예일(payment_days)" in 사유
    assert "조건이 정해지지 않았다: 납품일(delivery_date)" in 사유


def test_한_원인만이면_다른_절이_안_붙는다():
    """★ 없는 원인을 적으면 사람이 없는 담당자를 찾아간다."""
    사유 = _판정(user_request={}, delivery_date=None).detail

    assert "요청에 안 실렸다" in 사유
    assert "조건이 정해지지 않았다" not in 사유


def test_사유가_부서_판정과_섞이지_않는다():
    """🔴 **`validations` 에 가짜 항목을 밀어 넣지 않는다.** 탈락 사유가 *"재무가
    반려"* 처럼 보이면 사람이 재무를 본다."""
    후보 = _판정(user_request={}, delivery_date=None)

    assert set(후보.validations) == {"FINANCIAL_VALIDATION"}
    assert "FINANCIAL_VALIDATION(" not in 후보.detail


# ---------------------------------------------------------------------------
# ⑦ 자기 생존 검사 — 0건을 세고 초록이 되지 않는다
# ---------------------------------------------------------------------------


def test_상업조건이_다_있으면_어휘가_한_건도_안_나온다():
    """🔴 **위 검사들의 바닥이다.**

    상업조건이 늘 다 있으면 어휘가 한 건도 안 나오고, 그러면 *"REQUEST_MISSING 이
    안 나온다"* 를 재는 검사가 전부 공짜로 통과한다. **나올 수 있다**는 것과 **다
    있으면 안 나온다**는 것을 여기서 한 번 보인다.
    """
    assert _판정(user_request={}).missing_terms == ()
    assert _판정(user_request={}).passed is True
    assert _판정(user_request={}, delivery_date=None).missing_terms != ()


def test_판정이_실행기를_안_들고_있다():
    """🔴 **`CandidateVerdict` 가 `SalesFlow` 를 통째로 들지 않는다.** 판정 하나가
    실행기를 참조하면 이 판정을 재는 검사가 실행기를 세워야 한다 — 이 파일이 부서도
    DB 도 안 세우고 도는 것이 그 증거다.
    """
    후보 = _판정(user_request={})

    assert not hasattr(후보, "runner")
    assert not hasattr(후보, "flow")
