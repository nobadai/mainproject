"""매입 탭 — 값을 읽어오는 곳.

소유: **매입 파트 (우리)**.

★ **DB 가 안 붙어도 화면은 떠야 합니다.** 못 읽으면 예시값으로 떨어지고
  ``Source.filled = False`` 가 되어 「예시값」 딱지가 붙습니다. 화면이 통째로
  죽는 것보다, 예시라고 적힌 화면이 뜨는 편이 낫습니다 (ML 이 `forecast` 에서
  잡은 방식과 같습니다).

★ **여기서 에이전트를 돌리지 않습니다.** 저장된 실행(`master_agent_runs`)과
  확정 매입 원장(`purchases`)을 **읽기만** 합니다. 화면을 열 때마다 LLM 이
  돌면 안 됩니다 (CLAUDE.md 규칙 2 — read-only).

🔴 **DB 헬퍼를 ``app.finance.db`` 에서 가져오는 이유.**
  ``app.purchase_agent.db`` 에는 ``get_db_schema`` 가 **없습니다.** 일부러 뺐고
  (그 파일 머리말 참조) 이유는 *"``.env`` 가 어느 시세 테이블을 읽을지 정하면
  안 된다"* 입니다. 그 이유는 **에이전트 경로**의 것이고, 여기는 화면 층이라
  ``haetdeul`` 도메인 표를 읽습니다 — 스키마를 ``.env`` 가 정하는 것이 맞습니다.
  마스터 ``ledger_repository.py`` 가 같은 이유로 같은 선택을 했습니다.
  ⚠️ 쓰기 헬퍼(``execute_returning_one``)는 **가져오지 않습니다.**

★ **look-ahead 를 화면에도 적용합니다.** 확정 매입은 ``purchase_date <= as_of``
  만 봅니다. 그날 화면에 다음 주 매입이 보이면 «그날 알 수 있었던 것» 이
  아니게 됩니다 (규칙 1 의 정신).
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from app.api.primitives import Column, Note, Source, Stat, Table
from app.api.purchase.schema import Plan, PurchaseTab, Reason
from app.contracts.core import ITEMS

log = logging.getLogger(__name__)

_LEG_COLS = [
    Column(key="leg", label="회차"),
    Column(key="buy", label="사는 날", mono=True),
    Column(key="qty", label="수량", align="right", mono=True),
    Column(key="arrive", label="도착", mono=True),
]
_PAY_COLS = [
    Column(key="leg", label="회차"),
    Column(key="buy", label="사는 날", mono=True),
    Column(key="pay", label="내는 날", mono=True),
    Column(key="amount", label="금액", align="right", mono=True),
]
_COMMITTED_COLS = [
    Column(key="approval", label="승인", mono=True),
    Column(key="item", label="품목"),
    Column(key="buy", label="사는 날", mono=True),
    Column(key="arrive", label="도착", mono=True),
    Column(key="grade", label="등급"),
    Column(key="qty", label="수량", align="right", mono=True),
    Column(key="unit", label="단가", align="right", mono=True),
    Column(key="amount", label="금액", align="right", mono=True),
    Column(key="pay", label="지급", mono=True),
]

#: 축 어휘를 사람 말로. **내부 단어를 화면에 내보내지 않는다.**
_KNOB = {
    "quantity": "수량으로 조절",
    "timing": "사는 시점으로 조절",
    "mix": "등급 구성으로 조절",
}

_EMPTY_COMMITTED = "아직 확정된 매입이 없습니다 — 위에서 안을 고르면 여기에 생깁니다"


# ══════════════════════════════════════════════════════════════════════════
#  DB 읽기
# ══════════════════════════════════════════════════════════════════════════

def _read(as_of: date) -> dict[str, Any]:
    """저장된 실행과 확정 매입을 읽는다. **SELECT 뿐이다.**

    🔴 **축(`sim_run_id`)으로 여기서 거르지 않는다.** 칸을 읽어 오기만 하고 고르는 것은
    ``_pick`` · ``_committed`` 가 한다. 이유 둘::

        ① 화면이 «전체 몇 건 중 이 걷기 몇 건» 을 말하려면 전체를 봐야 한다.
           WHERE 로 걸러 오면 뺀 수를 셀 수 없고, 그러면 조용히 없애는 것이 된다
        ② 검사가 이 함수를 대신 세워 상황을 주입한다. WHERE 에 두면 그 주입이
           필터를 건너뛰어 **축이 도는지를 못 잰다** (규칙 8)
    """
    from psycopg import sql

    from app.finance.db import fetch_all, get_db_schema

    schema = get_db_schema()

    def table(name: str) -> sql.Composable:
        return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(name))

    runs = fetch_all(
        sql.SQL(
            "SELECT request_id, item, end_code, runtime_status, created_at, sim_run_id,"
            " response_payload AS payload"
            " FROM {} WHERE as_of = %(as_of)s AND cycle = 'PROCUREMENT'"
            " ORDER BY created_at DESC"
        ).format(table("master_agent_runs")),
        {"as_of": as_of},
    )
    #  🔴 레슨 ③ — purchases 와 purchase_items 를 조인하면 total_amount_krw 가
    #     줄마다 반복된다 (PUR-KIMCHI-015 는 5줄이고 다섯 다 3,370,487). 줄 금액은
    #     line_amount_krw 로 읽는다. 여기서는 아예 total 을 안 가져온다.
    buys = fetch_all(
        sql.SQL(
            "SELECT p.purchase_id, p.purchase_date, p.payment_due_date,"
            " p.settlement_status, p.sim_run_id, i.item_id, i.grade, i.quantity_kg,"
            " i.unit_price_krw_per_kg, i.line_amount_krw"
            " FROM {} p JOIN {} i USING (purchase_id)"
            " WHERE p.purchase_type = 'MASTER_APPROVAL' AND p.purchase_date <= %(as_of)s"
            " ORDER BY p.purchase_date, i.purchase_item_id"
        ).format(table("purchases"), table("purchase_items")),
        {"as_of": as_of},
    )
    decisions = fetch_all(
        sql.SQL("SELECT request_id, decision, scenario_label FROM {}").format(
            table("master_decisions")
        ),
    )
    items = fetch_all(sql.SQL("SELECT item_id, item_name FROM {}").format(table("items")))
    #  확정 매입의 도착일은 원장에 없다. 그날 실행의 시나리오에서 **금액으로**
    #  맞춰 온다 — purchase_id 문자열을 쪼개면 이름 규칙에 묶인다.
    dates = sorted({row["purchase_date"] for row in buys})
    arrivals = fetch_all(
        sql.SQL(
            "SELECT as_of, item, sim_run_id, response_payload->'scenarios' AS scenarios"
            " FROM {} WHERE as_of = ANY(%(dates)s) AND cycle = 'PROCUREMENT'"
        ).format(table("master_agent_runs")),
        {"dates": dates},
    ) if dates else []
    return {
        "runs": runs,
        "buys": buys,
        "decisions": decisions,
        "items": {row["item_id"]: row["item_name"] for row in items},
        "arrivals": arrivals,
    }


# ══════════════════════════════════════════════════════════════════════════
#  실행 고르기 — 🔴 레슨 ①
# ══════════════════════════════════════════════════════════════════════════

def _pick(
    runs: list[dict[str, Any]], sim_run_id: str | None = None
) -> tuple[list[dict[str, Any]], str]:
    """품목마다 **하나씩** 고르고, 몇 개 중 무엇을 골랐는지 같이 돌려준다.

    🔴 **같은 날 실행이 여럿이다.** `2026-01-06` 배추는 아홉이고 그중 넷이
    승인이다. 아무 말 없이 하나를 고르면, 다음 사람이 다른 행을 보고 «값이
    다르다» 고 한다. 그래서 규칙을 코드에 박고 화면에 적는다.

    ::

        ① runtime_status = 'READY'   — 미가동(E4)은 안을 못 낸 날이다
        ② scenarios 가 비지 않은 것
        ③ 🔴 item 이 계약 품목일 것 (contracts.core.ITEMS)
        ④ 🔴 sim_run_id 가 그 축일 것 — **안 주면 안 거른다**
        ⑤ 품목별 created_at 최신 하나

    🔴 **④ 가 ⑤ 앞이어야 한다.** 뒤로 가면 «최신 하나» 가 먼저 다른 걷기의 행을 집고
    그 뒤에 축으로 떨어뜨려, 같은 축에 있던 조금 오래된 행이 **같이 사라진다.**

    ⚠️ 지금 DB 에서는 축 있는 행이 언제나 더 새것이라(축이 `2026-09-08` 에 생겼다)
    순서를 바꿔도 값이 안 갈린다 — 그래서 **검사가 상황을 주입한다** (규칙 8).

    🔴 **축 이름을 쪼개 뜻을 읽지 않는다.** `SIM-WALK-202601-BASE` 의 `BASE` 는 사람이
    목록에서 고를 때 쓰는 꼬리표이고, 뜻은 `sim_runs` 행이 답한다 (마스터 통보
    2026-09-10). 여기서는 **같은지만** 본다.

    🔴 **③ 이 없으면 화면에 계약 밖 품목이 뜬다.** 저장된 실행에 피마늘 행이
    **194건** 남아 있다 (2026-09-09 실측 · 종전 주석의 143건은 그 뒤 늘었다)
    — `#216` 으로 계약에서 뺐지만 **기록은 일부러 안 고쳤다**
    (`e63f990` *"고쳐 쓰면 기록이 거짓이 된다"*). 기록을 고칠 자리가 아니라
    **보일 때 거를 자리**다. 계약이 그렇게 적어 두었다::

        contracts/core.py  ITEMS 각주
        제안 축   "사자고 제안한 품목"   ITEMS 로 거른다
        재고 축   "창고에 있는 품목"     자유 문자열 — 좁히지 않는다

    이 화면은 **제안 축**이다.

    ⚠️ 거른 것을 조용히 없애지 않는다 — 몇 건을 왜 뺐는지 돌려주는 글에 적는다.
    """
    ready = [r for r in runs if r["runtime_status"] == "READY"]
    with_plans = [r for r in ready if (r["payload"] or {}).get("scenarios")]
    ours = [r for r in with_plans if r["item"] in ITEMS]
    dropped = sorted({str(r["item"]) for r in with_plans if r["item"] not in ITEMS})

    #  ④ 축. `is None` 이라야 한다 — 빈 문자열은 «안 줬다» 가 아니라 **잘못 준 것**이고,
    #     그것을 «전부» 로 읽으면 오타가 조용히 전체 조회가 된다.
    mine = ours if sim_run_id is None else [r for r in ours if r["sim_run_id"] == sim_run_id]
    off_axis = len(ours) - len(mine)

    picked: dict[str, dict[str, Any]] = {}
    for run in mine:  # 이미 created_at DESC 라 처음 만난 것이 최신이다
        picked.setdefault(str(run["item"]), run)
    chosen = list(picked.values())

    aside = ""
    if dropped:
        names = " · ".join(x if x != "None" else "품목 미상" for x in dropped)
        aside = f". 계약 밖 품목({names})은 뺐습니다 — 지금 사는 것은 {'·'.join(ITEMS)} 입니다"
    #  🔴 거른 것을 조용히 없애지 않는다 — 계약 밖 품목과 같은 규율이다.
    if off_axis:
        aside += f". 다른 걷기의 실행 {off_axis}건은 뺐습니다 — 지금 보는 것은 {sim_run_id} 입니다"
    if not chosen:
        return [], f"그날 실행 {len(runs)}건 · 그중 안을 낸 계약 품목 실행 0건{aside}"
    names = " · ".join(f"{r['item']} {r['request_id']}" for r in chosen)
    return chosen, (
        f"그날 실행 {len(runs)}건 중 환경이 선 것 {len(ready)}건 · "
        f"안을 낸 것 {len(with_plans)}건 — 품목별 최신 하나를 보입니다 ({names}){aside}"
    )


def _no_plan_note(
    runs: list[dict[str, Any]], picked_text: str, sim_run_id: str | None = None
) -> Note:
    """안이 왜 없나. 🔴 레슨 ② — ``no_proposal_reason`` 이라는 칸은 **없다.**

    ``reason`` 은 한 줄 요약이라 *"어느 안이 왜 죽었나"* 를 못 말한다. 안별 컷
    사유는 ``judgment.rejected_reasons[]`` 에 있다.

    🔴 **사유도 같은 축에서만 가져온다** (2026-09-10). 안 그러면 «축으로 걸러 0건» 인데
    화면이 **다른 걷기의 컷 사유**를 붙여 *"단가가 상한을 넘어 죽었다"* 고 말한다 —
    실제로는 그 걷기에 실행이 아예 없었던 것이다. 「안 돌았다」와 「돌았는데 죽었다」는
    다른 사실이고, 섞으면 읽는 사람이 없는 원인을 고치려 든다.
    """
    if sim_run_id is not None:
        runs = [r for r in runs if r["sim_run_id"] == sim_run_id]
        if not runs:
            return Note(
                tone="warn",
                text=f"{picked_text}. 이 걷기({sim_run_id})의 실행이 그날 없습니다 —"
                " 안이 죽은 것이 아니라 돌지 않았습니다.",
            )
    for run in runs:
        payload = run["payload"] or {}
        rejected = (payload.get("judgment") or {}).get("rejected_reasons") or []
        if rejected:
            lines = " · ".join(f"**{x.get('label')}** {x.get('reason')}" for x in rejected)
            return Note(tone="warn", text=f"{picked_text}. 안별 컷 사유 — {lines}")
    for run in runs:
        reason = (run["payload"] or {}).get("reason")
        if reason:
            return Note(tone="warn", text=f"{picked_text}. {reason}")
    return Note(tone="warn", text=f"{picked_text}. 사유를 남긴 실행이 없습니다.")


# ══════════════════════════════════════════════════════════════════════════
#  시나리오 → 화면
# ══════════════════════════════════════════════════════════════════════════

def _money(value: Any) -> int | None:
    return None if value is None else round(float(value))


def _sourcing(lines: list[dict[str, Any]]) -> tuple[str, int] | None:
    """등급과 단가. **가중평균으로 접지 않는다** (판매 요청 · `§15-6`).

    ★ 등급이 여럿이면 **가장 비싼 단가**를 보인다. 컷은 줄마다 걸리므로
      (``self_check.check_max_price``), 컷 기준과 견줄 값은 최고가다.
      그 사실을 등급 글자에 그대로 적는다 — 숨기면 평균으로 읽힌다.
    """
    if not lines:
        return None
    prices = [round(float(line["grade_unit_price"])) for line in lines]
    grades = list(dict.fromkeys(str(line["grade"]) for line in lines))
    if len(lines) == 1:
        return grades[0], prices[0]
    return f"{' · '.join(grades)} ({len(lines)}등급 · 최고가 표시)", max(prices)


def _legs(scenario: dict[str, Any]) -> Table:
    rows: list[dict[str, Any]] = []
    for leg in scenario.get("split_plan") or []:
        qty = leg.get("qty_kg")
        rows.append({
            "leg": leg.get("seq"),
            "buy": leg.get("date"),
            "qty": None if qty is None else f"{float(qty):,.0f} kg",
            "arrive": leg.get("expected_arrival_date"),
        })
    return Table(columns=_LEG_COLS, rows=rows, empty_text="이 안에는 회차 계획이 없습니다")


def _payments(scenario: dict[str, Any]) -> Table:
    """지급 계획. ★ **분할 안에서만 실린다.**

    저장된 실행에서 회차 수와 완전히 맞물린다 (2026-09-08 실측)::

        split_plan 1회차   719건   payment_schedule 없음
        split_plan 2회차   228건   payment_schedule 배열     ← 228 = 228

    ⚠️ 그래서 **빈 표가 흔한 것이 정상**이다. 한 번에 사는 안은 지급이 한 건이라
    따로 계획을 만들지 않는다. 없는 것을 매입일로 메우지 않는다 (규칙 3) —
    지급일 규칙(``purchase_payment_days`` · N5)이 아직 미결이라 더 그렇다.

    ``basis`` · ``amount_max_krw`` 는 안 싣는다. 앞은 내부 어휘이고 뒤는 **재무
    STRESS 금액**이라, 지급 표에 두면 실제로 낼 돈으로 읽힌다.
    """
    rows: list[dict[str, Any]] = []
    for pay in scenario.get("payment_schedule") or []:
        amount = pay.get("amount_krw")
        rows.append({
            "leg": pay.get("seq"),
            "buy": pay.get("purchase_date"),
            "pay": pay.get("payment_date"),
            "amount": None if amount is None else f"{_money(amount):,} 원",
        })
    return Table(
        columns=_PAY_COLS,
        rows=rows,
        empty_text="이 실행에는 지급 계획이 없습니다 — 지급일 규칙이 아직 미결입니다",
    )


def _plan(item: str, scenario: dict[str, Any], decided: dict[tuple[str, str], str],
          request_id: str) -> Plan | None:
    label = str(scenario.get("label") or "")
    sourcing = _sourcing(scenario.get("sourcing_plan") or [])
    if sourcing is None:
        return None
    grade, unit_price = sourcing
    decision = decided.get((request_id, label))
    coverage = scenario.get("coverage_days")
    return Plan(
        #  🔴 품목을 이름에 넣는다. 이 탭에는 품목 축이 없는데 우리는 품목마다
        #     따로 도므로, 안 넣으면 여러 품목이 있는 날 이름이 겹친다.
        key=f"{item} · {label}",
        coverage="며칠치인지 모름" if coverage is None else f"{coverage}일치",
        knob=_KNOB.get(str(scenario.get("strategy_type")), "조절 축을 알 수 없음"),
        qty_kg=float(scenario.get("total_qty_kg") or 0),
        amount_krw=_money(scenario.get("total_amount_krw")) or 0,
        unit_price=unit_price,
        grade=grade,
        max_price=_money(scenario.get("max_price")) or 0,
        #  🔴 없으면 None 이다. max_price 로 대신 채우지 않는다 — 그 순간
        #     09-17 에 갈라질 두 값이 화면에서 다시 하나가 된다.
        cut_unit_price=_money(scenario.get("cut_unit_price")),
        legs=_legs(scenario),
        payments=_payments(scenario),
        reasons=[
            Reason(
                source=str(r.get("source") or "출처 미상"),
                text=str(r.get("claim") or ""),
                ref=r.get("ref_id"),
            )
            for r in scenario.get("rationale") or []
        ],
        risks=[str(x) for x in scenario.get("risks") or []],
        pending=decision is None,
        approved=decision == "APPROVE",
    )


# ══════════════════════════════════════════════════════════════════════════
#  확정 매입
# ══════════════════════════════════════════════════════════════════════════

def _arrival_index(
    arrivals: list[dict[str, Any]], sim_run_id: str | None = None
) -> dict[tuple[date, str, int], str]:
    """(매입일, 품목, 줄금액) → 도착일. **금액으로 맞춘다.**

    🔴 축을 주면 그 걷기의 실행에서만 맞춘다. 안 그러면 다른 걷기가 우연히 같은 금액을
    낸 날에 **엉뚱한 도착일**이 붙고, 그건 틀린 줄도 모르는 오류다.

    ⚠️ **축을 걸면 지금 맞던 줄이 공란이 된다** (2026-09-10 실측). `01-22` 까지 원장
    네 줄 중 **둘**이 축 없는 실행에서 도착일을 받아 오고 있었다::

        2026-01-05 배추   원장축 SIM-BURNIN-202512 ← 도착일 출처축 없음
        2026-01-13 배추   원장축 SIM-BURNIN-202512 ← 도착일 출처축 없음

    ★ **공란이 맞다.** 금액으로 맞추는 것은 원래 추정이고, 축이 다르면 그 추정을 받칠
    근거가 없다. 화면은 «도착일을 못 맞춘 줄 N개는 공란» 이라고 이미 적는다 — 없는
    값을 지어내는 것보다 못 맞췄다고 말하는 편이 낫다.
    """
    index: dict[tuple[date, str, int], str] = {}
    for row in arrivals:
        if sim_run_id is not None and row["sim_run_id"] != sim_run_id:
            continue
        for scenario in row["scenarios"] or []:
            amount = _money(scenario.get("total_amount_krw"))
            if amount is None:
                continue
            for leg in scenario.get("split_plan") or []:
                arrive = leg.get("expected_arrival_date")
                if arrive:
                    index.setdefault((row["as_of"], row["item"], amount), arrive)
    return index


def _committed(
    data: dict[str, Any], as_of: date, sim_run_id: str | None = None
) -> tuple[Table, int, list[float], int]:
    """확정 매입 표 · 이번 주 금액 · 아직 안 온 물량 · **다른 걷기라 뺀 줄 수.**

    🔴 원장도 축을 따른다. 안 그러면 이번 주 매입액이 **두 세상의 합**이 된다.
    """
    index = _arrival_index(data["arrivals"], sim_run_id)
    names = data["items"]
    week_start = as_of - timedelta(days=as_of.weekday())
    rows: list[dict[str, Any]] = []
    week_amount = 0
    inbound: list[float] = []
    off_axis = 0
    for buy in data["buys"]:
        if sim_run_id is not None and buy["sim_run_id"] != sim_run_id:
            off_axis += 1
            continue
        item = names.get(buy["item_id"], buy["item_id"])
        #  🔴 레슨 ③ — 줄 금액은 line_amount_krw 다. total 을 쓰면 여러 줄인
        #     매입에서 같은 금액이 줄마다 반복된다.
        amount = _money(buy["line_amount_krw"])
        arrive = index.get((buy["purchase_date"], item, amount))
        qty = float(buy["quantity_kg"])
        rows.append({
            "approval": buy["purchase_id"],
            "item": item,
            "buy": buy["purchase_date"].isoformat(),
            "arrive": arrive,  # 못 맞추면 공란. 0 도 오늘도 아니다
            "grade": buy["grade"],  # 원장이 NULL 이면 공란
            "qty": f"{qty:,.0f} kg",
            "unit": f"{_money(buy['unit_price_krw_per_kg']):,}",
            "amount": f"{amount:,}" if amount is not None else None,
            "pay": buy["payment_due_date"].isoformat() if buy["payment_due_date"] else None,
        })
        if amount is not None and week_start <= buy["purchase_date"] <= as_of:
            week_amount += amount
        if arrive and date.fromisoformat(arrive) > as_of:
            inbound.append(qty)
    return (
        Table(columns=_COMMITTED_COLS, rows=rows, empty_text=_EMPTY_COMMITTED),
        week_amount,
        inbound,
        off_axis,
    )


# ══════════════════════════════════════════════════════════════════════════
#  예시값 — DB 를 못 읽을 때만
# ══════════════════════════════════════════════════════════════════════════

_DEMO_REASONS = [
    Reason(source="예측", text="D+14 예측 −2.8%, 신뢰구간 폭 63.9%", ref="FC-2026-01-06"),
    Reason(source="시세관측", text="가락 2026-01-05 경락가 925원/kg 등 1개 등급",
           ref="MQ-가락-2026-01-05"),
    Reason(source="주문", text="확정주문 10,042.2kg → 일평균 717kg", ref="SO-2026-01-06"),
    Reason(source="재고", text="가용 286.92kg (로트 LOT-KIMCHI-015)", ref="INV-0106"),
    Reason(source="현금", text="재무 매입 상한 13,057,049원까지 매입 가능", ref="CASH-0106"),
]

#: 🔴 **문서 줄은 ⑥이 만드는 문장을 베낀 것이다** — 두 자리가 갈리면 예시값이 실물과
#:   다른 말을 한다. 원본은 ``package_scenarios._context_risks`` 이고, 문면을 고칠 때
#:   **여기를 같이 본다** (2026-09-09 · E3-5).
#:
#:   전에는 *"참조 가능한 발간물 0건"* 이었는데 **그건 다른 상태의 문장**이다. 운영은
#:   문서를 읽으려다 못 읽는 쪽이라 예시도 그쪽을 보여야 한다 — 실물 기록 158건이
#:   그 문장이었고, 읽는 사람에게는 *"그날 그 문서가 세상에 없었다"* 로 읽혔다.
_DEMO_RISKS = [
    "기존 로트 LOT-KIMCHI-015 잔여신선도 4일 — 새로 사는 물량이 이 로트를 밀어내지 않는지",
    "기준등급 ‘상’이 당일 시세에 없어 ‘특’으로 배정했다",
    "문서 1종을 요청했으나 읽지 못했다 — 그날 발간물이 0건이었다는 뜻이 아니다",
]


def _demo_plan(label: str, coverage: str, qty: float, amount: int, cap: int) -> Plan:
    return Plan(
        key=f"배추 · {label}", coverage=coverage, knob="수량으로 조절",
        qty_kg=qty, amount_krw=amount, unit_price=925, grade="특",
        max_price=cap, cut_unit_price=cap,
        legs=Table(columns=_LEG_COLS, rows=[
            {"leg": 1, "buy": "2026-01-06", "qty": f"{qty:,.0f} kg", "arrive": "2026-01-08"},
        ]),
        payments=Table(columns=_PAY_COLS, rows=[],
                       empty_text="예시값입니다 — 지급일 규칙이 아직 미결입니다"),
        reasons=list(_DEMO_REASONS), risks=list(_DEMO_RISKS), pending=True,
    )


def _demo(note: str) -> PurchaseTab:
    plans = [
        _demo_plan("보수", "2일치", 1435, 1_327_375, 1095),
        _demo_plan("기본", "5일치", 3587, 3_318_475, 1095),
    ]
    return PurchaseTab(
        stats=[
            Stat(label="오늘 제안", value="2", unit="안", detail="예시값", raw=2),
            Stat(label="승인 대기", value="2", unit="건", detail="예시값", tone="warn", raw=2),
            Stat(label="이번 주 확정 매입액", value="3,063,298", unit="원",
                 detail="예시값", tone="warn", raw=3_063_298),
            Stat(label="확정 입고 예정", value="3,587", unit="kg",
                 detail="예시값", tone="good", raw=3587),
        ],
        plans=plans,
        plans_note=Note(tone="warn", text=f"**예시값입니다.** {note}"),
        committed=Table(columns=_COMMITTED_COLS, rows=[], empty_text=_EMPTY_COMMITTED),
        committed_note=Note(tone="warn", text="**예시값입니다.** 확정 매입을 못 읽었습니다."),
        source=Source(filled=False, owner="매입", note=note),
    )


# ══════════════════════════════════════════════════════════════════════════
#  본체
# ══════════════════════════════════════════════════════════════════════════

def build(as_of: date, sim_run_id: str | None = None) -> PurchaseTab:
    """매입 탭. ``sim_run_id`` 는 **어느 걷기를 보는가**다.

    🔴 **안 주면 안 거른다.** 지금 DB 에는 축이 붙기 전 실행이 1,202건 있고, 그것을
    무조건 걸러 버리면 스무 날이 통째로 빈다 (2026-09-10 실측). 축이 갈리는 날 화면이
    두 세상을 섞지 않도록 **자리를 먼저 만들어 두는 것**이 이 인자다.

    ⚠️ 값을 여기서 짓지 않는다 — 받아서 그대로 흘린다 (마스터 당부 2026-09-10).
    """
    #  ★ 통째로 잡는 것이 맞습니다 — 여기서 무슨 일이 나든 **화면은 떠야** 하고
    #    대신 「예시값」 딱지가 붙습니다. 예외 종류를 골라 잡으면 안 골라낸
    #    하나 때문에 화면이 통째로 죽습니다 (ML 이 forecast 에서 같은 판단).
    try:
        data = _read(as_of)
    except Exception as error:  # noqa: BLE001  DB 미연결 · 표 없음 둘 다
        log.info("매입 값을 못 읽어 예시값을 씁니다: %s", error)
        return _demo(f"DB 를 못 읽었습니다 ({type(error).__name__})")

    runs = data["runs"]
    decided = {
        (row["request_id"], row["scenario_label"]): row["decision"]
        for row in data["decisions"]
        if row["scenario_label"]
    }
    chosen, picked_text = _pick(runs, sim_run_id)

    plans: list[Plan] = []
    skipped = 0
    for run in chosen:
        for scenario in (run["payload"] or {}).get("scenarios") or []:
            #  _pick 이 ITEMS 로 걸렀으므로 여기서 item 은 언제나 계약 품목이다
            plan = _plan(str(run["item"]), scenario, decided, run["request_id"])
            if plan is None:
                skipped += 1
            else:
                plans.append(plan)

    committed, week_amount, inbound, committed_off_axis = _committed(data, as_of, sim_run_id)
    week_start = as_of - timedelta(days=as_of.weekday())
    pending = sum(1 for p in plans if p.pending)
    no_cut = [p.key for p in plans if p.cut_unit_price is None]

    if plans:
        text = picked_text
        if skipped:
            text += f". 등급 배분이 비어 화면에 못 올린 안 {skipped}개"
        if no_cut:
            #  🔴 09-08 에 갈라 둔 칸이 옛 실행에는 없다. max_price 로 메우면
            #     두 값이 화면에서 다시 하나가 된다.
            text += (
                f". ⚠️ 컷 기준 칸이 없는 안 {len(no_cut)}개 — 이 실행보다 그 칸이"
                " 나중에 생겼습니다. 재무 스트레스 기준으로 대신 읽지 마세요"
            )
        plans_note = Note(tone="neutral", text=text)
    else:
        plans_note = _no_plan_note(runs, picked_text, sim_run_id)

    arrived_unknown = sum(1 for row in committed.rows if row["arrive"] is None)
    committed_text = (
        "승인(MASTER_APPROVAL)으로 원장에 남은 매입만 봅니다 — "
        f"{as_of.isoformat()} 까지 {len(committed.rows)}줄."
    )
    if arrived_unknown:
        committed_text += f" 도착일을 못 맞춘 줄 {arrived_unknown}개는 **공란**입니다."
    #  🔴 뺀 것을 조용히 없애지 않는다 — 이번 주 매입액이 왜 작은지가 여기 있다.
    if committed_off_axis:
        committed_text += (
            f" 다른 걷기의 줄 {committed_off_axis}개는 뺐습니다 —"
            f" 지금 보는 것은 {sim_run_id} 입니다."
        )
    if committed.rows:
        committed_text += (
            " ⚠️ 지급일이 매입일과 같게 적재돼 있습니다 — 지급일 규칙이 아직 미결입니다."
        )

    return PurchaseTab(
        stats=[
            Stat(label="오늘 제안", value=str(len(plans)), unit="안",
                 detail=f"{as_of.isoformat()} · 실행 {len(runs)}건 중 고른 {len(chosen)}건",
                 raw=len(plans)),
            Stat(label="승인 대기", value=str(pending), unit="건",
                 detail="사람이 고르면 확정 매입이 생깁니다",
                 tone="warn" if pending else "neutral", raw=pending),
            Stat(label="이번 주 확정 매입액", value=f"{week_amount:,}", unit="원",
                 detail=f"{week_start.isoformat()} ~ {as_of.isoformat()} · 승인분 줄 금액 합계",
                 tone="warn" if week_amount else "neutral", raw=week_amount),
            Stat(label="확정 입고 예정", value=f"{sum(inbound):,.0f}", unit="kg",
                 detail=f"{as_of.isoformat()} 이후 도착 예정 {len(inbound)}건",
                 tone="good" if inbound else "neutral", raw=sum(inbound)),
        ],
        plans=plans,
        plans_note=plans_note,
        committed=committed,
        committed_note=Note(tone="neutral", text=committed_text),
        source=Source(
            filled=True, owner="매입",
            note="master_agent_runs · master_decisions · purchases · purchase_items · items",
        ),
    )
