"""**화면 문구가 세 경우에 다 참인가** — `missing_data` 는 한 칸에 세 종류를 담는다.

```text
안 왔다              부서가 값을 안 보냈다
왔는데 쓰지 말라      ML 이 use_recommended=False 로 표시했다 (매입 #231)
값이 look-ahead 다    generated_at > as_of (매입 `_unusable_forecast_names`)
```

*"없는 입력"* 은 첫째에만 참이라 둘째가 오는 날 이런 줄이 나갔다 —

```text
예측을 만든 쪽이 오늘 값을 쓰지 말라고 표시해 시나리오를 만들지 않았다.
  / 없는 입력: forecast.use_recommended
```

앞뒤가 반대말이다. 어휘는 새로 만들지 않고 매입이 쓴 *"쓸 수 없는 입력"* 을 가져온다.
"""

from __future__ import annotations

from datetime import date

from app.master.answer import facts_from_status
from app.master.flow import AgentFailure
from app.master.plan import ExecutionPlan
from app.master.status_flow import StatusOutcome

#: 매입이 값을 받고도 *"쓰지 말라"* 로 표시했을 때 실제로 나가는 사유 문장이다.
_쓰지_말라고_온_사유 = "예측을 만든 쪽이 오늘 값을 쓰지 말라고 표시해 시나리오를 만들지 않았다."

#: 옛 문구를 잡는 앵커. `"없는 입력"` 만 보면 새 문구 `"쓸 수 없는 입력"` 안에도 들어
#: 있어 늘 걸린다. 옛 렌더는 `" / 없는 입력: ..."` 였으므로 구분자까지 앵커에 넣는다.
_옛_문구 = "/ 없는 입력"

#: 조회 경로의 옛 문구. 새 문구는 `"... 를 쓸 수 없어 답하지 못했습니다"` 라 겹치지 않는다.
_옛_조회_문구 = "가 없어 답하지 못했습니다"


def _status(missing: dict) -> StatusOutcome:
    return StatusOutcome(
        status_code="S2_PARTIAL",
        reason="...",
        plan=ExecutionPlan(request_id="REQ-TEST", as_of=date(2025, 12, 31)),
        answers={"finance": {"cash_balance": 1000}},
        unavailable=tuple(missing),
        missing_data=missing,
    )


# ── 매입 Flow 경로 (`AgentFailure.detail`) ────────────────────────────────


def test_이름_절은_쓸_수_없는_입력이라고_말한다():
    실패 = AgentFailure(
        agent="purchase",
        runtime_status="RUNTIME_NOT_READY",
        reasoning="예측이 오지 않았다.",
        missing_data=("forecast.daily",),
    )

    assert "쓸 수 없는 입력: forecast.daily" in 실패.detail


def test_옛_문구는_다시_나오지_않는다():
    """회귀 검사 — *"없는 입력"* 은 한 갈래에만 참이라 다시 쓰면 안 된다."""
    실패 = AgentFailure(
        agent="purchase",
        runtime_status="RUNTIME_NOT_READY",
        reasoning="예측이 오지 않았다.",
        missing_data=("forecast.daily", "forecast.generated_at"),
    )

    assert _옛_문구 not in 실패.detail


def test_부서_문장이_먼저_오고_이름이_뒤에_온다():
    """순서를 고정한다 — 부서가 쓴 사유가 앞이고 마스터가 붙이는 이름 절이 뒤다.
    마스터는 해석하지 않으므로 사람이 먼저 읽는 것은 부서 문장이어야 한다."""
    실패 = AgentFailure(
        agent="purchase",
        runtime_status="RUNTIME_NOT_READY",
        reasoning="예측이 오지 않았다.",
        missing_data=("forecast.daily",),
    )

    한_줄 = 실패.detail
    assert 한_줄.index("예측이 오지 않았다.") < 한_줄.index("쓸 수 없는 입력")


def test_이름이_없으면_이름_절도_없다():
    """없는 것을 있다고 하지 않는다 — 빈 `missing_data` 에 빈 절을 붙이면
    *"쓸 수 없는 입력: "* 라는 뒤가 잘린 줄이 나간다."""
    실패 = AgentFailure(
        agent="finance",
        runtime_status="ERROR",
        reasoning="호출이 터졌다.",
    )

    assert 실패.detail == "호출이 터졌다."
    assert "쓸 수 없는 입력" not in 실패.detail


def test_쓰지_말라고_온_값에도_같은_문구가_참이다():
    """🔴 이 검사가 이 변경의 이유다.

    값은 **왔다.** ML 이 `use_recommended=False` 로 *"쓰지 마세요"* 라고 표시했을 뿐이다
    (매입 #231). 한 줄 안에서 *"쓰지 말라고 왔다"* 와 *"없다"* 가 같이 나가면 반대말이
    붙는다.
    """
    실패 = AgentFailure(
        agent="purchase",
        runtime_status="RUNTIME_NOT_READY",
        reasoning=_쓰지_말라고_온_사유,
        missing_data=("forecast.use_recommended",),
    )

    한_줄 = 실패.detail
    assert _쓰지_말라고_온_사유 in 한_줄
    assert "쓸 수 없는 입력: forecast.use_recommended" in 한_줄
    assert _옛_문구 not in 한_줄, "왔는데 쓰지 말라고 온 값에 '없다' 는 거짓이다"


# ── 조회 경로 (`_status_gaps`) ────────────────────────────────────────────


def test_조회_경로도_같은_어휘를_쓴다():
    """같은 사실이 두 경로로 화면에 나간다 — 문구가 갈리면 사람은 다른 일로 읽는다."""
    사실 = facts_from_status(_status({"purchase": ("forecast.daily",)}))

    한_줄 = " ".join(사실.gaps)
    assert "forecast.daily 를 쓸 수 없어 답하지 못했습니다" in 한_줄
    assert _옛_조회_문구 not in 한_줄


def test_조회_경로도_쓰지_말라고_온_값에_참이다():
    사실 = facts_from_status(_status({"purchase": ("forecast.use_recommended",)}))

    한_줄 = " ".join(사실.gaps)
    assert "forecast.use_recommended 를 쓸 수 없어" in 한_줄
    assert _옛_조회_문구 not in 한_줄, "왔는데 쓰지 말라고 온 값에 '없다' 는 거짓이다"
