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
from types import SimpleNamespace

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
        self.blocked_failures = kw.get("blocked_failures", ())
        self.adjustments = kw.get("adjustments", ())
        self.missing_adapters = kw.get("missing_adapters", ())
        self.single_option = kw.get("single_option", False)
        self.verification_skipped = kw.get("verification_skipped", False)
        self.purchase_attempts = kw.get("purchase_attempts", 1)
        self.input_sources = kw.get("input_sources", {})
        self.mocked_inputs = kw.get("mocked_inputs", [])
        self.verdicts = kw.get("verdicts", {})
        self.as_of = date(2025, 12, 31)
        self.request_id = "REQ-20251231-0001"


def _adjustment(dept: str, axis: str, value: float, unit: str, reason: str = "사유"):
    """`AdjustmentOut` 최소 대역. 실제 필드 이름을 그대로 쓴다."""
    return SimpleNamespace(
        dept=dept,
        axis=axis,
        target_value=value,
        unit=unit,
        reason=reason,
        ref_ids=["REF-1"],
        scenario_labels=[],
        split_date=None,
    )


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


# ── 조언자 판정 ─────────────────────────────────────────────────────────


def test_조언자_판정을_적는다():
    """🔴 실측 2026-08-31 — 물류가 `conditional` 을 냈는데 화면에 **한 글자도**
    안 나왔다. 마스터는 그 값으로 `_acceptable` 을 정하면서 사람에게는 안 보여줬다."""
    facts = facts_from_procurement(
        _Response(
            end_code="E1_APPROVED",
            scenarios=[{"label": "기본"}],
            verdicts={
                "finance": {"business_status": "ok"},
                "inventory": {"business_status": "conditional"},
            },
        )
    )

    적힌_것 = _lines(facts)
    assert "재무 판정 통과" in 적힌_것
    assert "물류 판정 조건부" in 적힌_것


def test_조건부는_확인해_주세요_에도_올린다():
    """`conditional` 을 `ok` 와 같은 줄에만 두면 **사람이 무조건 통과로 읽는다.**"""
    facts = facts_from_procurement(
        _Response(
            scenarios=[{"label": "기본"}],
            verdicts={"inventory": {"business_status": "conditional"}},
        )
    )

    assert any("조건부입니다" in g for g in facts.gaps)


def test_통과한_부서도_지우지_않는다():
    """통과를 지우면 *"물류만 봤나"* 로 읽힌다 — 누가 봤는지가 답의 일부다."""
    facts = facts_from_procurement(
        _Response(scenarios=[{"label": "기본"}], verdicts={"finance": {"business_status": "ok"}})
    )

    assert "재무 판정 통과" in _lines(facts)


def test_조정_제안이_있으면_드러낸다():
    facts = facts_from_procurement(
        _Response(
            scenarios=[{"label": "기본"}],
            verdicts={"finance": {"business_status": "ok"}},
            adjustments=(_adjustment("finance", "amount", 18000000.0, "krw"),),
        )
    )

    assert any("조정을 제안했습니다 (1건)" in g for g in facts.gaps)


def test_조정_제안의_내용을_적는다():
    """🔴 전에는 개수만 말하고 *"실행 이력에서 보십시오"* 로 끝냈다 (2026-09-02).

    **실행 이력에 없었다.** 가서 봐도 없는 곳을 알려 주고 있었다.
    """
    facts = facts_from_procurement(
        _Response(
            scenarios=[{"label": "기본"}],
            verdicts={"inventory": {"business_status": "conditional"}},
            adjustments=(_adjustment("inventory", "quantity", 7120.0, "kg", "창고가 모자랍니다"),),
        )
    )

    적힌_것 = " ".join(facts.gaps)
    assert "7120kg" in 적힌_것, "목표값이 안 보이면 개수만 말하던 때와 같다"
    assert "창고가 모자랍니다" in 적힌_것, "부서가 쓴 사유를 그대로 옮긴다"
    assert "실행 이력에서 보십시오" not in 적힌_것, "없는 곳을 가리키던 문장이 남았다"


def test_남의_부서_조정을_자기_것으로_적지_않는다():
    """`AgentName` 과 `Dept` 는 지금 글자가 같을 뿐 **어휘가 다르다.**"""
    facts = facts_from_procurement(
        _Response(
            scenarios=[{"label": "기본"}],
            verdicts={"finance": {"business_status": "ok"}},
            adjustments=(_adjustment("inventory", "quantity", 7120.0, "kg"),),
        )
    )

    assert not any("재무 가 조정을 제안" in g for g in facts.gaps)


def test_조정이_0건이면_아무_줄도_안_낸다():
    """물류는 `reject` 안의 조정을 승격하지 않는다 — **0건이 정답인 날이 있다.**"""
    facts = facts_from_procurement(
        _Response(
            scenarios=[{"label": "기본"}],
            verdicts={"inventory": {"business_status": "reject"}},
            adjustments=(),
        )
    )

    assert not any("조정을 제안" in g for g in facts.gaps)


def test_모르는_판정값은_적지_않는다():
    """부서가 새 라벨을 내면 **추측해서 번역하지 않는다** — 모르는 것은 안 적는다."""
    facts = facts_from_procurement(
        _Response(scenarios=[{"label": "기본"}], verdicts={"finance": {"business_status": "???"}})
    )

    assert "재무 판정" not in _lines(facts)
