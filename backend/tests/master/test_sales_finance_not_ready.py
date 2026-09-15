"""재무가 **판정을 못 낸** 날 판매 후보에게 무슨 일이 일어나는가.

★ 이 파일이 지키는 것은 하나다. *"통과가 아니다"* 안에는 두 가지가 섞여 있고, 둘은
  다음에 할 일이 다르다.

    ```text
    판정이 났고 그 판정이 "안 된다"    reject   → 조건을 바꿔야 한다
    판정 자체가 안 났다                skipped  → 없는 자료를 채워야 한다
    ```

  섞으면 재무가 여신한도를 못 읽은 날 사용자가 **가격을 깎으러 간다.** 가격을 깎아도
  여신한도는 생기지 않으므로 같은 자리에서 또 막힌다.

🔴 **후보를 살려 두는 것과 통과시키는 것은 다른 문제다.** 여기서 늘리는 것은 후보의
  생존이지 통과 어휘가 아니다 — `PASSING_VERDICTS` 는 그대로이고 `skipped` 는 여전히
  통과가 아니다. 승인 문은 `decision.approve_end_codes` 와 `sales_approval` 이
  이미 지키고 있고, 이 파일은 그 문이 열리지 않는 것까지 같이 잰다.
"""

from __future__ import annotations

import pytest

from app.master.decision import SALES_CYCLE, approve_end_codes, decidable_end_codes
from app.master.sales_flow import SalesEndCode
from tests.master.test_sales_flow import (
    _meta,
    _reply,
    adjustment,
    financier,
    flow,
    logistics,
    scenario,
    seller,
)

# ---------------------------------------------------------------------------
# 실제 Finance 회신 모양 — PR #530 이 확정한 계약 그대로
# ---------------------------------------------------------------------------


def finance(kinds: dict[str, str] | None = None, default: str = "ok"):
    """후보별로 **실제 Finance 회신 모양**을 돌려준다.

    ★ `business_status` 만 바꾸지 않는다. 이번 구분은 `runtime_status` 와
      `payload.status` 까지 봐야 성립하므로, 재무가 실제로 보내는 칸을 그대로 싣는다
      (`app/finance/capabilities/sales.py` · `map_sales_finance_verdict`).
    """

    def port(request):
        scenario_id = str(request.payload.get("scenario_id") or "")
        kind = (kinds or {}).get(scenario_id, default)
        if kind == "not_ready":
            reply = _reply(
                request,
                runtime_status="RUNTIME_NOT_READY",
                business_status="skipped",
                missing_data=("partner_credit_limit_krw",),
                payload={
                    "status": "RUNTIME_NOT_READY",
                    "finance_verdict": None,
                    "missing_data": ["partner_credit_limit_krw"],
                    "missing_fields": [],
                    "data_quality": "INCOMPLETE",
                },
                needs_followup=True,
                reasoning="재무 검토에 필요한 정보가 확인되지 않았습니다.",
            )
        elif kind == "input_incomplete":
            reply = _reply(
                request,
                runtime_status="READY",
                business_status="skipped",
                payload={
                    "status": "INPUT_INCOMPLETE",
                    "finance_verdict": None,
                    "missing_fields": ["unit_price_krw"],
                    "missing_data": [],
                    "data_quality": "INCOMPLETE",
                },
                reasoning="판매 제안의 재무 검토에 필요한 정보가 충분하지 않습니다.",
            )
        elif kind == "error":
            reply = _reply(
                request,
                runtime_status="ERROR",
                business_status="skipped",
                reasoning="재무 검토를 끝까지 진행하지 못했습니다.",
            )
        elif kind == "fail":
            reply = _reply(
                request,
                business_status="reject",
                payload={"status": "EVALUATED", "finance_verdict": "FAIL"},
                reasoning="현재 자금 상태에서는 진행하기 어렵습니다.",
            )
        else:
            reply = _reply(
                request,
                business_status="ok",
                payload={"status": "EVALUATED", "finance_verdict": "PASS"},
                reasoning="재무상 추가 조정이 필요하지 않습니다.",
            )
        return reply, _meta(request, reply)

    return port


def run(ids: list[str], kinds: dict[str, str] | None = None, **kw):
    return flow(
        inventory=logistics(),
        sales=seller([[scenario(i, "FINANCIAL_VALIDATION") for i in ids]]),
        finance=finance(kinds),
        **kw,
    ).run()


def candidate(outcome, scenario_id: str):
    found = [c for c in outcome.candidates if c.scenario_id == scenario_id]
    assert found, f"{scenario_id} 후보가 결과에서 사라졌다"
    return found[0]


# ---------------------------------------------------------------------------
# A · B · C — 세 결과가 서로 다른 자리에 앉는다
# ---------------------------------------------------------------------------


def test_finance_pass_후보는_그대로_통과한다():
    out = run(["SCN-1"], {"SCN-1": "ok"})

    assert out.end_code == "SL1_PRESENTED"
    assert [c.scenario_id for c in out.presented] == ["SCN-1"]
    assert out.unresolved == ()


def test_finance_fail_후보는_탈락으로_남는다():
    """판정이 **난** 거절이다 — 미판정 후보와 같은 자리에 놓이면 안 된다."""
    out = run(["SCN-1"], {"SCN-1": "fail"})

    assert out.end_code == "SL3_ALL_REJECTED"
    assert [c.scenario_id for c in out.rejected] == ["SCN-1"]
    # 🔴 핵심 — 탈락은 미판정이 아니다.
    assert out.unresolved == ()
    assert candidate(out, "SCN-1").rejected_validations == ("FINANCIAL_VALIDATION",)
    assert candidate(out, "SCN-1").unresolved_validations == ()


def test_finance_not_ready_후보는_사라지지도_탈락하지도_않는다():
    """🔴 이번 결함의 핵심.

    재무가 여신한도를 못 읽은 것은 **판매 후보의 잘못이 아니다.** 예전에는 이 실행이
    `SL3_ALL_REJECTED` 로 접혀, 아무도 탈락시키지 않은 안이 *"전부 탈락"* 으로
    적혔다 — 사용자는 조건을 바꾸러 가고, 바꿔도 같은 자리에서 또 막힌다.
    """
    out = run(["SCN-1"], {"SCN-1": "not_ready"})

    # 후보가 사라지지 않았다.
    assert [c.scenario_id for c in out.candidates] == ["SCN-1"]
    # 탈락으로 거짓 분류되지 않았다.
    assert out.end_code == "SL6_VALIDATION_UNRESOLVED"
    assert candidate(out, "SCN-1").rejected_validations == ()
    assert candidate(out, "SCN-1").unresolved_validations == ("FINANCIAL_VALIDATION",)
    assert [c.scenario_id for c in out.unresolved] == ["SCN-1"]
    # 그래도 통과는 아니다 — 보지 않은 안을 올리지 않는다.
    assert not candidate(out, "SCN-1").passed
    assert out.presented == ()
    assert not out.presentable


def test_not_ready_와_fail_은_같은_상태가_아니다():
    """B 와 C 가 한 모양이 되면 이번 수정의 뜻이 사라진다."""
    not_ready = run(["SCN-1"], {"SCN-1": "not_ready"})
    failed = run(["SCN-1"], {"SCN-1": "fail"})

    assert not_ready.end_code != failed.end_code
    assert bool(not_ready.unresolved) is not bool(failed.unresolved)


# ---------------------------------------------------------------------------
# D — INPUT_INCOMPLETE 는 FAIL 도 RUNTIME_NOT_READY 도 아니다
# ---------------------------------------------------------------------------


def test_input_incomplete_는_탈락으로_위장되지_않는다():
    out = run(["SCN-1"], {"SCN-1": "input_incomplete"})

    assert out.end_code == "SL6_VALIDATION_UNRESOLVED"
    assert candidate(out, "SCN-1").rejected_validations == ()
    assert not candidate(out, "SCN-1").passed


def test_input_incomplete_와_not_ready_는_원인이_구분된다():
    """둘 다 판정은 없지만 **고칠 사람이 다르다** — 원인을 합치지 않는다.

    ```text
    INPUT_INCOMPLETE   판매/마스터가 재무에 사실을 덜 보냈다  → missing_fields
    RUNTIME_NOT_READY  재무가 자기 자료·정책을 못 가졌다      → missing_data
    ```
    """
    incomplete = candidate(run(["SCN-1"], {"SCN-1": "input_incomplete"}), "SCN-1")
    not_ready = candidate(run(["SCN-1"], {"SCN-1": "not_ready"}), "SCN-1")

    incomplete_v = incomplete.validations["FINANCIAL_VALIDATION"]
    not_ready_v = not_ready.validations["FINANCIAL_VALIDATION"]

    assert incomplete_v["runtime_status"] == "READY"
    assert incomplete_v["payload"]["status"] == "INPUT_INCOMPLETE"
    assert incomplete_v["payload"]["missing_fields"] == ["unit_price_krw"]

    assert not_ready_v["runtime_status"] == "RUNTIME_NOT_READY"
    assert not_ready_v["payload"]["status"] == "RUNTIME_NOT_READY"
    assert not_ready_v["missing_data"] == ["partner_credit_limit_krw"]


# ---------------------------------------------------------------------------
# E — 실제 ERROR
# ---------------------------------------------------------------------------


def test_error_는_not_ready_로_바뀌지_않는다():
    """★ 둘 다 *"판정이 안 났다"* 는 맞지만 **원인을 합치지 않는다.**

    종료 코드는 같은 `SL6` 이다 — 그 층이 답하는 질문이 *"판정이 끝났는가"* 이고
    둘 다 안 끝났기 때문이다. 하지만 **무엇 때문에** 안 끝났는지는 후보 판정 칸에
    부서가 보낸 그대로 남는다. `ERROR` 를 `RUNTIME_NOT_READY` 로 고쳐 적는 자리는
    어디에도 없다.
    """
    out = run(["SCN-1"], {"SCN-1": "error"})
    verdict = candidate(out, "SCN-1").validations["FINANCIAL_VALIDATION"]

    assert verdict["runtime_status"] == "ERROR"
    assert verdict["runtime_status"] != "RUNTIME_NOT_READY"
    assert not candidate(out, "SCN-1").passed
    # 실행 계획에도 ERROR 가 그대로 남는다 — 재시도 판단의 근거다.
    assert out.plan.last("finance", "SALES_VALIDATION").runtime_status == "ERROR"


def test_error_는_검증_완료로_간주되지_않는다():
    out = run(["SCN-1"], {"SCN-1": "error"})

    assert candidate(out, "SCN-1").rejected_validations == ()
    assert candidate(out, "SCN-1").unresolved_validations == ("FINANCIAL_VALIDATION",)


# ---------------------------------------------------------------------------
# F — 후보 셋이 섞인 실제 모양
# ---------------------------------------------------------------------------


def test_통과_미판정_탈락이_한_실행에서_갈린다():
    """A=PASS · B=NOT_READY · C=FAIL — 셋이 서로 다른 자리에 앉아야 한다."""
    out = run(
        ["SCN-A", "SCN-B", "SCN-C"],
        {"SCN-A": "ok", "SCN-B": "not_ready", "SCN-C": "fail"},
    )

    # Flow 전체가 실패로 접히지 않았다.
    assert out.end_code == "SL1_PRESENTED"
    assert out.presentable

    assert [c.scenario_id for c in out.presented] == ["SCN-A"]
    assert [c.scenario_id for c in out.unresolved] == ["SCN-B"]
    # 후보 셋이 전부 결과에 남아 있다.
    assert [c.scenario_id for c in out.candidates] == ["SCN-A", "SCN-B", "SCN-C"]

    assert candidate(out, "SCN-B").unresolved_validations == ("FINANCIAL_VALIDATION",)
    assert candidate(out, "SCN-C").rejected_validations == ("FINANCIAL_VALIDATION",)
    assert candidate(out, "SCN-C").unresolved_validations == ()


# ---------------------------------------------------------------------------
# G — 전부 미판정
# ---------------------------------------------------------------------------


def test_전부_미판정이면_후보가_없다고도_전부_탈락이라고도_하지_않는다():
    """두 거짓말을 동시에 피한다.

    ```text
    SL2_NO_CANDIDATE   "후보가 없다"    → 거짓. 판매는 안을 만들었다.
    SL3_ALL_REJECTED   "전부 탈락했다"  → 거짓. 아무도 탈락시키지 않았다.
    ```

    참인 것은 하나다 — **승인 가능한 후보가 없다.**
    """
    out = run(["SCN-A", "SCN-B"], {"SCN-A": "not_ready", "SCN-B": "not_ready"})

    assert out.end_code == "SL6_VALIDATION_UNRESOLVED"
    assert out.end_code not in {"SL2_NO_CANDIDATE", "SL3_ALL_REJECTED"}
    assert len(out.candidates) == 2
    assert len(out.unresolved) == 2
    assert out.presented == ()
    assert "FINANCIAL_VALIDATION" in out.reason


def test_미판정_사유에_탈락이라고_적지_않는다():
    out = run(["SCN-1"], {"SCN-1": "not_ready"})

    assert "탈락" not in out.reason or "탈락도 아니다" in out.reason


# ---------------------------------------------------------------------------
# H — 승인 문은 닫혀 있다
# ---------------------------------------------------------------------------


def test_미판정_종료코드로는_승인할_수_없다():
    """후보가 살아 있는 것과 승인 가능한 것은 다른 문제다."""
    assert "SL6_VALIDATION_UNRESOLVED" not in approve_end_codes(SALES_CYCLE)
    assert approve_end_codes(SALES_CYCLE) == frozenset({"SL1_PRESENTED"})


def test_미판정_종료코드에도_사람은_결정할_수_있다():
    """★ `SL3` 에서 쪼개져 나온 자리라 결정 가능성은 그대로여야 한다 — 빼면
    예전에 결정을 받던 실행이 조용히 결정 불가가 된다."""
    assert "SL6_VALIDATION_UNRESOLVED" in decidable_end_codes(SALES_CYCLE)


def test_미판정_후보는_승인_대상_목록에_오르지_않는다():
    """`presented` 가 승인 후보 목록이다 — 여기 없으면 고를 수 없다."""
    out = run(["SCN-A", "SCN-B"], {"SCN-A": "not_ready", "SCN-B": "fail"})

    assert out.presented == ()
    assert not out.presentable


def test_미판정_검증은_재검증_통과_어휘_밖이다():
    """최종 재검증이 `skipped` 를 통과로 읽으면 `confirm_sale` 까지 열린다.

    ★ 이 계약은 `revalidation._verdict` 가 이미 갖고 있다 — 여기서 새로 만들지 않고
      **그것이 살아 있는지**만 잰다.
    """
    from app.master.envelope import PASSING_VERDICTS

    assert "skipped" not in PASSING_VERDICTS
    assert PASSING_VERDICTS == frozenset({"ok", "conditional"})


# ---------------------------------------------------------------------------
# 되먹임 — 없는 자료는 후보를 다시 만들어도 생기지 않는다
# ---------------------------------------------------------------------------


def test_재무_미판정만으로는_되먹임하지_않는다():
    """🔴 여신한도가 없는 것은 같은 후보를 다시 만들어서 풀리는 문제가 아니다.

    재무는 미판정일 때 권위 있는 대안을 내지 않으므로 되먹임에 실을 것이 없다.
    그런데도 다시 부르면 호출 예산과 LLM 만 태운다 (C-2 · §1.2-12).
    """
    out = run(["SCN-1"], {"SCN-1": "not_ready"})

    assert out.feedback_attempts == 0
    assert out.plan.call_count("sales", "GENERATE_SALES_PROPOSAL") == 1


def test_권위_있는_대안이_있으면_기존_되먹임_계약은_그대로다():
    """이번 수정이 되먹임 계약을 재설계하지 않았다는 자국."""
    out = flow(
        inventory=logistics(),
        sales=seller([[scenario("SCN-1", "FINANCIAL_VALIDATION")]]),
        finance=financier({"SCN-1": "reject"}, adjustments=(adjustment(),)),
    ).run()

    assert out.feedback_attempts > 0
    assert out.end_code == "SL3_ALL_REJECTED"


# ---------------------------------------------------------------------------
# 계보 — 재무가 보낸 값이 사라지지 않는다
# ---------------------------------------------------------------------------


def test_missing_data_계보가_후보_판정까지_남는다():
    """*"왜 승인 못 하나"* 에 답하려면 **무엇이 없었는지**가 남아야 한다."""
    out = run(["SCN-1"], {"SCN-1": "not_ready"})
    verdict = candidate(out, "SCN-1").validations["FINANCIAL_VALIDATION"]

    assert verdict["missing_data"] == ["partner_credit_limit_krw"]
    assert verdict["payload"]["missing_data"] == ["partner_credit_limit_krw"]
    assert verdict["runtime_status"] == "RUNTIME_NOT_READY"
    assert verdict["business_status"] == "skipped"
    assert verdict["payload"]["finance_verdict"] is None
    # 어느 부서가 그렇게 말했는지도 남는다.
    assert verdict["agent"] == "finance"
    assert verdict["mode"] == "SALES_VALIDATION"


def test_후보_사유에_무엇이_끝나지_않았는지_적힌다():
    out = run(["SCN-1"], {"SCN-1": "not_ready"})
    detail = candidate(out, "SCN-1").detail

    assert "FINANCIAL_VALIDATION" in detail
    assert "RUNTIME_NOT_READY" in detail


@pytest.mark.parametrize(
    "kind", ["not_ready", "input_incomplete", "error", "fail", "ok"]
)
def test_어느_재무_상태에서도_후보_내용은_버려지지_않는다(kind):
    """후보 자체(가격·수량·납기)는 재무 상태와 무관하게 그대로 남는다."""
    out = run(["SCN-1"], {"SCN-1": kind})

    assert len(out.candidates) == 1
    assert candidate(out, "SCN-1").scenario["scenario_id"] == "SCN-1"
    assert candidate(out, "SCN-1").scenario["payment_days"] == 30


# ---------------------------------------------------------------------------
# 어휘 계약
# ---------------------------------------------------------------------------


def test_종료코드_어휘가_한_벌이다():
    from typing import get_args

    assert set(get_args(SalesEndCode)) == {
        "SL1_PRESENTED",
        "SL2_NO_CANDIDATE",
        "SL3_ALL_REJECTED",
        "SL4_NOT_STARTED",
        "SL5_BUDGET_EXHAUSTED",
        "SL6_VALIDATION_UNRESOLVED",
    }


def test_새_통과_어휘를_만들지_않았다():
    """🔴 `skipped` 를 통과로 만드는 것이 아니다 — 후보를 살려 둘 뿐이다."""
    from app.master.envelope import PASSING_VERDICTS

    assert PASSING_VERDICTS == frozenset({"ok", "conditional"})
