"""안이 없을 때 화면이 **왜 없는지**를 말하는가.

실측 2026-08-31 — ML backfill 이 들어와 `forecast` 가 `MEASURED` 가 된 첫 실행에서
매입이 안을 0개 냈다. 화면은 이렇게 나왔다.

```text
보류합니다 — 사람이 봐야 할 지적이 있습니다.
E2_HELD · 호출 3단계

검증
  지적 0건 · 판정하지 못한 검사 0건
```

**"봐야 할 지적이 있다" 는데 지적이 0건이고, 왜 안이 없는지는 어디에도 없다.**
응답에는 있었다 — `reason: "제약 조합 하에 유효한 안이 없어 제안을 내지 못했다."`
머리말이 `_END_HEADLINE` 으로 갈리면서 버려졌다.
"""

from __future__ import annotations

from datetime import date

from app.master.answer import facts_from_procurement


class _Response:
    """`ProcurementRunResponse` 의 최소 대역. 실제 필드 이름을 그대로 쓴다."""

    def __init__(self, **kw):
        self.end_code = kw.get("end_code", "E2_HELD")
        self.reason = kw.get("reason", "")
        self.scenarios = kw.get("scenarios", [])
        self.findings = kw.get("findings", [])
        self.concerns = kw.get("concerns", [])
        self.skipped_checks = kw.get("skipped_checks", [])
        self.blocked_by = kw.get("blocked_by", ())
        self.missing_adapters = kw.get("missing_adapters", ())
        self.single_option = kw.get("single_option", False)
        self.verification_skipped = kw.get("verification_skipped", False)
        self.purchase_attempts = kw.get("purchase_attempts", 1)
        self.input_sources = kw.get("input_sources", {})
        self.mocked_inputs = kw.get("mocked_inputs", [])
        self.as_of = date(2025, 12, 31)
        self.request_id = "REQ-20251231-0001"


def _lines(facts) -> str:
    return " ".join(f"{f.label} {f.value}" for f in facts.facts)


def test_안이_없으면_사유를_적는다():
    facts = facts_from_procurement(
        _Response(reason="제약 조합 하에 유효한 안이 없어 제안을 내지 못했다.")
    )

    assert "제약 조합 하에 유효한 안이 없어" in _lines(facts)


def test_보류_머리말이_없는_지적을_있다고_하지_않는다():
    """`E2_HELD` 는 **지적이 있어 멈춘 것**과 **안이 안 나온 것** 둘을 덮는다."""
    facts = facts_from_procurement(_Response(reason="유효한 안이 없다", findings=[]))

    assert "지적이 있습니다" not in facts.headline
    assert "보류" in facts.headline


def test_안이_있으면_사유를_안_적는다():
    """그때 `reason` 은 `사용자 선택 대기` 라 머리말과 겹친다 — 새 정보가 아니다."""
    facts = facts_from_procurement(
        _Response(
            end_code="E1_APPROVED",
            reason="사용자 선택 대기",
            scenarios=[{"label": "기본"}, {"label": "공격"}],
        )
    )

    적힌_것 = _lines(facts)
    assert "제시한 안" in 적힌_것
    assert "사용자 선택 대기" not in 적힌_것


def test_검증_분수는_안이_없어도_실린다():
    """`findings: []` 를 "문제 없음" 으로 읽게 두지 않는다 — 못 돈 검사 수를 같이 낸다."""
    facts = facts_from_procurement(_Response(reason="없다", skipped_checks=["A", "B"]))

    assert "지적 0건 · 판정하지 못한 검사 2건" in _lines(facts)
