"""매입안 보고서 — **사람이 들고 나가 읽는 문서.**

`answer.py` 와 무엇이 다른가:

```text
answer.py   대화창에 붙는 답. 짧게 결론만.
report.py   들고 나가는 문서. 안마다 매입량·금액·등급·이유와 부서 검토를 편다.
```

★ **실제 서비스 사용자에게 필요한 것만 사람 말로 싣는다** (2026-09-15 결정).
  검증 기록(지적·확인 필요·못 돈 검사·검사 커버리지) · 종료 코드 · 요청 번호 원문 ·
  입력 출처 · 근거 등급 · 참조 번호 · 영문 코드 · 빈 칸 알림은 **문서에 넣지 않는다.**
  그 기록은 실행 이력(`response_payload`)에 그대로 남아 있고 이 문서가 주인이 아니다.

★ **값을 만들지 않는다.** 저장된 실행에 있는 것만 옮긴다 — 합계도 다시 세지 않는다.
  보고서가 계산을 시작하면 화면과 문서가 **다른 숫자**를 말하게 된다.
  단위를 바꿔 적는 것(원 → 만 원)은 계산이 아니라 표기다.

★ **부서가 준 문장은 판단하지 않고 다듬기만 한다.** 알려진 모양이면 짧은 사람 말로
  바꾸고, 늘 뜨는 개발용 문장은 빼고, 모르는 문장은 원문을 두되 코드·영문·참조 번호만
  벗긴다. 벗기고 나서 한국어가 안 남으면 뺀다.

★ 판정·점검 항목 문구는 화면 사전 `frontend/src/lib/procurementLabels.ts` 와 **같은 뜻**
  이어야 한다 (`STATUS_LABEL` · `CLAIM_LABEL` · `LOGISTICS_CHECK_LABEL` ·
  `CHECK_STATUS_LABEL` · `financeSummary` · `logisticsSummary`). 파이썬이라 여기 한 벌을
  더 두므로 **한쪽을 고치면 다른 쪽도 고친다.**

★ Markdown 이다. 붙여 넣기·메신저·이슈 어디에도 그대로 들어간다.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

from app.master.answer import agent_label

# ─── 화면 사전과 같은 문구 (frontend/src/lib/procurementLabels.ts) ──────────

#: `STATUS_LABEL` 과 같다.
_VERDICT_LABEL: dict[str, str] = {"ok": "통과", "conditional": "조건부", "reject": "거절"}

#: `LOGISTICS_CHECK_LABEL` 과 같다. 여기 없는 항목은 문서에 안 나온다.
_LOGISTICS_CHECK_LABEL: dict[str, str] = {
    "LOG-H01": "창고 용량",
    "LOG-H02": "구역별 용량",
    "LOG-H03": "하루 입고량",
    "LOG-H04": "입고 운송량",
    "LOG-H05": "입고 소요일",
}

#: `CHECK_STATUS_LABEL` 과 같다.
_CHECK_STATUS_LABEL: dict[str, str] = {
    "PASS": "통과",
    "UNRESOLVED": "확인 못 함(정보 없음)",
    "FAIL": "넘침",
}

#: `CLAIM_LABEL` 중 이 문서가 쓰는 것. 같은 뜻을 같은 이름으로 부른다.
_LABEL_CAP = "매입에 쓸 수 있는 한도"
_LABEL_CASH_MIN = "이 안 실행 뒤 최저 현금"
_LABEL_STRESS_CASH_MIN = "회수가 늦어질 때 최저 현금"
_LABEL_MAX_PRICE = "이보다 비싸면 안 사는 단가"

#: 결정 → 상태 문장. 결정이 없으면 종료 결과로 적는다.
_DECISION_STATUS: dict[str, str] = {
    "APPROVE": "{label} 승인됨",
    "REJECT_ALL": "모든 안을 쓰지 않기로 함",
    "REQUEST_CHANGE": "안을 고쳐 달라고 요청함",
    "CANCEL": "승인했던 매입을 취소함",
}

_GRADE_WORD: dict[str, str] = {"특": "특품", "상": "상품", "중": "중품", "하": "하품"}

# ─── 값 형식 ───────────────────────────────────────────────────────────────


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _won(value: Any) -> str | None:
    """상세용. 원 단위 쉼표."""
    n = _num(value)
    return None if n is None else f"{round(n):,}원"


def _won_short(value: Any) -> str | None:
    """요약용. `formatWon` 과 같다 — 1만 원 이상은 만 원 단위로 반올림한다."""
    n = _num(value)
    if n is None:
        return None
    if abs(n) >= 10000:
        return f"{round(n / 10000):,}만 원"
    return f"{round(n):,}원"


def _kg(value: Any) -> str | None:
    n = _num(value)
    return None if n is None else f"{round(n):,}kg"


def _date(ymd: Any) -> str:
    """`2026-01-13` → 「2026년 1월 13일」. 모양이 다르면 받은 그대로."""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(ymd or ""))
    if not m:
        return str(ymd or "")
    return f"{m.group(1)}년 {int(m.group(2))}월 {int(m.group(3))}일"


def _short_date(ymd: Any) -> str:
    """`2026-01-13` → 「1월 13일」."""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(ymd or ""))
    if not m:
        return str(ymd or "")
    return f"{int(m.group(2))}월 {int(m.group(3))}일"


def _object_particle(word: str) -> str:
    """받침이 있으면 「을」, 없으면 「를」. 화면 `objectParticle` 과 같다."""
    if not word:
        return "을(를)"
    code = ord(word[-1]) - 0xAC00
    if code < 0 or code > 11171:
        return "을(를)"
    return "를" if code % 28 == 0 else "을"


def _scenario_name(label: Any, index: int | None = None) -> str:
    """「보수」 → 「보수안」. 화면 `scenarioName` 과 같다."""
    text = _clean(label) if label else ""
    if text:
        return text if text.endswith("안") else f"{text}안"
    return "안" if index is None else f"{index + 1}번째 안"


def _dept(agent: str) -> str:
    label = agent_label(agent)
    return label if _HANGUL.search(label) else "다른 부서"


def _grade(grade: Any) -> str:
    text = str(grade or "").strip()
    return _GRADE_WORD.get(text, text)


def _grades(text: str) -> str:
    """「상·중」 → 「상·중품」."""
    parts = [p.strip() for p in re.split(r"[·,]", text) if p.strip()]
    if not parts:
        return text
    if all(p in _GRADE_WORD for p in parts):
        return "·".join(parts) + "품"
    return text


# ─── 문장 다듬기 ───────────────────────────────────────────────────────────

_HANGUL = re.compile(r"[가-힣]")

#: 코드로 읽히는 조각. 벗겨 낸다.
_CODE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"`[^`]*`"),
    # 참조 번호: FC-ops_auc-2026-01-13 · MQ-가락-2026-01-12 · REQ-20260113-0001
    re.compile(r"\b[A-Z][A-Z0-9]{1,}(?:[-_:][0-9A-Za-z가-힣.]+)+"),
    # 종료 코드·상수: E1_APPROVED · SNAPSHOT_ID_UNRESOLVED
    re.compile(r"\b[A-Z0-9]+(?:_[A-Z0-9]+)+\b"),
    # 필드명: sim_run_id · policy_version_used
    re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b"),
    # 영어 단어 덩어리
    re.compile(r"[A-Za-z][A-Za-z0-9.'/-]*(?:\s+[A-Za-z][A-Za-z0-9.'/-]*)*"),
)


def _clean(text: Any) -> str:
    """코드·영문·참조 번호를 벗긴다. 한국어가 안 남으면 빈 문자열."""
    out = str(text or "")
    for pattern in _CODE_PATTERNS:
        out = pattern.sub("", out)
    out = re.sub(r"\(\s*[/·,:=\s]*\)", "", out)
    out = re.sub(r"\s+([,.·)])", r"\1", out)
    out = re.sub(r"\(\s+", "(", out)
    out = re.sub(r"\s{2,}", " ", out).strip(" -—:·,")
    return out if _HANGUL.search(out) else ""


#: 매 실행 뜨는 개발용 문장. 사람에게 뜻이 없어 뺀다.
_NOISE_RISKS: tuple[re.Pattern[str], ...] = (
    re.compile(r"문서 \d+종을 요청했으나 읽지 못했다"),
    re.compile(r"판단 재료.*읽지 못해"),
)

#: 부서가 늘 붙이는 채움 문장.
_NOISE_REASONING: frozenset[str] = frozenset({"매입 시나리오를 물류 관점에서 판정했다."})


def _risk_text(risk: Any) -> str:
    """이 안의 위험 한 줄 → 사람 말. 빼야 할 문장이면 빈 문자열."""
    text = str(risk or "").strip()
    if not text or any(p.search(text) for p in _NOISE_RISKS):
        return ""
    m = re.search(r"기준등급 '([^']+)'이 당일 시세에 없어 '([^']+)'", text)
    if m:
        wanted, used = _grade(m.group(1)), _grade(m.group(2))
        return f"기준 등급인 {wanted}이 당일 시세에 없어 {used}으로 샀습니다."
    m = re.search(
        r"등급 배분 보류 — (\S+) (\d{4}-\d{2}-\d{2}) .*?에서 (\S+) 등급 거래가 없다"
        r".*전량 (\S+) 단일 등급으로 배정했다",
        text,
    )
    if m:
        return (
            f"{m.group(1)} {_short_date(m.group(2))} 경매에 {_grades(m.group(3))} 거래가 없어 "
            f"{_grade(m.group(4))} 한 등급으로 샀습니다."
        )
    return _clean(text)


#: 근거 출처 → 사람 말 머리.
_SOURCE_HEAD: dict[str, str] = {
    "예측": "가격 전망",
    "시세관측": "시세",
    "주문": "수요",
    "현금": "자금",
    "재고": "재고",
}


def _rationale_line(item: Mapping[str, Any]) -> str:
    """근거 한 줄 → 「머리: 사람 말」. 등급·참조 번호는 싣지 않는다."""
    source = str(item.get("source") or "")
    claim = str(item.get("claim") or "").strip()
    head = _SOURCE_HEAD.get(source) or _clean(source) or "근거"

    m = re.search(r"D\+(\d+) 예측 ([+-]?[\d.]+)%(?:, 신뢰구간 폭 ([\d.]+)%)?", claim)
    if m:
        days = int(m.group(1))
        when = f"{days // 7}주" if days % 7 == 0 else f"{days}일"
        change = float(m.group(2))
        direction = "낮을" if change < 0 else "높을"
        body = f"{when} 뒤 가격이 지금보다 {abs(change):.1f}% {direction} 것으로 예측"
        if change == 0:
            body = f"{when} 뒤 가격이 지금과 비슷할 것으로 예측"
        if m.group(3):
            body += f" (예측 범위 폭 {float(m.group(3)):.1f}%)"
        return f"{head}: {body}"

    m = re.search(r"(\S+) (\d{4}-\d{2}-\d{2}) 경락가 ([\d,]+원/kg)", claim)
    if m:
        return f"{head}: {m.group(1)} {_short_date(m.group(2))} 경락가 {m.group(3)}"

    m = re.search(r"확정주문 ([\d,.]+)kg → 일평균 ([\d,.]+)kg × D=(\d+)", claim)
    if m:
        total = _kg(m.group(1).replace(",", ""))
        daily = _kg(m.group(2).replace(",", ""))
        return f"{head}: 확정 주문 {total} · 하루 평균 {daily} × {m.group(3)}일치"

    m = re.search(r"재무 매입 상한 ([\d,]+)원", claim)
    if m:
        return f"{head}: {_LABEL_CAP} {m.group(1)}원 안에서 삽니다"

    m = re.search(r"입고 여유 ([\d,.]+)kg", claim)
    if m:
        return f"창고: 입고 여유 {_kg(m.group(1).replace(',', ''))}"

    if "가용 재고 없음" in claim:
        return f"{head}: 쓸 수 있는 재고 없음"

    body = _clean(re.sub(r"\((확정|추정)\)", "", claim))
    return f"{head}: {body}" if body else ""


def _reason_text(run: Mapping[str, Any]) -> str:
    """실행이 멈춘 사유 → 사람 말. 알려진 모양만 바꾸고 나머지는 벗기기만 한다."""
    judgment = run.get("judgment") or {}
    raw = str(judgment.get("no_proposal_reason") or run.get("reason") or "").strip()
    end_code = str(run.get("end_code") or "")

    if raw.startswith("mock 입력"):
        return "필요한 자료를 읽지 못해 판단을 시작하지 않았습니다."
    if raw.startswith("호출 예산 소진"):
        return "정해진 검토 횟수 안에 판단을 끝내지 못했습니다."
    if raw.startswith("매입 에이전트 미가동"):
        return "매입 담당이 안을 만들지 못했습니다."
    if raw.startswith("경계를 내지 못한 에이전트"):
        names = [_dept(a) for a in re.findall(r"([a-z]+)\(", raw)]
        who = "·".join(dict.fromkeys(names)) or "부서"
        return f"매입 전 {who} 확인을 마치지 못해 판단을 시작하지 않았습니다."
    if raw.startswith("판정을 받지 못해"):
        names = [n for n in re.findall(r"([가-힣]+)\(", raw)]
        who = "·".join(dict.fromkeys(names)) or "부서"
        return f"{who} 검토를 받지 못해 안을 확정하지 않았습니다."
    if raw.startswith("매입 재호출") or end_code == "E3_REJECTED":
        return "여러 번 다시 만들었지만 부서 검토를 통과한 안이 없습니다."
    return _clean(raw) or "사유를 받지 못했습니다."


# ─── 부서 검토 ─────────────────────────────────────────────────────────────


def _finance_lines(verdict: Mapping[str, Any]) -> list[str]:
    """`financeSummary` 와 같은 내용. 안별 최저 현금·지급일, 공통 한도는 한 번."""
    payload = verdict.get("payload") or {}
    rows = [r for r in (payload.get("verdicts") or []) if isinstance(r, Mapping)]
    out: list[str] = []
    caps = [_num(r.get("finance_cap_amount_krw")) for r in rows]
    shared = caps[0] if caps and all(c is not None and c == caps[0] for c in caps) else None
    if shared is not None:
        out.append(f"  - {_LABEL_CAP} {_won_short(shared)}")
    for index, row in enumerate(rows):
        name = _scenario_name(row.get("scenario_id") or row.get("label"), index)
        parts = [f"{name} · {_VERDICT_LABEL.get(str(row.get('verdict')), '판정 없음')}"]
        reason = _clean(row.get("reason"))
        if reason:
            parts.append(reason)
        if shared is None and _num(row.get("finance_cap_amount_krw")) is not None:
            parts.append(f"{_LABEL_CAP} {_won_short(row.get('finance_cap_amount_krw'))}")
        if _num(row.get("scenario_projected_cash_min")) is not None:
            parts.append(f"{_LABEL_CASH_MIN} {_won_short(row['scenario_projected_cash_min'])}")
        if _num(row.get("stress_projected_cash_min")) is not None:
            parts.append(f"{_LABEL_STRESS_CASH_MIN} {_won_short(row['stress_projected_cash_min'])}")
        payments = [
            f"{_short_date(p.get('payment_date'))} {_won_short(p.get('amount_krw'))}"
            for p in (row.get("payment_schedule") or [])
            if isinstance(p, Mapping)
            and p.get("payment_date")
            and _num(p.get("amount_krw")) is not None
        ]
        if payments:
            parts.append("지급 " + ", ".join(payments))
        adjustments = len(row.get("suggested_adjustments") or [])
        if adjustments:
            parts.append(f"재무가 이 안을 고치자는 제안 {adjustments}건을 냈습니다")
        out.append("  - " + " — ".join(parts[:2]) + "".join(f" · {p}" for p in parts[2:]))
    return out


def _logistics_lines(verdict: Mapping[str, Any], status: str) -> list[str]:
    """`logisticsSummary` 와 같은 내용. 조건부 풀이·안별 판정·도착일·도착일 여유·점검."""
    payload = verdict.get("payload") or {}
    if not isinstance(payload, Mapping):
        return []
    scenarios = [s for s in (payload.get("scenario_results") or []) if isinstance(s, Mapping)]
    checks: list[tuple[str, str | None]] = []
    for c in payload.get("hard_constraints") or []:
        if not isinstance(c, Mapping):
            continue
        name = _LOGISTICS_CHECK_LABEL.get(str(c.get("code") or ""))
        if name:
            checks.append((name, c.get("status")))
    unresolved = [n for n, s in checks if s == "UNRESOLVED"]

    out: list[str] = []
    if (
        (status or payload.get("verdict")) == "conditional"
        and scenarios
        and all(s.get("verdict") == "ok" for s in scenarios)
        and unresolved
    ):
        names = "·".join(unresolved)
        particle = _object_particle(names)
        out.append(f"  - 안에는 문제가 없고, {names}{particle} 확인하지 못해 조건부입니다.")
    for index, s in enumerate(scenarios):
        name = _scenario_name(s.get("label"), index)
        out.append(f"  - {name} · {_VERDICT_LABEL.get(str(s.get('verdict')), '판정 없음')}")
    arrivals = [str(d) for d in (payload.get("expected_arrival_dates") or []) if d]
    cap_by_date = payload.get("cap_by_date") or {}
    for day in arrivals:
        line = f"  - 도착 예정일 {_date(day)}"
        free = _kg(cap_by_date.get(day)) if isinstance(cap_by_date, Mapping) else None
        if free:
            line += f" · 그날 창고 여유 {free}"
        out.append(line)
    attention = [(n, s) for n, s in checks if s != "PASS"]
    if attention:
        out.append(
            "  - 점검: "
            + " · ".join(
                f"{n} {_CHECK_STATUS_LABEL.get(str(s), '판정 없음')}" for n, s in attention
            )
        )
    elif checks:
        out.append("  - 점검: " + "·".join(n for n, _ in checks) + " 모두 통과")
    return out


def _review_block(verdicts: Mapping[str, Any]) -> list[str]:
    out = ["## 부서 검토", ""]
    for agent, verdict in verdicts.items():
        if not isinstance(verdict, Mapping):
            continue
        dept = _dept(str(agent))
        status = str(verdict.get("business_status") or "")
        label = _VERDICT_LABEL.get(status)
        reasoning = str(verdict.get("reasoning") or "").strip()
        why = "" if reasoning in _NOISE_REASONING else _clean(reasoning)
        if label is None or verdict.get("runtime_status") not in (None, "READY"):
            # 🔴 영어 상태 코드를 찍지 않는다. 판정이 없다는 사실은 사람 말로 남긴다.
            out.append(f"- {dept} · 판정을 내지 못함 — {why or '사유를 받지 못했습니다.'}")
            continue
        out.append(f"- {dept} · {label}" + (f" — {why}" if why else ""))
        if agent == "finance":
            out += _finance_lines(verdict)
        elif agent == "inventory":
            out += _logistics_lines(verdict, status)
    out.append("")
    return out


# ─── 안 ────────────────────────────────────────────────────────────────────


def _payments_from_finance(run: Mapping[str, Any], label: Any) -> bool:
    finance = (run.get("verdicts") or {}).get("finance") or {}
    payload = finance.get("payload") if isinstance(finance, Mapping) else None
    for row in (payload or {}).get("verdicts") or []:
        if isinstance(row, Mapping) and row.get("scenario_id") == label:
            return bool(row.get("payment_schedule"))
    return False


def _scenario_block(
    scenario: Mapping[str, Any], index: int, run: Mapping[str, Any] | None = None
) -> list[str]:
    name = _scenario_name(scenario.get("label"), index)
    out = [f"### {name}", ""]

    head = [
        p
        for p in (
            f"매입량 {_kg(scenario.get('total_qty_kg'))}"
            if _kg(scenario.get("total_qty_kg"))
            else "",
            f"금액 {_won(scenario.get('total_amount_krw'))}"
            if _won(scenario.get("total_amount_krw"))
            else "",
            f"{int(_num(scenario.get('coverage_days')) or 0)}일치"
            if _num(scenario.get("coverage_days"))
            else "",
        )
        if p
    ]
    split = [p for p in (scenario.get("split_plan") or []) if isinstance(p, Mapping)]
    if len(split) == 1:
        line = f"한 번에 발주 ({_date(split[0].get('date'))})"
        if split[0].get("expected_arrival_date"):
            line += f" · 도착 예정 {_date(split[0]['expected_arrival_date'])}"
        head.append(line)
    elif len(split) > 1:
        head.append(f"{len(split)}번에 나눠 발주")
    out.append("- " + " · ".join(head))

    if len(split) > 1:
        for i, p in enumerate(split):
            seq = p.get("seq", i + 1)
            line = f"  - {seq}회차 {_date(p.get('date'))} · {_kg(p.get('qty_kg')) or ''}"
            if p.get("expected_arrival_date"):
                line += f" · 도착 예정 {_date(p['expected_arrival_date'])}"
            out.append(line.rstrip(" ·"))

    max_price = scenario.get("max_price")
    if max_price is None:
        max_price = scenario.get("cut_unit_price")
    if _won(max_price):
        out.append(f"- {_LABEL_MAX_PRICE} {_won(max_price)}/kg")
    # `null` 은 적지 않는다. 0 으로도 적지 않는다 — 마진이 없는 것과 0 인 것은 다르다.
    if _num(scenario.get("expected_margin_rate")) is not None:
        out.append(f"- 기대 마진율 {float(scenario['expected_margin_rate']) * 100:.1f}%")

    sourcing = [s for s in (scenario.get("sourcing_plan") or []) if isinstance(s, Mapping)]
    cells = [
        " · ".join(
            x
            for x in (
                _clean(s.get("market")),
                _grade(s.get("grade")),
                _kg(s.get("qty_kg")) or "",
                f"{_won(s.get('grade_unit_price'))}/kg" if _won(s.get("grade_unit_price")) else "",
            )
            if x
        )
        for s in sourcing
    ]
    if len(cells) == 1:
        out.append(f"- 어디서 어떤 등급을: {cells[0]}")
    elif cells:
        out.append("- 어디서 어떤 등급을")
        out += [f"  - {c}" for c in cells]

    payments = [p for p in (scenario.get("payment_schedule") or []) if isinstance(p, Mapping)]
    if payments and not (run and _payments_from_finance(run, scenario.get("label"))):
        out.append("- 대금 지급")
        out += [
            f"  - {_date(p.get('payment_date'))} · {_won(p.get('amount_krw')) or ''}".rstrip(" ·")
            for p in payments
        ]

    reasons = [r for r in (_rationale_line(i) for i in scenario.get("rationale") or []) if r]
    if reasons:
        out.append("- 왜 이만큼인가")
        out += [f"  - {r}" for r in reasons]

    notes = [r for r in (_risk_text(x) for x in scenario.get("risks") or []) if r]
    warning = _clean(scenario.get("margin_warning"))
    if warning:
        notes.insert(0, warning)
    if notes:
        out.append("- 알아 둘 점")
        out += [f"  - {n}" for n in dict.fromkeys(notes)]
    out.append("")
    return out


def _status_line(run: Mapping[str, Any], decision: Mapping[str, Any] | None) -> str:
    if decision:
        template = _DECISION_STATUS.get(str(decision.get("decision") or ""))
        if template:
            return template.format(label=_scenario_name(decision.get("scenario_label")))
    if run.get("end_code") == "E1_APPROVED":
        return "선택 대기"
    return f"확정할 수 없음 — {_reason_text(run)}"


def _item_of(run: Mapping[str, Any], item: str | None) -> str:
    judgment = run.get("judgment") or {}
    meta = judgment.get("meta") if isinstance(judgment, Mapping) else None
    found = (meta or {}).get("item") if isinstance(meta, Mapping) else None
    return _clean(found or item or "")


def render_report(
    run: Mapping[str, Any],
    *,
    item: str | None = None,
    decision: Mapping[str, Any] | None = None,
) -> str:
    """저장된 실행 하나 → 사람이 읽는 매입안 Markdown.

    `item` 은 요청 품목이다 — 매입 회신(`judgment.meta.item`)에 없을 때만 쓴다.
    `decision` 은 이 실행에 붙은 현재 결정이다. 없으면 선택 대기로 적는다.

    ★ 실행이 안을 안 냈으면 **왜 없는지**가 본문이다. 빈 문서는 "아직 안 돌았나" 로 읽힌다.
    """
    scenarios = [s for s in (run.get("scenarios") or []) if isinstance(s, Mapping)]
    judgment = run.get("judgment") or {}
    name = _item_of(run, item)
    title = f"{name} 매입안" if name else "매입안"
    out = [
        f"# {title} — {_date(run.get('as_of'))}",
        "",
        "> 이 문서는 매입 제안입니다. 사람이 안을 골라야 발주가 확정됩니다.",
        "",
    ]

    if not scenarios:
        out += [f"오늘은 매입안을 내지 않았습니다 — {_reason_text(run)}", ""]
        rejected = [r for r in (judgment.get("rejected_reasons") or []) if isinstance(r, Mapping)]
        lines = [
            f"- {_scenario_name(r.get('label'), i)}: {_clean(r.get('reason'))}"
            for i, r in enumerate(rejected)
            if _clean(r.get("reason"))
        ]
        if lines:
            out += ["검토했지만 내지 않은 안", "", *lines, ""]
        verdicts = run.get("verdicts") or {}
        if verdicts:
            out += _review_block(verdicts)
        return "\n".join(out).rstrip() + "\n"

    out += [
        "## 한눈에",
        "",
        f"| 안 | 매입량 | 금액 | 며칠치 | {_LABEL_MAX_PRICE} |",
        "|---|---:|---:|---:|---:|",
    ]
    for index, s in enumerate(scenarios):
        max_price = (
            s.get("max_price") if s.get("max_price") is not None else s.get("cut_unit_price")
        )
        days = _num(s.get("coverage_days"))
        out.append(
            "| "
            + " | ".join(
                (
                    _scenario_name(s.get("label"), index),
                    _kg(s.get("total_qty_kg")) or "",
                    _won_short(s.get("total_amount_krw")) or "",
                    f"{int(days)}일" if days is not None else "",
                    f"{_won(max_price)}/kg" if _won(max_price) else "",
                )
            )
            + " |"
        )
    out += ["", f"상태: {_status_line(run, decision)}", ""]

    verdicts = run.get("verdicts") or {}
    if verdicts:
        out += _review_block(verdicts)

    out += ["## 안별 상세", ""]
    for index, scenario in enumerate(scenarios):
        out += _scenario_block(scenario, index, run)

    return "\n".join(out).rstrip() + "\n"


def report_filename(run: Mapping[str, Any], *, item: str | None = None) -> str:
    """`2026-01-13_배추_매입안.md`. 같은 날 두 번째 판단부터 `_2` 를 붙인다.

    순번은 요청 번호 끝자리에서 읽는다 — 파일 이름에 요청 번호 원문은 싣지 않는다.
    """
    name = _item_of(run, item)
    as_of = str(run.get("as_of") or "")
    m = re.search(r"-(\d+)$", str(run.get("request_id") or ""))
    seq = int(m.group(1)) if m else 1
    parts = [p for p in (as_of, name, "매입안") if p]
    stem = "_".join(parts)
    if seq > 1:
        stem += f"_{seq}"
    return f"{stem}.md"


# ─── Finance/Sales chat reports (deterministic, LLM 0회) ─────────────────


def _chat_won(value: Any) -> str:
    """Report용 원 표시. None과 0을 절대 합치지 않는다."""
    if value is None:
        return "—"
    try:
        return f"{round(float(value)):,}원"
    except (TypeError, ValueError):
        return "—"


def render_finance_chat_report(*, sim_run_id: str, as_of, start_date, end_date) -> dict[str, Any]:
    """기존 Finance read model만으로 만드는 보고서. LLM/새 계산 없음."""
    from app.finance.console_credit import get_console_credit
    from app.finance.console_expenses import get_console_expenses
    from app.finance.console_payables import get_console_payables
    from app.finance.console_receivables import get_console_receivables
    from app.finance.dashboard import get_finance_cashflow, get_finance_dashboard

    # 기말 상태 KPI는 요청 시점이 아니라 보고서 종료일의 동일 실행 read model을 쓴다.
    dashboard = get_finance_dashboard(sim_run_id=sim_run_id, as_of=end_date)
    receivables = get_console_receivables(sim_run_id=sim_run_id, as_of=end_date)
    payables = get_console_payables(sim_run_id=sim_run_id, as_of=end_date)
    expenses = get_console_expenses(
        sim_run_id=sim_run_id,
        as_of=end_date,
        from_date=start_date,
        to_date=end_date,
    )
    credit = get_console_credit(sim_run_id=sim_run_id, as_of=end_date)
    # Exact report range: do not silently cap a user-selected period to a recent horizon.
    days = max(1, (end_date - start_date).days + 1)
    cashflow = get_finance_cashflow(sim_run_id=sim_run_id, as_of=end_date, days=days)
    cashflow_dates = [row.close_date for row in getattr(cashflow, "cashflow", [])]
    dashboard_meta = getattr(dashboard, "meta", None)

    # Finance report 화면·PDF의 정본은 아래 read-model facts다. Markdown 조립은
    # 더 이상 생성 경로가 아니며, 보고서가 표시값을 다시 계산하지 않는다.
    return {
        "kind": "FINANCE",
        "sim_run_id": sim_run_id,
        "as_of": end_date.isoformat(),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        # 요청한 기간과 실제로 읽힌 원장 기간은 다른 사실이다. 데이터가 부족해도
        # 요청 기간을 조용히 바꾸지 않고, 화면이 둘 다 설명할 수 있게 싣는다.
        "available_start_date": min(cashflow_dates).isoformat() if cashflow_dates else None,
        "available_end_date": max(cashflow_dates).isoformat() if cashflow_dates else None,
        "data_mode": getattr(dashboard_meta, "data_type", None),
        "summary": dashboard.model_dump(mode="json"),
        "cashflow": cashflow.model_dump(mode="json"),
        # The chart/table must use the same exact report-range read as the availability
        # metadata. Dashboard recent_closings is intentionally a short console preview.
        "closings": [row.model_dump(mode="json") for row in getattr(cashflow, "cashflow", [])],
        "receivables": receivables.model_dump(mode="json"),
        "payables": payables.model_dump(mode="json"),
        "expenses": expenses.model_dump(mode="json"),
        "credit": credit.model_dump(mode="json"),
    }

    lines = [
        "# 재무 보고서",
        "",
        f"- 기준 실행: `{sim_run_id}`",
        f"- 기간: {start_date.isoformat()} ~ {end_date.isoformat()}",
        "",
        "## 현재 자금",
    ]
    if dashboard.states:
        for state in dashboard.states:
            lines.append(
                f"- {state.financing_mode}: 현재 현금 {_chat_won(state.current_cash_krw)}"
                f" · 운영 여유 {_chat_won(state.operating_cash_buffer_krw)}"
                f" · 차입 잔액 {_chat_won(state.current_debt_krw)}"
            )
    else:
        lines.append("- 기록 없음")

    rs = receivables.summary
    ps = payables.summary
    es = expenses.summary
    lines += [
        "",
        "## 채권 · 채무",
        f"- 아직 받을 돈: {_chat_won(rs.total_outstanding_krw)}",
        f"- 연체 1~7일: {_chat_won(rs.days_1_7_krw)}",
        f"- 연체 8~30일: {_chat_won(rs.days_8_30_krw)}",
        f"- 연체 30일 초과: {_chat_won(rs.days_30_plus_krw)}",
        f"- 아직 지급할 매입대금: {_chat_won(ps.total_outstanding_krw)}",
        f"- 오늘 지급 예정: {_chat_won(ps.due_today_krw)}",
        f"- 7일 내 지급 예정: {_chat_won(ps.due_next_7d_krw)}",
        f"- 연체 매입대금: {_chat_won(ps.overdue_krw)}",
        "",
        "## 비용",
        f"- 미지급(ACCRUED): {_chat_won(es.accrued_krw)} · {es.accrued_count}건",
        f"- 지급(PAID): {_chat_won(es.paid_krw)}",
        f"- 취소(CANCELLED): {_chat_won(es.cancelled_krw)}",
        "",
        "## 거래처 여신",
    ]
    if credit.partners:
        for row in credit.partners:
            name = row.partner_name or row.partner_id
            lines.append(
                f"- {name}: 한도 {_chat_won(row.credit_limit_krw)}"
                f" · 미수 {_chat_won(row.current_ar_krw)}"
                f" · 가용 {_chat_won(row.available_credit_krw)}"
            )
    else:
        lines.append("- 표시할 거래처 여신 기록 없음")

    lines += ["", "## 일마감 현금 흐름"]
    if dashboard.recent_closings:
        for row in dashboard.recent_closings:
            lines.append(
                f"- {row.close_date}: 수금 {_chat_won(row.collection_cash_in_krw)}"
                f" · 매입 {_chat_won(row.purchase_cash_out_krw)}"
                f" · 물류 {_chat_won(row.logistics_cash_out_krw)}"
                f" · 급여·이자 {_chat_won(row.payroll_interest_cash_out_krw)}"
                f" · 운영비 {_chat_won(row.operating_expense_cash_out_krw)}"
                f" · 순현금 {_chat_won(row.base_net_cash_krw)}"
            )
    else:
        lines.append("- 기록 없음")

    return {
        "kind": "FINANCE",
        "sim_run_id": sim_run_id,
        "as_of": as_of.isoformat(),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "summary": dashboard.model_dump(mode="json"),
        "cashflow": cashflow.model_dump(mode="json"),
        "closings": [row.model_dump(mode="json") for row in dashboard.recent_closings],
        "receivables": receivables.model_dump(mode="json"),
        "payables": payables.model_dump(mode="json"),
        "expenses": expenses.model_dump(mode="json"),
        "credit": credit.model_dump(mode="json"),
    }


def render_sales_chat_report(*, sim_run_id: str, as_of, start_date, end_date) -> dict[str, Any]:
    """기존 Sales read model만으로 만드는 보고서. LLM/재계산 없음."""
    from app.sales.console_partners import get_console_partners
    from app.sales.console_proposals import get_console_sales_proposals
    from app.sales.console_trend import get_console_sales_trend
    from app.sales.dashboard import get_sales_dashboard

    dashboard = get_sales_dashboard(sim_run_id=sim_run_id, as_of=as_of)
    proposals = get_console_sales_proposals(sim_run_id=sim_run_id, as_of=as_of)
    days = max(1, min(400, (end_date - start_date).days + 1))
    trend = get_console_sales_trend(
        sim_run_id=sim_run_id,
        as_of=as_of,
        days=days,
        from_date=start_date,
        to_date=end_date,
    )
    partners = get_console_partners(sim_run_id=sim_run_id, as_of=as_of)
    trend_dates = [row.sale_date for row in trend.rows]
    dashboard_meta = getattr(dashboard, "meta", None)

    # 판매 보고서도 저장된 sales/read-model facts만 전달한다. 확정 여부는
    # sale_status가 실제 CONFIRMED·DELIVERED인 행만으로 제한한다.
    return {
        "kind": "SALES",
        "sim_run_id": sim_run_id,
        "as_of": as_of.isoformat(),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "available_start_date": min(trend_dates).isoformat() if trend_dates else None,
        "available_end_date": max(trend_dates).isoformat() if trend_dates else None,
        "data_mode": getattr(dashboard_meta, "data_type", None),
        "summary": dashboard.model_dump(mode="json"),
        "proposals": proposals.model_dump(mode="json"),
        "trend": trend.model_dump(mode="json"),
        "partners": partners.model_dump(mode="json"),
        "confirmed_sales": [
            row.model_dump(mode="json")
            for row in proposals.rows
            if row.sale_status in {"CONFIRMED", "DELIVERED"}
        ],
        "confirmed_count": sum(
            1 for row in proposals.rows if row.sale_status in {"CONFIRMED", "DELIVERED"}
        ),
    }

    lines = [
        "# 판매 보고서",
        "",
        f"- 기준 실행: `{sim_run_id}`",
        f"- 기간: {start_date.isoformat()} ~ {end_date.isoformat()}",
        "",
        "## 금일 판매 후보",
        f"- 전체 화면 상태: {proposals.state}",
        f"- 제시 가능: {proposals.presentable_count}건",
        f"- 검토 필요: {proposals.review_required_count}건",
        f"- 판정 대기: {proposals.unresolved_count}건",
        f"- 확정 불가: {proposals.rejected_count}건",
    ]

    confirmed = [row for row in proposals.rows if row.sale_status in {"CONFIRMED", "DELIVERED"}]
    lines += ["", "## 금일 확정 판매", f"- {len(confirmed)}건"]
    for row in confirmed:
        lines.append(
            f"- {row.item or '품목 미상'} · {row.partner_id or '거래처 미상'}"
            f" · {row.quantity_kg if row.quantity_kg is not None else '—'}kg"
            f" · 매출 {_chat_won(row.reported_sales_amount_krw)}"
            f" · 상태 {row.sale_status}"
        )

    lines += ["", "## 기간 판매 추이"]
    if trend.rows:
        for row in trend.rows:
            lines.append(
                f"- {row.sale_date}: {row.sales_count}건"
                f" · {row.quantity_kg}kg"
                f" · 매출 {_chat_won(row.sales_amount_krw)}"
                f" · 공헌이익 {_chat_won(row.contribution_profit_krw)}"
            )
    else:
        lines.append("- 기록 없음")

    lines += ["", "## 금일 전략 · 검증"]
    if proposals.rows:
        for row in proposals.rows:
            strategy = row.strategy
            strategy_text = (
                "전략 기록 없음"
                if strategy is None
                else f"{strategy.source or '—'} / LLM {strategy.llm_status or '—'}"
            )
            lines.append(
                f"- {row.scenario_type or row.scenario_id}: {row.presentation_state}"
                f" · 재무 {row.finance_verdict or '—'}"
                f" · {strategy_text}"
            )
            if row.presentation_state == "UNRESOLVED" and row.unresolved_reason_codes:
                lines.append("  - 미판정: " + ", ".join(row.unresolved_reason_codes))
    else:
        lines.append("- 후보 없음")

    return {
        "kind": "SALES",
        "sim_run_id": sim_run_id,
        "as_of": as_of.isoformat(),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "summary": dashboard.model_dump(mode="json"),
        "proposals": proposals.model_dump(mode="json"),
        "trend": trend.model_dump(mode="json"),
        "partners": partners.model_dump(mode="json"),
        "confirmed_sales": [
            row.model_dump(mode="json")
            for row in proposals.rows
            if row.sale_status in {"CONFIRMED", "DELIVERED"}
        ],
        "confirmed_count": sum(
            1 for row in proposals.rows if row.sale_status in {"CONFIRMED", "DELIVERED"}
        ),
    }


# ─── 재고·물류 chat report (deterministic, LLM 0회) ────────────────────────


def _logistics_in_scope(name: Any, items: tuple[str, ...]) -> bool:
    """이 행을 보고서 품목 칸에 싣는가. **계약 `ITEMS` 하나가 기준이다.**

    🔴 **제외 품목 이름을 여기 적지 않는다.** 피마늘·건고추는 «현재 프로젝트 범위 밖»
       이라는 업무 결정이고, 그 결정의 주인은 `contracts.core.ITEMS` 다. 여기에 이름을
       또 적으면 범위가 바뀔 때 두 곳이 갈린다.

    ★ **이름을 못 읽은 행(`None`)은 남긴다.** 이름 미상과 범위 밖은 다른 사실이다 —
      물류 화면이 지키는 원칙 그대로다.
    """
    return name is None or str(name) in items


def _logistics_qty(value: Any) -> str | None:
    """수량 한 칸. 🔴 **`None` 을 0 으로 메우지 않는다.**

    못 읽은 것과 0 kg 은 다른 사실이다. 자릿수를 잃지 않게 `Decimal` 문자열 그대로 둔다 —
    화면 `reportFormat.kg()` 가 `Number()` 로 읽는다 (Finance/Sales facts 와 같은 모양).
    """
    return None if value is None else str(value)


def _logistics_sum(values: list[Any]) -> Any:
    """수량 합. 🔴 **한 칸이라도 `None` 이면 합계도 `None` 이다.**

    아는 값만 더해 숫자를 만들면 «모르는 값이 0 이었다» 고 말하는 것과 같다.
    판매가능량 합계가 지키는 규율을 입고 실적 집계에도 그대로 쓴다.
    """
    from decimal import Decimal

    if any(value is None for value in values):
        return None
    return sum(values, start=Decimal(0))


def _logistics_arrival_display_state(expected_arrival_date: Any, as_of: Any) -> str | None:
    """도착 전 물량 한 줄의 **화면 표기용** 도착 상태. 🔴 업무 판정이 아니다.

    ```text
    None            예정일을 모른다        → 화면은 「—」
    SCHEDULED       예정일이 아직 안 왔다
    OVERDUE         예정일이 지났는데 아직 안 왔다
    ```

    🔴 **`arrival.select_due_inbound` 의 네 갈래(due · blocked · not_due · unresolved)를
       여기서 흉내 내지 않는다.** 그 판정의 주인은 물류이고, 결과는 이미
       `arrival_summary` 카드로 나간다. 그 함수를 이 목록에 다시 돌리면
       **아직 안 켜진 `purchase_id` 참조** 때문에 정상 건이 전부 「막힘」으로 찍힌다
       (`schemas.InTransitItem` · `arrival.select_due_inbound` 주석).

    ★ 그래서 여기서 보는 것은 **예정일이 지났나** 하나뿐이다. 「아직 Receipt 가 없다」는
      사실은 이 목록의 모집단 자체가 이미 보장한다 — 새 상태를 만드는 것이 아니다.
    """
    if expected_arrival_date is None:
        return None
    return "SCHEDULED" if expected_arrival_date > as_of else "OVERDUE"


def _logistics_receipt_rollup(receipts: list[Any]) -> list[dict[str, Any]]:
    """Receipt 원장을 **「입고일 + 품목」 기간 실적**으로 접는다.

    ★ **단순 합산이지 업무 판정이 아니다.** 상태를 새로 매기지 않고, 검수 결과는
      그 묶음에 실제로 있던 값들을 **그대로 나열**한다 — 섞여 있으면 하나로
      뭉뚱그리지 않는다.

    🔴 **`None` 수량을 0 으로 세지 않는다** (`_logistics_sum`).

    ★ 정렬은 최신 입고일 먼저, 같은 날은 품목 이름순이다 (지시 §27).
    """
    groups: dict[tuple[Any, str], list[Any]] = {}
    for receipt in receipts:
        key = (receipt.arrived_at, receipt.item_name or receipt.item_id)
        groups.setdefault(key, []).append(receipt)

    out: list[dict[str, Any]] = []
    for (arrived_at, item), rows_in_group in groups.items():
        verdicts = sorted({r.inspection_verdict for r in rows_in_group if r.inspection_verdict})
        out.append(
            {
                "arrived_at": arrived_at.isoformat(),
                "item": item,
                "receipt_count": len(rows_in_group),
                "ordered_qty_kg": _logistics_qty(
                    _logistics_sum([r.ordered_qty_kg for r in rows_in_group])
                ),
                "accepted_qty_kg": _logistics_qty(
                    _logistics_sum([r.accepted_qty_kg for r in rows_in_group])
                ),
                "hold_qty_kg": _logistics_qty(
                    _logistics_sum([r.hold_qty_kg for r in rows_in_group])
                ),
                "rejected_qty_kg": _logistics_qty(
                    _logistics_sum([r.rejected_qty_kg for r in rows_in_group])
                ),
                #: 그 묶음에 있던 검수 결과들. 판정을 지어내지 않는다.
                "inspection_verdicts": verdicts,
                #: 아직 검수 결과가 없는 건수. 「0건」과 「모름」을 가른다.
                "inspection_unknown_count": sum(
                    1 for r in rows_in_group if not r.inspection_verdict
                ),
                "stock_applied_count": sum(1 for r in rows_in_group if r.stock_applied),
            }
        )
    out.sort(key=lambda row: (row["arrived_at"], row["item"]), reverse=True)
    return out


def _logistics_lot_rows(lots: list[Any]) -> list[dict[str, Any]]:
    """Lot 을 **사용자 표시 순서**로 늘어놓고 표시용 순번을 붙인다.

    ★ **raw `lot_id` 를 쪼개 뜻을 캐내지 않는다.** 표시명은 `item_name` 과
      `received_at` 구조화 칸으로 화면이 만든다. 같은 품목·같은 입고일 Lot 이 여럿이면
      `lot_id` 정렬로 **안정된 순번**(`display_index`)만 여기서 매긴다.

    ★ 순서는 폐기 검토 → 우선 출고 → 신선도 잔여 적은 순 → 입고일 오래된 순이다
      (지시 §27). **표시 순서일 뿐 업무 판정이 아니다** — 값은 read model 것 그대로다.
    """
    groups: dict[tuple[Any, Any], list[Any]] = {}
    for lot in lots:
        groups.setdefault((lot.item_name or lot.item_id, lot.received_at), []).append(lot)
    seq: dict[str, tuple[int, int]] = {}
    for members in groups.values():
        ordered = sorted(members, key=lambda lot: lot.lot_id)
        for index, lot in enumerate(ordered, start=1):
            seq[lot.lot_id] = (index, len(ordered))

    def _order(lot: Any) -> tuple[Any, ...]:
        fresh = lot.remaining_freshness_days
        return (
            not lot.disposal_candidate,
            not lot.sell_priority,
            # 🔴 `None` 은 0 이 아니다 — 모르는 값을 «가장 급한 것» 으로 올리지 않는다.
            (1, 0) if fresh is None else (0, fresh),
            lot.received_at,
            lot.lot_id,
        )

    out: list[dict[str, Any]] = []
    for lot in sorted(lots, key=_order):
        index, size = seq[lot.lot_id]
        row = lot.model_dump(mode="json")
        row["display_index"] = index
        row["display_group_size"] = size
        out.append(row)
    return out


def render_logistics_chat_report(
    *,
    sim_run_id: str,
    as_of,
    start_date,
    end_date,
) -> dict[str, Any]:
    """기존 재고·물류 read model 만으로 만드는 보고서. **LLM·새 계산·새 SQL 0.**

    ```text
    기준일 재고 · 창고 사용량   get_inventory_console                        ← as_of 원장
    입고 · 검수                 get_inbound_console
    예약 · 출고                 get_outbound_console                         ← 같은 예약 한 벌
    기간 재고 추이              onhand_total_by_day + snapshot_days_between
    ```

    🔴 **여기서 업무를 새로 판정하지 않는다.** 신선도·회전·예약 상태·Receipt 상태는
       전부 read model 이 `as_of` 축에서 낸 값을 받아 적기만 한다. 보고서가 판정을
       시작하면 화면과 문서가 **다른 상태**를 말하게 된다.

    🔴 **커넥션은 한 보고서에 하나다.** `reservation_state_at` 도 한 번만 읽어 재고
       콘솔과 출고 콘솔이 나눠 쓴다 — 화면(`api/logistics/query.build_result`)과 같은
       조립 순서다.

    🔴 **창고 사용량을 표시 품목 합으로 다시 만들지 않는다.** 실제 창고 점유는 계약 밖
       품목까지 포함한 «그날 실재한 모든 Lot» 의 합이라 표시 품목 합과 다를 수 있다.
    """
    from datetime import timedelta
    from decimal import Decimal

    # 🔴 **«아직 일이 남은 예약» 판정을 여기서 다시 적지 않는다.** 그 규칙의 주인은
    #    화면이고(#675 §10), 두 벌로 적으면 한쪽만 고쳐지는 날이 온다.
    from app.api.logistics.query import _still_working
    from app.contracts.core import ITEMS
    from app.logistics.console_service import (
        get_inbound_console,
        get_inventory_console,
        get_outbound_console,
        load_console_runtime,
    )
    from app.logistics.db import get_connection
    from app.logistics.historical_repository import (
        onhand_total_by_day,
        reservation_state_at,
        snapshot_days_between,
    )

    with get_connection() as conn:
        runtime = load_console_runtime(conn=conn, sim_run_id=sim_run_id, as_of=as_of)
        reservations = reservation_state_at(conn, sim_run_id=sim_run_id, as_of=as_of)
        inventory = get_inventory_console(
            conn=conn,
            sim_run_id=sim_run_id,
            as_of=as_of,
            runtime=runtime,
            reservations=reservations,
        )
        inbound = get_inbound_console(
            conn=conn, sim_run_id=sim_run_id, as_of=as_of, runtime=runtime
        )
        outbound = get_outbound_console(
            conn=conn, sim_run_id=sim_run_id, as_of=as_of, reservations=reservations
        )
        series = onhand_total_by_day(conn, sim_run_id=sim_run_id, start=start_date, end=end_date)
        opened = snapshot_days_between(conn, sim_run_id=sim_run_id, start=start_date, end=end_date)

    items = [row for row in inventory.items if _logistics_in_scope(row.item_name, ITEMS)]
    lots = [row for row in inventory.lots if _logistics_in_scope(row.item_name, ITEMS)]
    # ★ 내부 이름 `in_transit` 은 **차량 위치 추적이 아니다.** 「입고 일정에 올라 있고 아직
    #   Receipt 가 안 선 건」 = 도착 전 물량이다 (`get_inbound_console` 머리말).
    #   `None`(그날 목록을 확인 못 했다)과 `[]`(0건 확인)은 다른 값이라 그대로 가른다.
    in_transit = (
        None
        if inbound.in_transit is None
        else [row for row in inbound.in_transit if _logistics_in_scope(row.item, ITEMS)]
    )

    # 🔴 **Receipt 는 기간 발생 내역이다 — 기준일 Snapshot 이 아니다.**
    #    `receipt_state_at` 은 그날까지 도착한 **전체 이력**을 낸다(실측 289건). 하루짜리
    #    보고서에 1월 입고가 딸려 나오던 자리라, 보고 기간 안에 도착한 것만 남긴다.
    receipts = [
        row
        for row in inbound.receipts
        if _logistics_in_scope(row.item_name, ITEMS) and start_date <= row.arrived_at <= end_date
    ]

    # 🔴 **예약은 «그날 아직 일이 남은 것» 만 본문에 싣는다.** 모집단 정의의 주인은
    #    화면(`api/logistics/query._still_working`)이고 여기서 새로 적지 않는다 —
    #    전량 출고가 끝난 과거 예약과 SHIPPED 할당 이력을 수개월치 늘어놓지 않는다.
    scoped_reservations = [
        row for row in outbound.reservations if _logistics_in_scope(row.item_name, ITEMS)
    ]
    working = [row for row in scoped_reservations if _still_working(row)]
    # ★ 납기일 빠른 순. 납기일이 없는 예약은 뒤로 둔다 (지시 §27).
    working.sort(key=lambda row: (row.due_date is None, row.due_date, row.reservation_id))
    settled_count = len(scoped_reservations) - len(working)

    # ★ 화면 표시용 단순 합계까지만 한다 — 업무 공식을 새로 만들지 않는다.
    #   🔴 판매가능량은 한 칸이라도 못 읽었으면 전체도 못 읽은 것이다. 아는 값만 더해
    #      숫자를 만들면 «모르는 값이 0 이었다» 고 말하는 것과 같다.
    total_available = _logistics_sum([row.available_qty_kg for row in items])
    total_on_hand = sum((row.on_hand_qty_kg for row in items), start=Decimal(0))
    total_unallocated = sum((row.unallocated_reserved_qty_kg for row in items), start=Decimal(0))

    # 🔴 기간 추이: **시뮬레이션이 안 연 날은 `null` 이다 — 0kg 이 아니다.**
    #    원장 누계는 어떤 날짜에도 숫자를 내고 첫 사실 이전 구간에서 그 값이 0 인데,
    #    그 0 은 «재고가 없다» 가 아니라 «그날을 모른다» 다 (`query._onhand_series` 와 같은 규칙).
    trend: list[dict[str, Any]] = []
    for offset in range((end_date - start_date).days + 1):
        day = start_date + timedelta(days=offset)
        known = day in opened and day in series
        trend.append(
            {"date": day.isoformat(), "on_hand_qty_kg": float(series[day]) if known else None}
        )

    capacity = inventory.capacity
    return {
        "kind": "LOGISTICS",
        "sim_run_id": sim_run_id,
        "as_of": as_of.isoformat(),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        #: ★ **기준일 Snapshot 인가 기간 발생 내역인가를 칸 이름으로 가른다.**
        #:   `summary` · `inventory` · `outbound` 는 `as_of` 상태이고,
        #:   `inbound.period_*` 와 `trend` 는 `start_date~end_date` 에 일어난 일이다.
        "summary": {
            "item_count": len(items),
            "total_on_hand_qty_kg": _logistics_qty(total_on_hand),
            "total_available_qty_kg": _logistics_qty(total_available),
            "total_reserved_qty_kg": _logistics_qty(
                sum((row.reserved_qty_kg for row in items), start=Decimal(0))
            ),
            "total_unallocated_reserved_qty_kg": _logistics_qty(total_unallocated),
            #: 판매가능량이 `None` 인 이유. read model 이 낸 값을 그대로 옮긴다.
            "available_qty_unresolved_reason": inventory.available_qty_unresolved_reason,
            #: 🔴 창고 Capacity 는 read model 값 그대로다 — 품목 카드 합이 아니다.
            "used_capacity_kg": _logistics_qty(capacity.used_capacity_kg),
            "guaranteed_capacity_kg": _logistics_qty(capacity.guaranteed_capacity_kg),
            "burst_capacity_kg": _logistics_qty(capacity.burst_capacity_kg),
            "capacity_basis": capacity.capacity_basis,
            "lot_count": len(lots),
            #: Lot 건수는 품목 카드가 이미 센 값을 더한 것이다 — 여기서 다시 판정하지 않는다.
            #: ⚠️ 이 read model 에서 «만료 Lot» 과 «폐기 검토 Lot» 은 **같은 모집단**이라
            #:    (`console_service`: `disposal_candidate and remaining > 0`) 한 칸만 낸다.
            "sell_priority_lot_count": sum(row.sell_priority_lot_count for row in items),
            "disposal_candidate_lot_count": sum(row.disposal_candidate_lot_count for row in items),
            "working_reservation_count": len(working),
            "settled_reservation_count": settled_count,
            #: 🔴 단순 사실 비교다 — 새 KPI 도 severity 도 아니다 (지시 §30).
            #:    `Decimal` 로 재서 문자열 비교의 오차를 남기지 않는다.
            "unallocated_exceeds_on_hand": bool(total_unallocated > total_on_hand),
            "trend_is_single_day": start_date == end_date,
        },
        "inventory": {
            "items": [row.model_dump(mode="json") for row in items],
            #: 🔴 표시 순서와 표시용 순번만 붙인 Lot. 값은 read model 것 그대로다.
            "lots": _logistics_lot_rows(lots),
            "capacity": capacity.model_dump(mode="json"),
            "available_qty_unresolved_reason": inventory.available_qty_unresolved_reason,
        },
        "inbound": {
            "arrival_summary": inbound.arrival_summary.model_dump(mode="json"),
            "in_transit_status": inbound.in_transit_status,
            #: 도착 전 물량. 내부 이름은 계약대로 `in_transit` 이고 화면 표시명만 「입고 예정」이다.
            "in_transit": (
                None
                if in_transit is None
                else [
                    dict(
                        row.model_dump(mode="json"),
                        arrival_display_state=_logistics_arrival_display_state(
                            row.expected_arrival_date, as_of
                        ),
                    )
                    for row in in_transit
                ]
            ),
            #: 보고 기간에 도착한 Receipt 를 「입고일 + 품목」으로 접은 실적. **본문용.**
            "period_receipt_rollup": _logistics_receipt_rollup(receipts),
            "period_receipt_count": len(receipts),
            #: 🔴 추적용 원장은 facts 에 **남긴다.** 화면이 안 그릴 뿐이다 (지시 §10).
            "period_receipts": [row.model_dump(mode="json") for row in receipts],
        },
        "outbound": {
            #: 🔴 «그날 아직 일이 남은» 예약만. 전량 출고가 끝난 과거 예약은 건수로만 남긴다.
            "working_reservations": [
                dict(
                    row.model_dump(mode="json"),
                    #: 할당 Lot 수 = `allocated_qty_kg` 와 **같은 모집단**(아직 안 나간 할당)이다.
                    allocation_lot_count=len(
                        {a.lot_id for a in row.allocations if a.status == "ALLOCATED"}
                    ),
                )
                for row in working
            ],
            "working_reservation_count": len(working),
            "settled_reservation_count": settled_count,
        },
        "trend": trend,
    }
