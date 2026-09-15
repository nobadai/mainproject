"""걷기 요약이 **LLM 이 실제로 돌았는지**를 말하는가 — 2026-09-12.

걷기 한 판(`SIM-CHAIN-V6` · 71영업일)에서 LLM `SUCCESS` 가 **0건**인데 아무도
몰랐다.

```text
재무    FALLBACK          918    🔴 불렀는데 실패해서 결정론이 대신 답했다
물류    DISABLED          519    설정으로 껐다              🟢 정상
판매    SKIPPED_TEMPLATE  213    부를 일이 없었다            🟢 정상
매입    SKIPPED_TEMPLATE  171    같음                      🟢 정상
```

★★ 재무는 2026-09-11 19시대 이후 성공이 0건이다. 그 뒤 다섯 판이 전부 결정론으로
  돌았고, **원장에는 처음부터 다 적혀 있었는데 요약에 안 나와서 아무도 안 봤다.**

★★ 그리고 `app/master/plan.py:62` 가 이미 그 위험을 적어 뒀다 — *"Planner 가 죽어
  규칙 경로로 떨어져도 **산출물은 멀쩡해 보이고**, 그게 오늘 하루 종일 고친 실패
  방식이다."* 그 경고가 요약까지 안 올라와 있었다.

🔴 **이 파일이 잡으려는 셋.**

```text
① 네 어휘가 각자 세어진다        하루에 넷이 섞여 있어도 접히지 않는다
② 🔴 SUCCESS 0 이 보인다         0 이 사라지면 오늘 이 사태의 모양이 그대로 다시 선다
③ 어휘 이름을 마스터가 안 짓는다   주인은 envelope.LLMStatus 하나다 — 0건을 세도 막는다
```

★ **DB 를 안 탄다.** `ItemRunOutcome` 을 손으로 세워 `DayRunOutcome` 에 담고,
  걷기 결과는 `WalkResult` 를 직접 만든다.
"""

from __future__ import annotations

import ast
import unicodedata
from datetime import date
from pathlib import Path

import pytest

from app.master import backtest_runner
from app.master.backtest_runner import WalkResult, format_summary
from app.master.envelope import LLM_STATUSES, LLMStatus
from app.master.scheduler import DayRunOutcome, ItemRunOutcome, _llm_statuses

오늘 = date(2026, 9, 12)

#: LLM 어휘 넷. 🔴 **주인은 `envelope.LLMStatus` 다** — 여기서 새 이름을 안 붙인다.
넷 = tuple(sorted(LLM_STATUSES))

#: 실측이 말한 그 분포. `SUCCESS` 가 없다는 사실이 이 판의 전부다.
실측 = {"FALLBACK": 918, "DISABLED": 519, "SKIPPED_TEMPLATE": 384}


def _NFC(text: str) -> str:
    """한글 비교 전 정규화. **자모 분리형과 완성형이 섞이면 `in` 이 거짓말한다.**"""
    return unicodedata.normalize("NFC", text)


def _품목(*어휘들: str, item: str = "배추") -> ItemRunOutcome:
    return ItemRunOutcome(
        item=item,
        request_id=f"REQ-{item}",
        status="RAN",
        end_code="E1_PRESENTED",
        llm_statuses=tuple(어휘들),
    )


def _하루(*품목들: ItemRunOutcome) -> DayRunOutcome:
    return DayRunOutcome(as_of=오늘, action="RUN_NOW", reason="", items=tuple(품목들))


def _걷기(*날들: DayRunOutcome) -> WalkResult:
    return WalkResult(start=오늘, end=오늘, days=tuple(날들))


# ---------------------------------------------------------------------------
# ⓪ 🔴 **걷기가 그 값을 실제로 손에 쥐는가** — 손으로 세운 행으로는 못 재는 자리
# ---------------------------------------------------------------------------


class _계획단계:
    """부서 응답의 실행 계획 한 걸음 대역. **걷기가 읽는 칸만 든다.**"""

    def __init__(self, llm_status: str) -> None:
        self.llm_status = llm_status


class _응답:
    """`run_procurement` 응답 대역. `plan` 이 실려 오는 그 모양이다."""

    def __init__(self, *어휘들: str) -> None:
        self.end_code = "E1_PRESENTED"
        self.plan = [_계획단계(어휘) for 어휘 in 어휘들]


def test_부서_응답의_계획에서_어휘를_그대로_떼어_온다() -> None:
    """🔴 **값은 처음부터 `plan[].llm_status` 에 있었고 걷기에서 끊겼다.**

    ★ 접지 않고 부서가 낸 순서 그대로 나른다 — 여기서 세면 *"어느 부서가
      떨어졌나"* 를 나중에 열 수 없다.
    """
    assert _llm_statuses(_응답("FALLBACK", "DISABLED", "FALLBACK")) == (
        "FALLBACK",
        "DISABLED",
        "FALLBACK",
    )


def test_계획이_없는_응답은_빈_튜플이다() -> None:
    """⚠️ *"LLM 이 안 돌았다"* 가 아니라 **"셀 것이 없었다"** 다.

    빈 자리에 `DISABLED` 를 채우면 **끈 적 없는 날이 끈 날로 세어진다.**
    """

    class _계획없음:
        end_code = "E4_NOT_STARTED"

    assert _llm_statuses(_계획없음()) == ()
    assert _llm_statuses(None) == ()


def test_못_돈_품목은_어휘를_안_나른다() -> None:
    """★ 응답이 없으면 계획도 없다 — 그 자리는 `status` 가 `FAILED` 라고 말한다."""
    터진것 = ItemRunOutcome(item="배추", request_id="REQ-1", status="FAILED", reason="boom")

    assert 터진것.llm_statuses == ()


# ---------------------------------------------------------------------------
# ① 네 어휘가 각자 세어진다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("어휘", 넷)
def test_어휘_넷이_각자_세어진다(어휘: str) -> None:
    """⚠️ 접으면 *"설정으로 껐다"* 와 *"불렀는데 실패했다"* 가 같아 보인다.

    앞은 의도이고 뒤는 사고다 — 다음에 볼 자리가 다르다.
    """
    센것 = dict(_걷기(_하루(_품목(어휘))).llm_outcomes)

    assert 센것[어휘] == 1
    assert sum(센것.values()) == 1, f"한 건을 넣었는데 합이 다르다: {센것}"


def test_하루에_넷이_섞여_있어도_안_접힌다() -> None:
    """🔴 **한 품목 안에서 부서마다 다른 값이 난다** — 재무는 FALLBACK, 물류는 DISABLED."""
    센것 = dict(_걷기(_하루(_품목(*넷))).llm_outcomes)

    assert 센것 == dict.fromkeys(넷, 1)


def test_매입과_판매를_합계_한_줄로_센다() -> None:
    """★ 지금 필요한 것은 *"돌았나 안 돌았나"* 다 — 부서별로 가르면 줄이 넷이 된다."""
    하루 = DayRunOutcome(
        as_of=오늘,
        action="RUN_NOW",
        reason="",
        items=(_품목("FALLBACK"),),
        sales_items=(_품목("SKIPPED_TEMPLATE", item="무"),),
    )

    assert dict(_걷기(하루).llm_outcomes) == {
        "FALLBACK": 1,
        "SKIPPED_TEMPLATE": 1,
        "DISABLED": 0,
        "SUCCESS": 0,
    }


# ---------------------------------------------------------------------------
# ② 🔴 **`SUCCESS` 0 이 보인다** — 이 판의 핵심
# ---------------------------------------------------------------------------


def test_SUCCESS_가_0_이어도_요약에_0_으로_찍힌다() -> None:
    """🔴 **「0이라 안 보임」이 오늘 이 사태의 모양이다.**

    ★★ 재무 918건이 전부 `FALLBACK` 인데 `SUCCESS` 줄이 아예 없으면, 사람은 그
      성적표를 보고 *"LLM 이야기가 없네"* 로 읽는다 — 71영업일이 그렇게 지나갔다.
    """
    결과 = _걷기(_하루(_품목("FALLBACK"), _품목("DISABLED", item="무")))

    센것 = dict(결과.llm_outcomes)
    요약 = _NFC(format_summary(결과))

    assert 센것["SUCCESS"] == 0, "SUCCESS 가 세어지지도 않았다"
    assert "'SUCCESS': 0" in 요약, (
        f"요약에서 SUCCESS 0 이 사라졌다 — 그것이 이 판이 막으려는 실패다: {요약}"
    )


def test_한_번도_안_돈_걷기도_네_어휘가_다_보인다() -> None:
    """★ 빈 걷기의 `{}` 는 *"LLM 이 잘 돌았다"* 와 화면에서 같아 보인다."""
    센것 = dict(_걷기().llm_outcomes)

    assert 센것 == dict.fromkeys(넷, 0)
    assert "'SUCCESS': 0" in _NFC(format_summary(_걷기()))


def test_요약에_LLM_줄_자체가_선다() -> None:
    """★★ 값이 있는데 성적표가 안 읽으면 **없는 것과 같다.**"""
    결과 = _걷기(_하루(_품목("FALLBACK")))
    요약 = _NFC(format_summary(결과))

    assert _NFC("LLM어휘   ") in 요약, "요약에 LLM 줄 자체가 없다"
    for 어휘 in 넷:
        assert 어휘 in 요약, f"요약에 LLM 어휘 '{어휘}' 가 없다"


def test_실측_분포가_그대로_요약에_올라온다() -> None:
    """🔴 **그날 원장에 적혀 있던 그 숫자다.** 918 · 519 · 384 · 그리고 0."""
    품목들 = [
        _품목(*([어휘] * 개수), item=f"품목{번호}")
        for 번호, (어휘, 개수) in enumerate(실측.items())
    ]
    결과 = _걷기(_하루(*품목들))
    요약 = _NFC(format_summary(결과))

    assert dict(결과.llm_outcomes) == {**실측, "SUCCESS": 0}
    assert "'FALLBACK': 918" in 요약
    assert "'SUCCESS': 0" in 요약


# ---------------------------------------------------------------------------
# ③ 어휘 이름을 마스터가 안 짓는다 — **0건을 세면 그것도 막는다**
# ---------------------------------------------------------------------------


_요약파일 = Path(backtest_runner.__file__)


def _문자열들(source: str) -> list[str]:
    """그 파일이 코드에 적어 둔 문자열 상수 전부. **docstring 도 여기 든다.**"""
    return [
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def test_요약_모듈이_네_이름을_손으로_안_적는다() -> None:
    """🔴 **넷의 주인은 `envelope.LLMStatus` 다.**

    손으로 적으면 어휘가 느는 날 요약만 옛말을 하고, 새로 든 값이 성적표에서
    조용히 사라진다 — `envelope` 가 `get_args` 로 한 벌만 만드는 이유 그대로다.

    ★ **자기 생존 검사를 같이 둔다.** 스캐너가 문자열을 실제로 찾는지부터 본다 —
      안 그러면 0건을 세는 날 공짜 초록이 난다.
    """
    문자열 = _문자열들(_요약파일.read_text(encoding="utf-8"))

    assert 문자열, "스캐너가 문자열을 한 개도 못 찾았다 — 아래 단언은 공짜 초록이다"
    assert any(_NFC("LLM어휘") in one for one in 문자열), (
        "스캐너가 요약의 LLM 줄을 못 찾았다 — 스캐너가 망가졌거나 줄이 사라졌다"
    )

    지어낸것 = sorted(
        어휘
        for 어휘 in LLM_STATUSES
        # ★ docstring 의 어휘 설명표는 뜻을 적는 자리라 뺀다. 막는 것은 **식**이
        #   그 이름에 기대는 것이다 — `"SUCCESS" in ...` · `== "FALLBACK"` 처럼.
        for one in 문자열
        if one.strip() == 어휘
    )
    assert not 지어낸것, (
        f"요약 모듈이 LLM 어휘 이름을 직접 적는다: {지어낸것} —"
        " 주인은 envelope.LLMStatus 하나다"
    )


def test_요약_모듈이_어휘_집합을_주인에게서_들여온다() -> None:
    """★ 안 들여오면 위 검사는 *"안 적었다"* 만 말하고 0 이 사라져도 초록이 난다."""
    들여온것 = {
        alias.asname or alias.name
        for node in ast.walk(ast.parse(_요약파일.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom) and node.module == "app.master.envelope"
        for alias in node.names
    }

    assert "LLM_STATUSES" in 들여온것, (
        f"요약이 어휘 집합의 주인을 안 본다: {sorted(들여온것)}"
    )


def test_주인이_말하는_넷_그대로다() -> None:
    """★ 이 파일이 세는 어휘와 계약이 정한 어휘가 같은지를 한 줄로 붙잡는다."""
    assert set(넷) == set(LLMStatus.__args__)
    assert len(넷) == 4
