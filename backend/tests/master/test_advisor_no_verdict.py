"""조언자가 **판정을 못 냈을 때** 세 곳이 같은 말을 하는가.

실측 2026-08-31 — 물류가 기준일 불일치를 fail-closed 로 막으면서
`runtime_status=ERROR` · `business_status=skipped` 를 내기 시작했다 (물류 회신 §1-5).
그 봉투를 실제 실행 응답에 얹어 재현했더니 세 곳이 서로 다르게 말했다.

```text
화면     라벨 표에 없는 판정을 continue 로 건너뛴다  → 물류가 통째로 사라진다
보고서   라벨 표에 없는 값을 그대로 찍는다            → 영어 "skipped" 가 뜬다
검증     plan.called() 는 "불렀나" 만 본다            → 답을 못 받아도 concern 0건
```

🔴 **"안 물어봤다" 와 "물어봤는데 못 답했다" 가 화면에서 같아 보였다.** 앞엣것은
마스터 배선 문제고 뒤엣것은 그 부서 문제라 사람이 할 일이 완전히 다르다.

★ 그렇다고 **모르는 라벨까지 "내지 못함" 으로 적으면 안 된다.** 그것은 부서가 안 한
  일을 했다고 하는 것이다 — `test_no_plan_answer.py` 의 선 결정을 이 파일이 함께 지킨다.
"""

from __future__ import annotations

from datetime import date

from app.master.answer import facts_from_procurement
from app.master.report import render_report
from app.master.verifier import MasterVerifier


class _Response:
    """`ProcurementRunResponse` 의 최소 대역. 실제 필드 이름을 그대로 쓴다."""

    def __init__(self, **kw):
        self.end_code = kw.get("end_code", "E1_APPROVED")
        self.reason = kw.get("reason", "")
        self.scenarios = kw.get("scenarios", [{"label": "기본"}])
        self.findings = kw.get("findings", [])
        self.concerns = kw.get("concerns", [])
        self.skipped_checks = kw.get("skipped_checks", [])
        self.blocked_by = kw.get("blocked_by", ())
        self.blocked_failures = kw.get("blocked_failures", ())
        self.missing_adapters = kw.get("missing_adapters", ())
        self.single_option = kw.get("single_option", False)
        self.verification_skipped = kw.get("verification_skipped", False)
        self.purchase_attempts = kw.get("purchase_attempts", 1)
        self.input_sources = kw.get("input_sources", {})
        self.mocked_inputs = kw.get("mocked_inputs", [])
        self.verdicts = kw.get("verdicts", {})
        self.as_of = date(2025, 12, 31)
        self.request_id = "REQ-20251231-0001"


#: 물류가 실제로 내는 모양 (물류 회신 §1-5).
_AS_OF_MISMATCH = {
    "business_status": "skipped",
    "runtime_status": "ERROR",
    "payload": {"validation_errors": ["proposal.meta.as_of"]},
    "suggested_adjustments": 0,
    "needs_followup": False,
    "reasoning": "Purchase proposal as-of does not match the Master request.",
}


def _text(facts) -> str:
    lines = [f"{f.label} {f.value}" for f in facts.facts]
    return " ".join(lines + list(facts.gaps))


def test_판정을_못_낸_부서가_화면에서_사라지지_않는다():
    facts = facts_from_procurement(_Response(verdicts={"inventory": _AS_OF_MISMATCH}))

    assert "물류" in _text(facts), "부서가 통째로 빠지면 '안 물어봤다' 와 구분되지 않는다"
    assert "내지 못함" in _text(facts)


def test_왜_못_냈는지를_같이_적는다():
    """사유가 없으면 읽는 사람이 할 수 있는 것이 없다."""
    facts = facts_from_procurement(_Response(verdicts={"inventory": _AS_OF_MISMATCH}))

    assert "as-of does not match" in _text(facts)


def test_사유가_비어도_빈칸으로_두지_않는다():
    verdict = {**_AS_OF_MISMATCH, "reasoning": ""}
    facts = facts_from_procurement(_Response(verdicts={"inventory": verdict}))

    assert "사유 미기재" in _text(facts)


def test_모르는_라벨은_내지_못함으로_번역하지_않는다():
    """🔴 판정을 **했는데 모르는 값**이다. '안 냈다' 고 쓰면 거짓말이다."""
    facts = facts_from_procurement(
        _Response(
            verdicts={"finance": {"business_status": "???", "runtime_status": "READY"}}
        )
    )

    assert "내지 못함" not in _text(facts)


def test_보고서가_영어_코드를_그대로_찍지_않는다():
    run = {"request_id": "REQ-1", "as_of": "2025-12-31", "verdicts": {"inventory": _AS_OF_MISMATCH}}

    markdown = render_report(run)

    assert "판정을 내지 못함" in markdown
    assert "as-of does not match" in markdown


def test_검증이_판정_못_받은_사실에_운다():
    """`plan.called()` 는 '불렀나' 만 본다 — 답을 못 받은 것은 따로 봐야 한다."""
    concerns: list[str] = []

    MasterVerifier()._check_advisor_answered({"inventory": _AS_OF_MISMATCH}, concerns)

    assert len(concerns) == 1
    assert "ADVISOR-NO-VERDICT" in concerns[0]
    assert "판정을 내지 않았다" in concerns[0]


def test_검증은_모르는_라벨을_다르게_적는다():
    """고칠 곳이 다르다 — 앞은 그 부서, 뒤는 **마스터의 어휘가 낡은 것**이다."""
    concerns: list[str] = []

    MasterVerifier()._check_advisor_answered(
        {"finance": {"business_status": "???", "runtime_status": "READY"}}, concerns
    )

    assert "모르는 판정값" in concerns[0]
    assert "판정을 내지 않았다" not in concerns[0]


def test_정상_판정에는_울지_않는다():
    concerns: list[str] = []

    MasterVerifier()._check_advisor_answered(
        {
            "finance": {"business_status": "ok", "runtime_status": "READY"},
            "inventory": {"business_status": "conditional", "runtime_status": "READY"},
        },
        concerns,
    )

    assert concerns == []
