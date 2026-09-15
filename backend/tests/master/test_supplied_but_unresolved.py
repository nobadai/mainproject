"""`SUPPLIED-BUT-UNRESOLVED` — **실린 값을 미결이라 답하는가.**

매입 8/31 회신에서 나온 둘을 고정한다.

```text
① 12자 창                매입 문구가 13자라 안 울렸다 — 검사가 남의 문장 길이를 정했다
② 최상위만 봄            operational_limit_days 는 item_storage_policies[] 안이라 안 보였다
```

★ **②가 더 무섭다.** ①은 안 울려서 눈에 띄지만, ②는 *"②③ 배선하면 이 검사가
  울린다"* 고 클래스 주석에 예고까지 해 둔 채로 조용했다. **예고가 검사를
  대신하지 않는다.**
"""

from __future__ import annotations

from app.master.verifier import supplied_but_unresolved


def _concerns(constraints: dict, risks: list[str]) -> list[str]:
    """🔴 **공개 진입점으로 부른다.** 매입도 같은 문을 쓴다 (8/31) — 검사와 실물이
    다른 코드를 타면 *"통과했는데 실물은 0건"* 이 다시 생긴다."""
    return supplied_but_unresolved([{"label": "기본", "risks": risks}], constraints)


# ── ① 문장 길이가 검사를 좌우하지 않는다 ─────────────────────────────────


def test_매입이_쓰려던_문구가_울린다():
    """`operational_limit_days는 받았으나 등급 어휘 미확정(#69)` — 13자였다."""
    out = _concerns(
        {"inventory": {"item_storage_policies": [{"item": "배추", "operational_limit_days": 10}]}},
        ["operational_limit_days는 받았으나 등급 어휘 미확정(#69) 로 등급 배분 보류"],
    )

    assert len(out) == 1
    assert "operational_limit_days" in out[0]


def test_긴_문장도_울린다():
    out = _concerns(
        {"inventory": {"inbound_lead_days": 2.0}},
        ["inbound_lead_days 를 받아 도착일을 계산하려 했으나 아직 미확정 상태다"],
    )

    assert len(out) == 1


# ── ② 원래 오탐은 살아나지 않는다 ────────────────────────────────────────


def test_다른_키_얘기면_건너뛴다():
    """🔴 12자 창이 막던 것. **사이에 다른 실린 키가 있으면 그 키 얘기다.**

    같은 원인의 파생을 두 번 보고하면 읽는 사람이 원인을 둘로 센다.
    """
    out = _concerns(
        {"inventory": {"cap_by_date": {"2026-01-02": 7636.72}, "inbound_lead_days": 2.0}},
        ["cap_by_date 검사는 inbound_lead_days(N4) 미확정으로 보류"],
    )

    assert len(out) == 1
    assert "inbound_lead_days" in out[0]
    assert "'cap_by_date'" not in out[0]


def test_문장이_바뀌면_다른_얘기다():
    out = _concerns(
        {"inventory": {"warehouse_free_kg": 7636.72}},
        ["warehouse_free_kg 로 상한을 잡았다. 등급 어휘는 미확정이다"],
    )

    assert out == []


# ── 실린 값만 본다 ───────────────────────────────────────────────────────


def test_None_인_칸은_지적하지_않는다():
    """로트 `grade` 는 전부 `None` 이다 — *"grade 미확정"* 은 **맞는 말**이고,
    맞는 말을 지적으로 올리면 안 된다."""
    out = _concerns(
        {"inventory": {"lots": [{"lot_id": "L1", "qty_kg": 10.0, "grade": None}]}},
        ["grade 미확정으로 등급 배분을 못 했다"],
    )

    assert out == []


def test_중첩_한_겹까지_본다():
    out = _concerns(
        {"inventory": {"item_storage_policies": [{"item": "배추", "medium_grade_factor": 0.6}]}},
        ["medium_grade_factor 미확정"],
    )

    assert len(out) == 1
    assert "medium_grade_factor" in out[0]


def test_봉투_메타는_대조하지_않는다():
    out = _concerns(
        {"inventory": {"soft_warnings": ["X"], "as_of": "2025-12-31"}},
        ["soft_warnings 미확정", "as_of 미확정"],
    )

    assert out == []


def test_같은_키를_두_번_보고하지_않는다():
    out = _concerns(
        {"inventory": {"inbound_lead_days": 2.0}},
        ["inbound_lead_days 미확정", "inbound_lead_days 미확정 (다시)"],
    )

    assert len(out) == 1


def test_공개_진입점이_관통과_같은_것을_본다():
    """🔴 매입이 8/31 에 겪은 일 — **규칙을 재현하면 검사와 실물이 갈린다.**

    정규식만 빌려 재현했더니 검사는 통과하는데 실물은 `concerns` 0건이었다.
    관문이 둘인데(supplied 조회 · 미결 어휘) 하나만 재현했기 때문이다.

    이 검사는 **관통이 쓰는 것과 같은 함수**를 부르는지 붙잡는다.
    """
    from app.master import verifier

    seen: list[str] = []
    verifier.MasterVerifier()._check_supplied_but_unused(
        ({"label": "기본", "risks": ["inbound_lead_days 미확정"]},),
        {"inventory": {"inbound_lead_days": 2.0}},
        seen,
    )
    공개 = supplied_but_unresolved(
        [{"label": "기본", "risks": ["inbound_lead_days 미확정"]}],
        {"inventory": {"inbound_lead_days": 2.0}},
    )

    assert seen == 공개, "공개 자리와 관통이 다른 답을 내면 매입 검사가 또 헛돈다"
