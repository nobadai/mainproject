"""**장부 관문이 막은 날에 행 하나를 남긴다.**

🔴 **왜 필요한가.** 매입 화면이 세 가지를 한 문구로 보여준다.

```text
① 장부 게이트가 막았다        →  "사유를 남긴 실행이 없습니다"
② 스케줄러가 그날을 안 돌렸다  →  "사유를 남긴 실행이 없습니다"
③ 돌렸는데 적재가 실패했다     →  "사유를 남긴 실행이 없습니다"
```

`①` 은 정상 동작이고 `②` 는 운영 공백이고 `③` 은 사고다. 이 파일이 잠그는 것은
**`①` 하나뿐**이다 — `②` `③` 은 여전히 행이 없고, 그것은 이 판의 범위가 아니다.

⚠️ **DB 를 안 탄다.** 적재도 조회도 전부 대역으로 준다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import pytest

from app.master import persistence, scheduler
from app.master.clock import SEOUL
from app.master.commitment import ITEM_CODES
from app.master.forecast_gate import DayForecastReadiness, ItemForecastGate
from app.master.scheduler import daily_request_id, ledger_gap_request_id, run_scheduled_day

AS_OF = date(2026, 9, 8)
ITEMS = ("무", "배추", "양파")


# ── 대역 ────────────────────────────────────────────────────────────────


@dataclass
class _Out:
    """서비스 함수가 내는 값의 최소 모양."""

    status: str
    reason: str = ""


class _Spy:
    """서비스 함수 대역. **몇 번 불렸는지**를 센다."""

    def __init__(self, out: object = None) -> None:
        self.out = out
        self.calls: list[object] = []

    def __call__(self, arg, *args, **kwargs):
        self.calls.append(arg)
        return self.out


class _Procure:
    """`run_procurement` 대역. 불리면 안 되는 자리를 잠그기 위해 요청을 모은다."""

    def __init__(self) -> None:
        self.requests: list[object] = []

    def __call__(self, request, verifier=None):
        self.requests.append(request)
        return _Out(status="RAN")


class _Record:
    """`persistence.record_ledger_gap` 대역. **인자를 그대로 모은다.**"""

    def __init__(self, boom: Exception | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.boom = boom

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.boom is not None:
            raise self.boom
        return "run-1"


class _Calendar:
    def is_market_open(self, as_of: date) -> bool:
        return True


_ALL_READY = DayForecastReadiness(
    as_of=AS_OF,
    readiness="ALL_READY",  # type: ignore[arg-type]
    items=tuple(
        ItemForecastGate(item=item, as_of=AS_OF, readiness="READY", grade="MEASURED")  # type: ignore[arg-type]
        for item in ITEMS
    ),
)


def _plan() -> scheduler.ScheduledAction:
    return scheduler.plan_next_action(
        now=datetime(AS_OF.year, AS_OF.month, AS_OF.day, 9, 30, tzinfo=SEOUL),
        as_of=AS_OF,
        calendar=_Calendar(),
        gate_result=_ALL_READY,
    )


def _run(*, inbound="RECEIVED", receivable="ISSUED", collection="COLLECTED", **kwargs):
    """하루를 건다. **막을 단계만 인자로 바꾼다.**"""
    procure = _Procure()
    defaults = {
        "open_day_fn": _Spy(_Out("OPENED")),
        "receive_fn": _Spy(_Out(inbound)),
        "issue_fn": _Spy(_Out(receivable)),
        "collect_fn": _Spy(_Out(collection)),
        "procure_fn": procure,
        "outbound_fn": _Spy(_Out("NOTHING_DUE")),
        "items": ITEMS,
    }
    defaults.update(kwargs)
    return run_scheduled_day(_plan(), **defaults), procure  # type: ignore[arg-type]


@pytest.fixture
def 적재(monkeypatch) -> _Record:
    """관문 적재를 대역으로 바꾼다. **진짜는 DB 를 찾아간다.**"""
    spy = _Record()
    monkeypatch.setattr(persistence, "record_ledger_gap", spy)
    return spy


# ══════════════════════════════════════════════════════════════════════
#  ① 관문이 막은 날 — 행이 생긴다
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("막힌상태", ["BLOCKED", "FAILED"])
def test_관문이_막으면_행을_남긴다(적재, 막힌상태):
    """🔴 **행이 없으면 화면이 정상 동작을 사고와 같은 문구로 그린다.**"""
    out, _ = _run(inbound=막힌상태)

    assert out.procurement_status == "NOT_ATTEMPTED"
    assert len(적재.calls) == 1, "관문이 막았는데 행을 안 남겼다"


def test_관문이_안_막은_날에는_행을_안_남긴다(적재):
    """🔴 **정상인 날에 이 행이 생기면 미가동 날 수가 부풀고, 그 수가 곧 거짓이 된다.**

    ★ **자기 생존.** 막은 날에 실제로 불리는 것을 같은 검사가 확인한다 — 대역이
      영영 안 불려도 초록이 되는 검사를 만들지 않는다.
    """
    out, procure = _run()

    assert out.procurement_status == "RAN"
    assert len(procure.requests) == len(ITEMS)
    assert 적재.calls == [], "관문이 안 막았는데 행을 남겼다"

    _run(collection="BLOCKED")
    assert len(적재.calls) == 1, "막은 날에도 안 불린다 — 대역이 아예 배선되지 않았다"


def test_행을_남겨도_판단은_여전히_안_돈다(적재):
    """🔴 **남기는 것은 행이지 판단이 아니다.** 흐려지면 *"판단을 돌렸다"* 로 읽힌다."""
    _, procure = _run(receivable="BLOCKED")

    assert procure.requests == [], "행을 남기면서 판단까지 돌렸다"
    assert len(적재.calls) == 1


def test_출고도_여전히_안_돈다(적재):
    """★ 관문 뒤 동작이 하나도 안 바뀌었다는 것 — 행 하나만 더해졌다."""
    shipped = _Spy(_Out("NOTHING_DUE"))
    out, _ = _run(inbound="BLOCKED", outbound_fn=shipped)

    assert shipped.calls == [], "장부가 안 섰는데 물건이 나갔다"
    assert out.outbound_status == "NOT_ATTEMPTED"


# ══════════════════════════════════════════════════════════════════════
#  ② 무엇을 넘기나 — 사유를 **다시 짓지 않는다**
# ══════════════════════════════════════════════════════════════════════


def test_사유를_다시_짓지_않는다(적재):
    """🔴 **문장의 주인은 `_ledger_gap_note` 하나다.**

    두 벌이 되면 한쪽만 고치는 날 화면과 이력이 갈리고, 그때 어느 쪽이 참인지
    아무도 모른다.
    """
    _run(inbound="BLOCKED", collection="FAILED")

    넘긴사유 = 적재.calls[0]["reason"]
    assert 넘긴사유 == scheduler._ledger_gap_note("BLOCKED", "ISSUED", "FAILED")


def test_넘긴_사유가_결과에_담긴_문장과_같다(적재):
    """★ 같은 문장이 `notes` 와 표에 함께 간다 — 화면과 이력이 한 문장을 본다."""
    out, _ = _run(collection="BLOCKED")

    assert 적재.calls[0]["reason"] in out.notes


def test_세_칸이_그날_상태와_같다(적재):
    """🟢 문자열을 파싱하지 않고도 **어느 쪽이 막았는지** 꺼낼 수 있어야 한다."""
    _run(inbound="BLOCKED", receivable="ISSUED", collection="FAILED")

    보낸것 = 적재.calls[0]
    assert 보낸것["inbound_status"] == "BLOCKED"
    assert 보낸것["receivable_status"] == "ISSUED"
    assert 보낸것["collection_status"] == "FAILED"


def test_업무_키가_하루_단위다(적재):
    """★ 관문은 하루를 통째로 돌려세운다 — 품목이 없다."""
    _run(inbound="BLOCKED")

    assert 적재.calls[0]["request_id"] == ledger_gap_request_id(AS_OF)
    assert "LEDGER-GAP" in 적재.calls[0]["request_id"]


def test_업무_키가_품목_키와_안_겹친다():
    """🔴 겹치면 그날 그 품목의 판단 행과 게이트 행이 같은 키를 갖는다.

    ★ **자기 생존.** 계약 품목을 하나도 못 찾으면 먼저 실패한다 — 0건을 세고
      초록이 되는 검사를 만들지 않는다.
    """
    assert ITEM_CODES, "계약 품목을 하나도 못 찾았다 — 스캐너가 죽었다"

    게이트키 = ledger_gap_request_id(AS_OF)
    겹친것 = [item for item in sorted(ITEM_CODES) if daily_request_id(AS_OF, item) == 게이트키]
    assert 겹친것 == [], f"품목 키와 겹친다: {겹친것}"


# ══════════════════════════════════════════════════════════════════════
#  ③ 적재가 터져도 걷기는 그대로다
# ══════════════════════════════════════════════════════════════════════


def test_적재가_터져도_그날_결과가_그대로_나온다(monkeypatch):
    """🔴 **이력 때문에 운영이 멈추면 안 된다.** `try_save_run` 이름이 `try_` 인 이유다.

    ★ **자기 생존.** 터지는 대역이 실제로 불렸다는 것을 같이 확인한다.
    """
    조용한대역 = _Record()
    monkeypatch.setattr(persistence, "record_ledger_gap", 조용한대역)
    성한결과, _ = _run(inbound="BLOCKED")

    터지는대역 = _Record(boom=RuntimeError("표가 죽었다"))
    monkeypatch.setattr(persistence, "record_ledger_gap", 터지는대역)
    터진결과, procure = _run(inbound="BLOCKED")

    assert len(터지는대역.calls) == 1, "터지는 대역이 안 불렸다 — 아무것도 안 쟀다"
    assert 터진결과 == 성한결과, "적재가 터졌다고 그날 결과가 달라졌다"
    assert procure.requests == []


# ══════════════════════════════════════════════════════════════════════
#  ④ 표에 무엇이 적히나 — `record_ledger_gap` 안쪽
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def 표(monkeypatch) -> dict[str, object]:
    """`try_save_run` 을 대역으로. **넘어간 칸을 그대로 본다.**"""
    captured: dict[str, object] = {}
    monkeypatch.setattr(persistence, "history_enabled", lambda: True)
    monkeypatch.setattr(persistence, "list_runs", lambda **kw: [])
    monkeypatch.setattr(persistence, "try_save_run", lambda **kw: captured.update(kw))
    return captured


def _적재(**kwargs) -> str | None:
    보낼것 = {
        "request_id": ledger_gap_request_id(AS_OF),
        "as_of": AS_OF,
        "policy_version": "v1.3-PROVISIONAL",
        "reason": (
            "장부가 안 서서 판단을 안 돌린다"
            " (입고: BLOCKED · 채권: ISSUED · 수금: COLLECTED)"
        ),
        "inbound_status": "BLOCKED",
        "receivable_status": "ISSUED",
        "collection_status": "COLLECTED",
    }
    보낼것.update(kwargs)
    return persistence.record_ledger_gap(**보낼것)  # type: ignore[arg-type]


def test_런타임_상태가_미가동이다(표):
    """🔴 **환경이 안 섰다** — 장부 관문이 정확히 그 뜻이다.

    `READY` 로 적히면 화면이 이 행을 안으로 고르러 가고, 그러면 *"안 0개인 실행"*
    이 하나 생긴다.
    """
    _적재()

    assert 표["runtime_status"] == "RUNTIME_NOT_READY"


def test_종료_코드를_새로_만들지_않는다(표):
    """★ *"왜 못 했나"* 는 종료 코드가 아니라 `reason` 이 답할 자리다."""
    _적재()

    assert 표["end_code"] == "E4_NOT_STARTED"


def test_화면이_읽는_칸에_사유가_그대로_있다(표):
    """🔴 **매입 화면이 읽는 칸은 `response_payload.reason` 하나다** (`_no_plan_note`)."""
    사유 = "장부가 안 서서 판단을 안 돌린다 (입고: FAILED · 채권: ISSUED · 수금: COLLECTED)"
    _적재(reason=사유, inbound_status="FAILED")

    assert 표["response_payload"]["reason"] == 사유


def test_어느_쪽이_막았는지_파싱_없이_꺼낸다(표):
    """🟢 문구가 바뀌어도 이 세 칸은 안 흔들린다."""
    _적재(inbound_status="BLOCKED", receivable_status="FAILED", collection_status="NOTHING_DUE")

    assert 표["response_payload"]["ledger_gate"] == {
        "inbound": "BLOCKED",
        "receivable": "FAILED",
        "collection": "NOTHING_DUE",
    }


def test_사이클이_매입과_같다(표):
    """🔴 화면이 `cycle = 'PROCUREMENT'` 축으로 그날을 훑는다 — 새 사이클은 안 닿는다."""
    _적재()

    assert 표["cycle"] == "PROCUREMENT"


def test_품목을_지어내지_않는다(표):
    """★ 관문은 하루 단위다. 품목 칸을 채우면 *"배추 때문에 막혔다"* 가 생긴다."""
    _적재()

    assert 표.get("item") is None


def test_안을_지어내지_않는다(표):
    """🔴 **이것이 화면 안전의 조건이다.**

    매입 화면은 `response_payload.scenarios` 가 있는 행만 안으로 고른다
    (`app/api/purchase/query.py` `_pick`). 지어낸 안을 한 줄이라도 실으면 그 순간
    품목 미상 행이 안 목록에 뜬다.
    """
    _적재()

    assert 표["plan"] == []
    assert "scenarios" not in 표["response_payload"]


def test_표에_실린_실행_축이_받은_값_그대로다(표):
    """★ 적재 함수가 받은 축을 **손대지 않고** 표로 넘긴다.

    ⚠️ 이 검사는 *"넘기는가"* 만 잰다. **누가 값을 주는가**는 아래 ⑥ 이 잰다 —
      두 사실이 다르고, 넘기는 쪽만 초록인 채 축이 비어 있을 수 있다.
    """
    _적재(sim_run_id="SIM-TEST-0001")

    assert 표["sim_run_id"] == "SIM-TEST-0001"


# ══════════════════════════════════════════════════════════════════════
#  ⑤ 같은 날을 두 번 걸어도 행은 한 벌이다
# ══════════════════════════════════════════════════════════════════════


def test_같은_날_게이트_행이_이미_있으면_안_넣는다(monkeypatch):
    """🔴 **인덱스가 두 번째를 못 막는다** (실측 · `REQ-DAILY-20260106-배추` 가 7행).

    ★ **자기 생존.** 행이 없을 때는 실제로 넣는 것을 같은 검사가 확인한다.
    """
    적힌것: list[dict[str, object]] = []
    있는행: list[object] = []
    monkeypatch.setattr(persistence, "history_enabled", lambda: True)
    monkeypatch.setattr(persistence, "list_runs", lambda **kw: list(있는행))
    monkeypatch.setattr(persistence, "try_save_run", lambda **kw: 적힌것.append(kw))

    _적재()
    assert len(적힌것) == 1, "행이 없는데도 안 넣었다"

    있는행.append({"run_id": "이미 있다"})
    _적재()
    assert len(적힌것) == 1, "같은 날 게이트 행이 두 벌 쌓였다"


def test_중복_확인이_터져도_행을_남긴다(monkeypatch):
    """★ **행이 두 벌인 것보다 행이 아예 없는 것이 나쁘다.**

    행이 없으면 화면이 다시 *"사유를 남긴 실행이 없습니다"* 로 돌아가고, 그것이 이
    판이 고치려는 바로 그 문구다.
    """
    적힌것: list[dict[str, object]] = []

    def _터진다(**kwargs):
        raise RuntimeError("표를 못 읽었다")

    monkeypatch.setattr(persistence, "history_enabled", lambda: True)
    monkeypatch.setattr(persistence, "list_runs", _터진다)
    monkeypatch.setattr(persistence, "try_save_run", lambda **kw: 적힌것.append(kw))

    _적재()

    assert len(적힌것) == 1, "중복 확인이 터졌다고 행을 통째로 잃었다"


# ══════════════════════════════════════════════════════════════════════
#  ⑥ 실행 축 — 게이트 행이 같은 날 판단 행과 **같은 값**을 싣는다
# ══════════════════════════════════════════════════════════════════════
#
# 🔴 **인자만 있고 값을 안 주면 늘 NULL 이다.** `record_ledger_gap` 이 `sim_run_id` 를
#    받을 줄 알아도 진입점이 안 주면 게이트 행의 축이 비고, 그러면 **모두가 쓰는 축으로
#    훑을 때 막힌 날이 도로 안 보인다** — 이 판이 존재하는 이유가 그것이다.
#
# ⚠️ **`None` 이 아니다** 로 재지 않는다. 값이 갈려도 초록이 되기 때문이다. 같은 날
#    판단 행이 실제로 싣는 값을 **진짜 진입점을 돌려서** 꺼내 놓고 그것과 비교한다.


def _판단_행의_축(monkeypatch) -> object:
    """같은 날 **판단 행**이 싣는 축을 진짜 진입점(`run_procurement`)에서 꺼낸다.

    ★ 상수를 여기 다시 적으면 비교가 *"내가 적은 값과 같은가"* 가 되어 아무것도 안
      잰다. 개장 관문을 막아 부서를 한 번도 안 부르고 적재 인자만 받아낸다.
    """
    from app.master.day_gate import DayGate
    from app.master.schemas import ProcurementRunRequest
    from app.master.service import run_procurement

    막힘 = DayGate(
        as_of=AS_OF,
        gate="BLOCKED",
        result="NOT_OPENED",
        reason="재무가 안 열렸다",
        next_action="RETRY_OPEN_DAY",
    )
    받은것: dict[str, object] = {}
    monkeypatch.setattr("app.master.service.check_day_gate", lambda as_of, **kw: 막힘)
    monkeypatch.setattr(
        "app.master.service.persistence.record",
        lambda *a, **k: 받은것.update(k) or "RUN-1",
    )

    run_procurement(
        ProcurementRunRequest(
            as_of=AS_OF, policy_version="v1.3", item="배추", request_id="REQ-AXIS-1"
        ),
        verifier=None,
    )
    return 받은것.get("sim_run_id")


def test_게이트_행이_같은_날_판단_행과_같은_축을_싣는다(적재, monkeypatch):
    """🔴 **두 행이 같은 축에 앉아야 한 실행으로 묶인다.**

    ★ **자기 생존.** 판단 행의 축이 비면 먼저 실패한다 — 둘 다 `None` 이라 통과하는
      비교를 만들지 않는다.
    """
    판단축 = _판단_행의_축(monkeypatch)
    assert isinstance(판단축, str) and 판단축, f"판단 행의 축을 못 꺼냈다: {판단축!r}"

    _run(inbound="BLOCKED")

    assert len(적재.calls) == 1, "관문이 막았는데 행을 안 남겼다"

    # ★ `[...]` 가 아니라 `.get` 이다 — 아예 안 넘긴 날에 `KeyError` 대신 *"안 실렸다"*
    #   가 그대로 보여야 한다. 그것이 이 검사가 잡으려는 바로 그 모양이다.
    게이트축 = 적재.calls[0].get("sim_run_id")
    assert 게이트축 == 판단축, f"게이트 행의 축이 판단 행과 다르다: {게이트축!r} != {판단축!r}"


def test_실행_축을_새로_짓지_않는다(적재):
    """🔴 **값의 주인은 하나다** (`bootstrap.py` 가 못 박아 둔 것).

    스케줄러가 문자열을 다시 적거나 자기 상수를 만들면 실행이 둘이 되는 날 그 자리만
    안 바뀐다.
    """
    from app.master.ledger_repository import BURN_IN_SIM_RUN_ID

    _run(collection="BLOCKED")

    assert 적재.calls[0].get("sim_run_id") == BURN_IN_SIM_RUN_ID


def test_이력을_끄면_읽지도_않는다(monkeypatch):
    """🔴 **중복 확인이 표를 찾아간다.** 이력을 안 남기는 판(pytest)에서 이 확인이
    나가면 팀 공용 DB 로 SELECT 가 나간다.

    ★ **자기 생존.** 켠 판에서는 실제로 읽고 쓰는 것을 같은 검사가 확인한다.
    """
    읽음: list[object] = []
    적힌것: list[object] = []
    켜짐 = [False]
    monkeypatch.setattr(persistence, "history_enabled", lambda: 켜짐[0])
    monkeypatch.setattr(persistence, "list_runs", lambda **kw: 읽음.append(kw) or [])
    monkeypatch.setattr(persistence, "try_save_run", lambda **kw: 적힌것.append(kw))

    assert _적재() is None
    assert 읽음 == [], "이력이 꺼졌는데 표를 읽으러 갔다"
    assert 적힌것 == []

    켜짐[0] = True
    _적재()
    assert len(읽음) == 1, "켠 판에서도 안 읽는다 — 아무것도 안 쟀다"
    assert len(적힌것) == 1
