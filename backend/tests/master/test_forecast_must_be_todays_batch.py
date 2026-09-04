"""**당일 ML 배치가 아니면 예측을 안 쓴다.**

2026-09-04 실측에서 나온 구멍이다. `_forecast_from_db` 가 `as_of <= %s` 로 최신
배치를 집는데 **지연 상한이 없어서**, 그날 배치가 없으면 조용히 옛 배치를 집고
`grade="MEASURED"` 로 실어 보냈다.

```text
as_of        집은 배치      지연     등급
2026-09-04   2026-09-04     0일      MEASURED   ← 정상
2026-08-25   2026-01-27     210일    MEASURED   ← 🔴 결함
2026-08-01   2026-01-27     186일    MEASURED   ← 🔴 결함
2026-06-01   2026-01-27     125일    MEASURED   ← 🔴 결함
```

**210일 전 예측으로 오늘 매입안을 만들었다.** `#227` 이 *"ML DB 가 죽으면 선다"* 를
만들었는데, **배치가 없는 날은 죽은 것으로 안 쳤다.**

★ **DB 를 타지 않는다.** `test_inputs.py` 와 같은 방식으로 조회 함수를 갈아 끼운다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.master import inputs

AS_OF = date(2026, 8, 25)

#: 실측에서 2026-08-25 요청이 실제로 집어 온 배치일. 210일 전이다.
STALE_BATCH = date(2026, 1, 27)


def _row(batch_as_of: date) -> dict[str, Any]:
    """뷰 한 행. `as_of` 만 바꿔 가며 쓴다."""
    return {
        "as_of": batch_as_of,
        "item": "배추",
        "target_kind": "AUC",
        "generated_at": f"{batch_as_of}T06:00:00+09:00",
        "unit": "원/kg",
        "current_price": 645,
        "horizon_days": 18,
        "daily": [{"date": "2026-08-26", "predicted": 645}],
        "model_version": "ops_auc",
        "quality_note": "세 구간 모두 양수",
        "use_recommended": True,
    }


def patch(monkeypatch, one):
    monkeypatch.setattr(inputs, "fetch_one", one)
    monkeypatch.setattr(inputs, "fetch_all", lambda *a: [])
    monkeypatch.setattr(inputs, "get_db_schema", lambda: "haetdeul")


# ── 당일이면 쓴다 ────────────────────────────────────────────────────────


def test_당일_배치면_MEASURED_로_실린다(monkeypatch):
    """정상 경로가 안 막혔는지부터 본다 — 이게 없으면 아래 검사는 '전부 막기' 로도 통과한다."""
    patch(monkeypatch, lambda *a: _row(AS_OF))
    got = inputs.load_forecast("배추", AS_OF)

    assert got.grade == "MEASURED"
    assert got.payload is not None
    assert got.payload["current_price"] == 645
    assert f"as_of={AS_OF}" in got.source


# ── 하루만 밀려도 안 쓴다 ────────────────────────────────────────────────


def test_어제_배치밖에_없으면_MISSING_이다(monkeypatch):
    """**하루만 밀려도 안 쓴다.**

    ★ "며칠까지는 봐 준다" 는 상한을 두지 않는다. 그 상한을 정하는 근거가 없고,
      한 번 두면 그 숫자가 다음 사람에게 *"이만큼은 정상"* 으로 읽힌다.
    """
    yesterday = date(2026, 8, 24)
    patch(monkeypatch, lambda *a: _row(yesterday))
    got = inputs.load_forecast("배추", AS_OF)

    assert got.grade == "MISSING"
    assert got.payload is None, "당일 배치가 아닌데 값이 실렸다"


def test_210일_밀린_배치가_MEASURED_로_안_나간다(monkeypatch):
    """🔴 **제가 실측한 그 구멍의 회귀 검사다** (2026-09-04).

    ```text
    as_of=2026-08-25  → 집은 배치 2026-01-27  → 지연 210일  → 등급 MEASURED
    ```

    이 검사가 빨개지면 210일 전 예측으로 오늘 매입안을 만드는 길이 다시 열린 것이다.
    """
    patch(monkeypatch, lambda *a: _row(STALE_BATCH))
    got = inputs.load_forecast("배추", AS_OF)

    assert got.grade == "MISSING", "210일 전 예측이 실측으로 나갔다"
    assert got.payload is None
    # 실측 지연일수. 이 전제가 깨지면 이 검사가 재는 것이 그 구멍이 아니다.
    assert (AS_OF - STALE_BATCH).days == 210


# ── 사유가 세 가지를 다 말한다 ────────────────────────────────────────────


def test_사유에_요청일과_최신배치일과_지연일수가_다_있다(monkeypatch):
    """사람이 읽고 **"언제 것을 집을 뻔했는지"** 를 알아야 한다.

    지연일수가 없으면 두 날짜를 눈으로 빼야 하고, 210일과 1일이 같은 문장으로 보인다.
    """
    patch(monkeypatch, lambda *a: _row(STALE_BATCH))
    note = inputs.load_forecast("배추", AS_OF).note

    assert "2026-08-25" in note, f"요청한 날이 없다: {note}"
    assert "2026-01-27" in note, f"실제로 있는 최신 배치일이 없다: {note}"
    assert "210일" in note, f"지연일수가 없다: {note}"


def test_사유가_원인을_단정하지_않는다(monkeypatch):
    """⚠️ 당일 배치가 없는 이유가 공휴일인지 ML 미실행인지 적재 지연인지 **마스터는
    구분할 수 없다.** 뷰에는 "배치가 있다/없다" 만 있고 "왜 없다" 가 없다.

    사유가 원인을 단정하면 다음 사람이 엉뚱한 데를 판다 — 사실만 적는다.
    """
    patch(monkeypatch, lambda *a: _row(STALE_BATCH))
    note = inputs.load_forecast("배추", AS_OF).note

    for 단정 in ("공휴일", "휴장", "장애", "휴일", "미실행"):
        assert 단정 not in note, f"원인을 단정했다({단정}): {note}"


# ── look-ahead 방지가 안 걷혔다 ───────────────────────────────────────────


def test_조회가_여전히_미래_배치를_막는다(monkeypatch):
    """★ 당일 동일성을 더하면서 `as_of <= ` 를 걷으면 **미래 배치를 집는다.**

    그러면 백테스트 성적이 통째로 무효가 된다 (look-ahead). 조회가 그 조건을
    실제로 들고 있는지, 그리고 `as_of` 를 파라미터로 넘기는지 본다.
    """
    seen: dict[str, Any] = {}

    def watching(query, params):
        seen["sql"] = query.as_string(None) if hasattr(query, "as_string") else str(query)
        seen["params"] = params
        return _row(AS_OF)

    patch(monkeypatch, watching)
    inputs.load_forecast("배추", AS_OF)

    assert "as_of <= %s" in seen["sql"], f"look-ahead 방지가 걷혔다: {seen['sql']}"
    assert AS_OF in seen["params"], "as_of 를 조회에 안 넘긴다 — 상한이 무의미해진다"


# ── 기존 동작 회귀 ───────────────────────────────────────────────────────


def test_배치가_아예_없으면_지금처럼_MISSING_이다(monkeypatch):
    """`#227` 이 만든 경로다. 당일 규칙이 이 갈래를 가리면 안 된다."""
    patch(monkeypatch, lambda *a: None)
    got = inputs.load_forecast("배추", AS_OF)

    assert got.grade == "MISSING"
    assert got.payload is None
    assert "예측 배치가 없다" in got.note


# ── 통합 — MISSING 이면 매입안을 안 만든다 ────────────────────────────────


def test_당일_배치가_없으면_run_procurement_이_E4_로_선다(monkeypatch):
    """★ **새 정지 경로를 만들지 않았다.** `#227` 이 낸 그 길을 그대로 탄다.

    ```text
    forecast MISSING → 매입 payload 에 forecast 없음 → RUNTIME_NOT_READY → E4_NOT_STARTED
    ```
    """
    from app.master import wiring
    from app.master.envelope import AgentReply, AgentRequest, ExecutionMetadata
    from app.master.inputs import MasterInputs, SourcedInput
    from app.master.schemas import ProcurementRunRequest
    from app.master.service import run_procurement

    patch(monkeypatch, lambda *a: _row(STALE_BATCH))  # 210일 전 배치만 있다

    def 실제_적재(item: str, as_of: date) -> MasterInputs:
        """conftest 가 꺼 둔 적재를 이 검사에서만 되살린다 — forecast 만 실물로 태운다."""
        return MasterInputs(
            forecast=inputs.load_forecast(item, as_of),
            confirmed_orders=SourcedInput("confirmed_orders", {"total_kg": 1.0}, "DERIVED", "뷰"),
            policy_values=SourcedInput("policy_values", {"item_mix_ratio": {}}, "DERIVED", "표"),
        )

    monkeypatch.setattr("app.master.service.collect_inputs", 실제_적재)
    monkeypatch.setattr("app.master.service.persistence.record", lambda *a, **k: None)

    seen: list[dict[str, Any]] = []

    def port(request: AgentRequest):
        run_id = f"{request.agent.upper()}-{request.call_seq}"
        payload: dict[str, Any] = {"cap": 1}
        runtime, business, missing = "READY", "ok", ()
        if request.agent == "purchase":
            seen.append(dict(request.payload))
            payload = {"scenarios": [{"scenario_id": "SCN-1"}]}
            if not request.payload.get("forecast"):
                # 실 매입 어댑터가 forecast 없이 답하는 모양 — `#227` 이 낸 그 경로다
                runtime, business, missing = "RUNTIME_NOT_READY", "skipped", ("forecast",)
                payload = {"scenarios": []}
        reply = AgentReply(
            request_id=request.context.request_id,
            as_of=request.context.as_of,
            agent=request.agent,
            mode=request.mode,
            run_id=run_id,
            runtime_status=runtime,
            business_status=business,
            payload=payload,
            missing_data=missing,
        )
        return reply, ExecutionMetadata(
            run_id=run_id, request_id=request.context.request_id, agent=request.agent
        )

    wiring.reset()
    wiring.register("finance", port)
    wiring.register("inventory", port)
    wiring.register("purchase", port)

    response = run_procurement(
        ProcurementRunRequest(
            as_of=AS_OF, policy_version="v1.3", item="배추", request_id="REQ-STALE-1"
        ),
        verifier=None,
    )
    wiring.reset()

    assert seen, "매입이 불려야 이 검사가 의미 있다"
    assert not seen[0].get("forecast"), "210일 전 예측이 매입까지 갔다"
    assert response.end_code == "E4_NOT_STARTED"
    assert response.input_sources["forecast"].startswith("MISSING:"), response.input_sources
