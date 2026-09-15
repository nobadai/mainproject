"""도착 후보 선택 규칙 검사 (3-B4-B).

★ **DB 를 부르지 않는다 — 부를 수 없어야 한다.** 이 파일이 재는 것은 값이 어디에
  들어갔는지가 아니라 **물류가 소유한 분류 규율 다섯**이다.

  ```text
  날짜 규칙이 <= 인가            건너뛴 날의 도착분이 갇히지 않는다
  날짜를 지어내지 않는가          eta 가 없으면 as_of 로 채우지 않는다
  참조를 지어내지 않는가          purchase_id 를 조립하지 않는다
  막힌 행이 보이는가             조용히 버리지도, due 로 올리지도 않는다
  순서가 입력에 안 흔들리는가      같은 사실이면 같은 순서다
  ```
"""

from __future__ import annotations

import ast
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.logistics import arrival
from app.logistics.arrival import ArrivalSelection, select_due_inbound
from app.logistics.schemas import InTransitItem

AS_OF = date(2026, 1, 7)


def _행(
    *,
    inbound_id: str | None = "INB-H1-REQ-1-1-1",
    purchase_id: str | None = "PUR-REQ-1-D1-S1",
    item: str = "배추",
    qty_kg: str = "300",
    eta: date | None = AS_OF,
) -> InTransitItem:
    return InTransitItem(
        inbound_id=inbound_id,
        purchase_id=purchase_id,
        item=item,
        quantity_kg=Decimal(qty_kg),
        expected_arrival_date=eta,
    )


def _코드만(source: str) -> str:
    """docstring 과 `#` 주석을 걷어낸 **실제로 실행되는 코드**.

    ⚠️ 원문을 그대로 뒤지면 *"DB 를 부르지 않는다"* 고 **설명하는 문장**이 호출로
       잡힌다. 설명과 실행문은 다른 것이고, 잠가야 할 것은 후자다.

    ★ **문자열 리터럴은 남긴다.** `f"PUR-{…}"` 로 ID 를 조립하는 것이 바로 잡으려는
      위반이라, 문자열까지 걷어내면 검사가 아무것도 안 잰다.
    """
    tree = ast.parse(source)
    코드 = source
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            docstring = ast.get_docstring(node, clean=False)
            if docstring:
                코드 = 코드.replace(docstring, "", 1)
    return chr(10).join(line.split("#", 1)[0] for line in 코드.splitlines())


def _원문() -> str:
    return Path(arrival.__file__).read_text(encoding="utf-8")


# ── 1~3. 날짜 규칙은 `<=` 다 ────────────────────────────────────────────


def test_1_당일_도착은_due_다():
    """★ `eta == as_of` — 가장 흔한 정상 경로."""
    선택 = select_due_inbound([_행(eta=AS_OF)], as_of=AS_OF)

    assert len(선택.due) == 1
    assert 선택.due[0].inbound_id == "INB-H1-REQ-1-1-1"
    assert 선택.due[0].purchase_id == "PUR-REQ-1-D1-S1"
    assert 선택.due[0].expected_arrival_date == AS_OF
    assert 선택.blocked == () and 선택.unresolved == () and 선택.not_due == ()


def test_2_지난_도착일도_due_이고_연체로_잡힌다():
    """🔴 **`==` 로 잡으면 여기가 영영 안 걸린다.**

    fixture 달력에 2026-01-03 · 01-04 가 없다(실측). 그날 도착 예정이던 물건은
    처리 창이 지나가 버리고, `day_open` 이 `in_transit` 을 물려받으므로 **사라지지도
    않은 채** 매일 실려 온다 — B-1 은 통과하고 점유는 계속 세므로 아무도 못 잡는다.
    """
    선택 = select_due_inbound([_행(eta=AS_OF - timedelta(days=1))], as_of=AS_OF)

    assert len(선택.due) == 1
    assert 선택.due[0].overdue is True
    assert 선택.overdue_count == 1


def test_2b_당일_도착은_연체가_아니다():
    """★ `<` 와 `<=` 를 가른다 — 정시 도착을 밀린 것으로 적지 않는다."""
    선택 = select_due_inbound([_행(eta=AS_OF)], as_of=AS_OF)

    assert 선택.due[0].overdue is False
    assert 선택.overdue_count == 0


def test_3_미래_도착일은_not_due_다():
    """★ 손대지 않고 그대로 담는다."""
    미래 = _행(eta=AS_OF + timedelta(days=1))

    선택 = select_due_inbound([미래], as_of=AS_OF)

    assert 선택.not_due == (미래,)
    assert 선택.due == () and 선택.blocked == ()
    assert 선택.overdue_count == 0


# ── 4. 도착일이 없으면 unresolved ───────────────────────────────────────


def test_4_도착일이_없으면_unresolved_다():
    """🔴 `as_of` 로 채우지 않는다 — 지어낸 하루가 로트의 `received_at` 이 된다."""
    선택 = select_due_inbound([_행(eta=None)], as_of=AS_OF)

    assert len(선택.unresolved) == 1
    assert 선택.unresolved[0].reason == "ARRIVAL_DATE_UNRESOLVED"
    assert 선택.due == () and 선택.blocked == () and 선택.not_due == ()


def test_4b_도착일이_없으면_참조가_있어도_unresolved_다():
    """★ 도착 자격을 따질 **날 자체가 없다** — `purchase_id` 유무와 무관하다."""
    있음 = select_due_inbound([_행(eta=None, purchase_id="PUR-REQ-1-D1-S1")], as_of=AS_OF)
    없음 = select_due_inbound([_행(eta=None, purchase_id=None)], as_of=AS_OF)

    assert len(있음.unresolved) == 1
    assert len(없음.unresolved) == 1
    assert 있음.blocked == () and 없음.blocked == ()


def test_4c_unresolved_는_한_행만_막고_전체를_뒤집지_않는다():
    """★ 이 함수는 **분류만** 한다 — 한 행 때문에 나머지 판단을 멈추지 않는다.

    🔴 스냅샷 전체를 `RUNTIME_NOT_READY` 로 만드는 것은 이 자리의 일이 아니다.
       도착과 무관한 판단(`cap_by_date` · 시나리오)까지 함께 죽는다.
    """
    선택 = select_due_inbound([_행(eta=None), _행(eta=AS_OF)], as_of=AS_OF)

    assert len(선택.unresolved) == 1
    assert len(선택.due) == 1, "옆 행은 정상적으로 분류돼야 한다"
    assert 선택.source_status == "CONFIRMED"


# ── 5~7. 막힌 행은 보이게 남는다 ────────────────────────────────────────


def test_5_매입_참조가_없으면_blocked_다():
    """⚠️ **지금 실데이터가 오는 자리다.** 마스터가 참조를 아직 안 넘긴다."""
    선택 = select_due_inbound([_행(purchase_id=None)], as_of=AS_OF)

    assert len(선택.blocked) == 1
    assert 선택.blocked[0].reasons == ("ARRIVAL_PURCHASE_REFERENCE_MISSING",)
    assert 선택.due == (), "due 로 올리지 않는다"
    assert 선택.not_due == (), "조용히 버리지도 않는다"


def test_6_inbound_id_가_없으면_blocked_다():
    선택 = select_due_inbound([_행(inbound_id=None)], as_of=AS_OF)

    assert len(선택.blocked) == 1
    assert 선택.blocked[0].reasons == ("ARRIVAL_INBOUND_ID_MISSING",)
    assert 선택.due == ()


def test_7_사유가_둘이면_둘_다_남는다():
    """🔴 **하나를 다른 하나 뒤에 숨기지 않는다.** 하나를 고친 뒤 또 막히면, 두 번째
    문제는 첫 번째를 고치기 전에는 존재하지도 않았던 것처럼 보인다.
    """
    선택 = select_due_inbound([_행(inbound_id=None, purchase_id=None)], as_of=AS_OF)

    assert len(선택.blocked) == 1
    assert set(선택.blocked[0].reasons) == {
        "ARRIVAL_INBOUND_ID_MISSING",
        "ARRIVAL_PURCHASE_REFERENCE_MISSING",
    }


@pytest.mark.parametrize("빈값", ["", None])
def test_7b_빈_문자열도_없는_것으로_본다(빈값: str | None):
    """★ 있는 척하는 값을 통과시키면 뒤 단계가 그 값으로 조회를 나간다."""
    선택 = select_due_inbound([_행(purchase_id=빈값)], as_of=AS_OF)

    assert 선택.blocked[0].reasons == ("ARRIVAL_PURCHASE_REFERENCE_MISSING",)


def test_7c_막힌_행도_연체가_잡힌다():
    """★ 사유와 연체는 다른 축이다 — 막혔다고 밀린 사실이 없어지지 않는다."""
    선택 = select_due_inbound([_행(purchase_id=None, eta=AS_OF - timedelta(days=2))], as_of=AS_OF)

    assert 선택.blocked[0].overdue is True
    assert 선택.overdue_count == 1


# ── 8~9. None 과 [] 는 다른 사실이다 ────────────────────────────────────


def test_8_None_과_빈목록은_다른_결과다():
    """🔴 뭉치면 *"도착할 게 없다"* 와 *"뭐가 도착할지 모른다"* 가 같은 값이 된다."""
    모름 = select_due_inbound(None, as_of=AS_OF)
    영건 = select_due_inbound([], as_of=AS_OF)

    assert 모름.source_status == "UNRESOLVED"
    assert 영건.source_status == "CONFIRMED_ZERO"
    assert 모름 != 영건, "네 목록이 둘 다 비어도 같은 사실이 아니다"


def test_9_빈목록은_알려진_빈_선택이다():
    영건 = select_due_inbound([], as_of=AS_OF)

    assert 영건 == ArrivalSelection(
        source_status="CONFIRMED_ZERO",
        due=(),
        blocked=(),
        unresolved=(),
        not_due=(),
        overdue_count=0,
    )


def test_9b_None_도_네_목록이_비어_있다():
    """★ *"모른다"* 를 *"있다"* 로도 *"없다"* 로도 바꾸지 않는다."""
    모름 = select_due_inbound(None, as_of=AS_OF)

    assert (모름.due, 모름.blocked, 모름.unresolved, 모름.not_due) == ((), (), (), ())
    assert 모름.overdue_count == 0


def test_9c_행이_다_막혀도_목록_상태는_CONFIRMED_다():
    """⚠️ `source_status` 는 **목록의 상태**이지 행의 판정이 아니다."""
    선택 = select_due_inbound([_행(purchase_id=None)], as_of=AS_OF)

    assert 선택.source_status == "CONFIRMED", "무엇이 떠 있는지는 안다"


# ── 10~11. 순서가 입력에 안 흔들린다 ────────────────────────────────────


def _여러행() -> list[InTransitItem]:
    return [
        _행(inbound_id="INB-B", eta=date(2026, 1, 5)),
        _행(inbound_id="INB-A", eta=date(2026, 1, 5)),
        _행(inbound_id="INB-C", eta=date(2026, 1, 3)),
        _행(inbound_id="INB-A", eta=date(2026, 1, 7)),
    ]


def test_10_도착일_그다음_inbound_id_순이다():
    """★ 연체가 오래된 것부터, 같은 날은 `inbound_id` 로. 둘 다 이미 있는 사실이다."""
    선택 = select_due_inbound(_여러행(), as_of=AS_OF)

    assert [(row.expected_arrival_date, row.inbound_id) for row in 선택.due] == [
        (date(2026, 1, 3), "INB-C"),
        (date(2026, 1, 5), "INB-A"),
        (date(2026, 1, 5), "INB-B"),
        (date(2026, 1, 7), "INB-A"),
    ]


def test_11_입력_순서를_뒤집어도_결과가_같다():
    """🔴 같은 사실을 다르게 담기만 해도 순서가 달라지면 결정적이지 않다."""
    바로 = select_due_inbound(_여러행(), as_of=AS_OF)
    거꾸로 = select_due_inbound(list(reversed(_여러행())), as_of=AS_OF)

    assert 바로 == 거꾸로


def test_11b_inbound_id_가_없는_행도_순서가_흔들리지_않는다():
    """★ `(eta, inbound_id)` 는 `inbound_id` 가 없으면 전순서가 아니다 — 그 동률을
    입력 순서가 정하게 두면 같은 입력을 다르게 담을 때 결과가 갈린다.
    """
    행들 = [
        _행(inbound_id=None, purchase_id=None, item="배추", eta=AS_OF),
        _행(inbound_id=None, purchase_id=None, item="무", eta=AS_OF),
    ]

    바로 = select_due_inbound(행들, as_of=AS_OF)
    거꾸로 = select_due_inbound(list(reversed(행들)), as_of=AS_OF)

    assert len(바로.blocked) == 2
    assert 바로 == 거꾸로


def test_11c_네_갈래가_섞여도_각자_정렬된다():
    행들 = [
        _행(inbound_id="INB-FUT", eta=AS_OF + timedelta(days=3)),
        _행(inbound_id="INB-DUE", eta=AS_OF),
        _행(inbound_id="INB-BLK", purchase_id=None, eta=AS_OF - timedelta(days=1)),
        _행(inbound_id="INB-UNK", eta=None),
        _행(inbound_id="INB-FU2", eta=AS_OF + timedelta(days=1)),
    ]

    선택 = select_due_inbound(행들, as_of=AS_OF)

    assert [row.inbound_id for row in 선택.due] == ["INB-DUE"]
    assert [row.item.inbound_id for row in 선택.blocked] == ["INB-BLK"]
    assert [row.item.inbound_id for row in 선택.unresolved] == ["INB-UNK"]
    assert [row.inbound_id for row in 선택.not_due] == ["INB-FU2", "INB-FUT"]
    assert 선택 == select_due_inbound(list(reversed(행들)), as_of=AS_OF)


# ── 12~14. 지어내지 않고, DB 를 안 부른다 ───────────────────────────────


def test_12_날짜를_지어내지_않는다():
    """🔴 `eta` 가 없는 행은 **없는 채로** 남는다 — `as_of` 가 새어 들어가지 않는다."""
    날짜없음 = _행(eta=None)

    선택 = select_due_inbound([날짜없음], as_of=AS_OF)

    담긴행 = 선택.unresolved[0].item
    assert 담긴행.expected_arrival_date is None
    assert 담긴행 == 날짜없음, "원본을 고치지 않는다"


def test_12b_시계를_읽지_않는다():
    """★ 기준일은 **인자로 받은 `as_of` 뿐이다.** 오늘 날짜를 몰래 읽으면 같은 입력이
    실행하는 날마다 다른 답을 낸다.
    """
    코드 = _코드만(_원문())

    for 금지 in ("today(", "now(", "utcnow"):
        assert 금지 not in 코드, f"{금지} — 시계를 읽고 있다"


def test_13_매입_참조를_조립하지_않는다():
    """🔴 `purchase_id` 의 주인은 마스터다. 같은 규칙이 두 곳에 있으면 마스터가 형식을
    바꾸는 날 조용히 어긋난다.
    """
    코드 = _코드만(_원문())

    assert "PUR-" not in 코드, "매입 ID 문자열을 조립하고 있다"
    assert "purchase_id_for" not in 코드, "마스터 ID 함수를 부르고 있다"
    assert "approval_id" not in 코드, "승인 id 를 뜯어 참조를 만들고 있다"


def test_13b_없는_참조는_없는_채로_남는다():
    """★ 행동으로도 잰다 — 원문 검사만으로는 우회를 다 막지 못한다."""
    선택 = select_due_inbound([_행(purchase_id=None)], as_of=AS_OF)

    assert 선택.blocked[0].item.purchase_id is None


def test_14_DB_를_부르지_않는다():
    """🔴 이 모듈은 **이미 읽어 둔 사실을 분류만** 한다."""
    코드 = _코드만(_원문())

    for 금지 in (
        "get_connection",
        "psycopg",
        "cursor",
        "execute",
        "INSERT",
        "UPDATE",
        "DELETE",
        "SELECT",
        "commit",
        "rollback",
        "fetch_all",
    ):
        assert 금지 not in 코드, f"{금지} — 순수 계산이 아니다"


def test_14b_임포트에_DB_모듈이_없다():
    """★ 문자열 검사와 별개로 **의존 자체**를 잠근다."""
    tree = ast.parse(_원문())
    모듈: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            모듈.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            모듈.add(node.module)

    assert not any("psycopg" in name or name.endswith(".db") for name in 모듈), 모듈
    assert 모듈 <= {
        "__future__",
        "json",
        "collections.abc",
        "dataclasses",
        "datetime",
        "typing",
        "app.logistics.schemas",
    }, 모듈


# ── 15~16. 판정 순서와 실데이터 모양 ────────────────────────────────────


def test_15_미래_행은_참조가_없어도_not_due_다():
    """🔴 **날짜를 먼저 본다.**

    아직 오지도 않은 물건을 *"막혔다"* 고 적으면, 협의가 진행 중인 정상 상태가 매일
    장애로 보고된다. 참조가 없다는 사실이 업무를 막는 순간은 **도착 처리 대상이 되는
    날부터**다.
    """
    미래_참조없음 = _행(eta=date(2026, 1, 10), purchase_id=None)

    선택 = select_due_inbound([미래_참조없음], as_of=AS_OF)

    assert 선택.not_due == (미래_참조없음,)
    assert 선택.blocked == (), "아직 안 온 물건을 막힌 것으로 적지 않는다"


def test_16_현재_실데이터_모양은_참조없음으로_막힌다():
    """★ **2026-09-05 실 DB 실측 모양을 그대로 고정한다.**

    ```text
    inbound_id  INB-H1-THRU-20260105-BAECHU-1-1
    eta         2026-01-07      ← as_of 와 같은 날 (오늘 도착 예정)
    purchase_id (키 없음 → None)
    ```

    ⚠️ 이것은 **정상적으로 예상된 상태**다 — 마스터 전이 규약에 `purchase_ids` 를
       더하는 것이 아직 협의 중이라서다. 물류가 값을 지어내 풀 일이 아니고,
       이 검사는 그 상태가 **보이게 남는지**를 잰다.
    """
    실데이터 = InTransitItem.model_validate(
        {
            "inbound_id": "INB-H1-THRU-20260105-BAECHU-1-1",
            "item": "배추",
            "quantity_kg": "3587.0",
            "expected_arrival_date": "2026-01-07",
        }
    )
    assert 실데이터.purchase_id is None, "옛 fixture 행에는 이 키가 없다"

    선택 = select_due_inbound([실데이터], as_of=date(2026, 1, 7))

    assert len(선택.blocked) == 1
    assert 선택.blocked[0].reasons == ("ARRIVAL_PURCHASE_REFERENCE_MISSING",)
    assert 선택.blocked[0].expected_arrival_date == date(2026, 1, 7)
    assert 선택.blocked[0].overdue is False
    assert 선택.due == ()
    assert 선택.source_status == "CONFIRMED"


def test_16b_참조가_붙으면_같은_행이_due_가_된다():
    """★ 막힌 이유가 **그 하나뿐**임을 보인다 — 마스터가 참조를 넘기면 바로 풀린다."""
    참조붙음 = _행(
        inbound_id="INB-H1-THRU-20260105-BAECHU-1-1",
        purchase_id="PUR-THRU-20260105-BAECHU-D1-S1",
        qty_kg="3587.0",
        eta=date(2026, 1, 7),
    )

    선택 = select_due_inbound([참조붙음], as_of=date(2026, 1, 7))

    assert len(선택.due) == 1
    assert 선택.due[0].purchase_id == "PUR-THRU-20260105-BAECHU-D1-S1"
    assert 선택.blocked == ()


# ── 아직 답하지 않는 질문 ───────────────────────────────────────────────


def test_영수_조회를_하지_않는다():
    """⚠️ 이 함수는 *"이 건이 이미 처리됐나"* 에 답하지 않는다 — 3-B4-C 다.

    🔴 지금 `due` 는 *"물류가 아는 사실만으로는 자격이 있다"* 는 뜻이지
       *"처리해도 된다"* 가 아니다. 그 구분이 흐려지면 `<=` 규칙이 같은 건을 날마다
       다시 처리하게 된다.
    """
    코드 = _코드만(_원문())

    assert "inbound_receipt" not in 코드
    assert "receipt" not in 코드.lower()


def test_arrived_at_을_정하지_않는다():
    """🔴 `expected_arrival_date` 는 **예정일**이고 Receipt 의 `arrived_at` 은 도착
    사실이다. 둘을 같게 볼지는 Receipt 단계의 결정이라 여기서 고르지 않는다.
    """
    코드 = _코드만(_원문())

    assert "arrived_at" not in 코드
