"""검토 결과를 **사람이 읽는 문장으로** 만든다 (E3-10) — 고정 템플릿.

🔴 **판단자는 문장을 안 쓴다.** 고르는 것은 finding 코드와 이미 있는 ``ref_id`` 뿐이고,
문장은 여기서 만든다. 그래야 같은 코드 집합이면 ``risks`` 가 **바이트까지 같다** —
자유 문장을 그대로 실으면 같은 판정에서도 표현이 매번 달라진다.

⚠️ **그래도 「결정성 보장」은 아니다.** 여기서 고정되는 것은 **문장과 순서**이고,
*"어떤 코드를 고르는가"* 는 여전히 판단자의 응답이다. 그 차이를 §6 이 적어 뒀다.

★ 문면 규율은 다른 출력과 같다 — **내부 용어를 안 쓴다.** 화면과 Critic 이 이 문장을
읽으므로 ``SKIPPED_BY_GATE`` 같은 단계 이름이 그대로 나가면 읽는 사람이 못 읽는다.
"""

from dataclasses import dataclass

#: 🔴 **선언 순서가 곧 정렬 순서다.** 판단자가 같은 집합을 뒤집어 돌려줘도 같은 순서로 선다.
#:
#: ``target_ref_id`` 가 **필수인 것과 아닌 것**이 갈린다 — 어떤 지적은 «이 근거가» 를
#: 가리켜야 뜻이 서고(앞 셋), 어떤 지적은 **가리킬 근거가 없다**(뒤 셋). 없는 것을
#: 필수로 두면 판단자가 아무 ``ref_id`` 나 붙이고, 그 순간 지적이 엉뚱한 근거를 문다.
FINDINGS: dict[str, "Finding"] = {}


@dataclass(frozen=True)
class Finding:
    code: str
    needs_ref: bool
    template: str


def _더한다(code: str, *, needs_ref: bool, template: str) -> None:
    FINDINGS[code] = Finding(code=code, needs_ref=needs_ref, template=template)


_더한다(
    "CLAIM_SOURCE_MISMATCH",
    needs_ref=True,
    template="근거 {ref}의 종류와 그 근거로 든 주장이 맞지 않는다 — 다시 볼 것",
)
_더한다(
    "CLAIM_STRONGER_THAN_EVIDENCE",
    needs_ref=True,
    template="근거 {ref}가 받쳐 주는 것보다 결론이 세게 쓰였다 — 다시 볼 것",
)
_더한다(
    "CONFLICTING_EVIDENCE_IGNORED",
    needs_ref=True,
    template="근거 {ref}와 어긋나는 사실이 같이 있는데 결론이 한쪽만 들었다 — 다시 볼 것",
)
_더한다(
    "MIX_REASON_LABEL_MISMATCH",
    needs_ref=False,
    template="등급 조합을 고른 사유가 그날 시세·신선도 상태와 맞지 않는다 — 다시 볼 것",
)
_더한다(
    "RISK_CATEGORY_MISSING",
    needs_ref=False,
    template="이 안의 상태에서 나와야 할 위험이 적혀 있지 않다 — 다시 볼 것",
)
_더한다(
    "LABEL_BODY_MISMATCH",
    needs_ref=False,
    template="안에 붙은 전략 이름과 실제 계획의 모양이 다르다 — 다시 볼 것",
)


class UnknownFinding(ValueError):
    """판단자가 **제시하지 않은 코드**를 골랐다 — 비율을 지어낸 것과 같다."""


def render(findings: list[tuple[str, str | None]], known_refs: set[str]) -> list[str]:
    """``(코드, ref_id)`` 목록 → 문장 목록. **중복을 걷고 선언 순서로 세운다.**

    🔴 **판단자가 돌려준 순서를 안 쓴다.** 같은 집합을 다른 순서로 돌려줘도 ``risks`` 가
    같아야 한다 — 안 그러면 같은 판정의 산출물이 호출마다 달라진다.

    🔴 **모르는 코드·모르는 근거는 거부한다.** 제시한 집합 밖의 코드는 «없는 지적» 이고,
    입력에 없던 ``ref_id`` 는 «없는 근거» 다. 둘 다 지어낸 것이라 문장으로 만들지 않는다.
    """
    본 = []
    for 항목 in findings:
        코드, ref = 항목
        if 코드 not in FINDINGS:
            raise UnknownFinding(f"모르는 검토 코드 {코드!r}")
        틀 = FINDINGS[코드]
        if 틀.needs_ref and not ref:
            raise UnknownFinding(f"{코드} 는 어느 근거인지 가리켜야 한다")
        if ref is not None and ref not in known_refs:
            raise UnknownFinding(f"{코드} 가 없는 근거 {ref!r} 를 가리킨다")
        if not 틀.needs_ref:
            ref = None
        본.append((코드, ref))

    보임 = dict.fromkeys(본)  # 순서를 지키며 중복 제거
    순서 = sorted(보임, key=lambda 항목: (list(FINDINGS).index(항목[0]), 항목[1] or ""))
    return [FINDINGS[코드].template.format(ref=ref) for 코드, ref in 순서]
