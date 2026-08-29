"""부서 간 이름·단위 번역 — **🔴 한시 조치가 드러나는지**를 잠근다.

이 파일이 지키는 명제는 둘이다.

```text
바꾼 것은 반드시 기록에 남는다      조용히 맞추면 "합의가 안 됐다" 는 사실이 사라진다
뜻이 다르면 바꾸지 않고 비운다      숫자는 들어가고 뜻이 틀리는 것이 가장 나쁘다
```
"""

from __future__ import annotations

from app.master.interop import floor_kg, translate_inventory, translate_lots

LOTS = [
    {
        "lot_id": "LOT-A",
        "item": "배추",
        "available_qty_kg": 286.92,
        "remaining_freshness_days": 10,
        "grade": None,
        "status": "STORED",
    }
]


def test_이름만_다른_것은_바꾸고_기록한다():
    lots, notes = translate_lots(LOTS)

    assert lots[0]["remaining_kg"] == 286.92
    assert "available_qty_kg" not in lots[0]
    assert any("available_qty_kg → remaining_kg" in n for n in notes)


def test_뜻이_다른_칸은_채우지_않는다():
    """🔴 **여기가 이 모듈의 핵심이다.**

    물류의 `remaining_freshness_days`(기존 로트의 잔여)를 매입의 `shelf_life_days`
    (새로 살 물건의 전체 유통기한) 칸에 넣으면 **숫자는 들어가고 뜻이 틀린다.**
    매입이 미결로 처리하도록 비워 두는 쪽이 맞다.
    """
    lots, notes = translate_lots(LOTS)

    assert "shelf_life_days" not in lots[0]
    assert "stocked_at" not in lots[0]
    assert any("shelf_life_days" in n and "뜻이 다르므로" in n for n in notes)
    assert any("stocked_at" in n and "거짓 입고일" in n for n in notes)


def test_원래_값을_지우지_않는다():
    """번역은 **덧붙이는 것이 아니라 이름만 바꾸는 것**이고, 나머지는 그대로 간다."""
    lots, _ = translate_lots(LOTS)

    assert lots[0]["lot_id"] == "LOT-A"
    assert lots[0]["remaining_freshness_days"] == 10
    assert lots[0]["grade"] is None  # None 을 임의 등급으로 채우지 않는다


def test_빈_로트는_아무것도_하지_않는다():
    assert translate_lots([]) == ([], [])
    assert translate_lots(None) == ([], [])


# ── kg 소수/정수 ────────────────────────────────────────────────────────


def test_kg_은_반올림이_아니라_내림이다():
    """**덜 사는 방향을 고른다** — 창고를 넘겨 사는 것보다 안전하다."""
    out, notes = floor_kg({"warehouse_free_kg": 7636.72, "used_capacity_kg": 363.28})

    assert out["warehouse_free_kg"] == 7636  # 7637 이 아니다
    assert out["used_capacity_kg"] == 363
    assert any("내림" in n for n in notes)


def test_정수는_건드리지_않는다():
    out, notes = floor_kg({"guaranteed_capacity_kg": 8000.0, "lot_count": 4})

    assert out["guaranteed_capacity_kg"] == 8000.0
    assert notes == []


def test_금액은_내리지_않는다():
    """수량이 정수가 되면 금액도 따라 정수가 된다 — 여기서 함께 내리면 **두 번 깎인다.**"""
    out, _ = floor_kg({"finance_cap_amount_krw": 31854627.5})

    assert out["finance_cap_amount_krw"] == 31854627.5


def test_로트와_cap_by_date_안의_소수도_내린다():
    out, notes = floor_kg(
        {
            "cap_by_date": {"2026-01-02": 1234.5},
            "lots": [{"lot_id": "L", "remaining_kg": 286.92}],
        }
    )

    assert out["cap_by_date"]["2026-01-02"] == 1234
    assert out["lots"][0]["remaining_kg"] == 286
    assert len(notes) == 1  # 한 문장으로 묶어 낸다


def test_바꾼_것이_없으면_기록도_없다():
    """**할 말이 없으면 안 한다.** 매번 문장을 내면 진짜 우회가 묻힌다."""
    _, notes = translate_inventory({"warehouse_free_kg": 8000, "lots": []})

    assert notes == []


def test_이름과_단위를_한_번에_처리한다():
    payload = {"warehouse_free_kg": 7636.72, "lots": LOTS}
    out, notes = translate_inventory(payload)

    assert out["warehouse_free_kg"] == 7636
    assert out["lots"][0]["remaining_kg"] == 286  # 이름도 바뀌고 내림도 됐다
    assert len(notes) >= 4  # 이름 · shelf_life · stocked_at · kg
