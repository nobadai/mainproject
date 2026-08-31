"""``constraints.yaml`` 로더 — 도메인 임계·상수의 단일 진입점 (CLAUDE.md 규칙 7).

임계를 코드에 하드코딩하지 않는다는 규칙은 "YAML에 적어두었다"만으로는 지켜지지 않는다.
읽는 경로가 하나여야 self_check·Critic·노드가 같은 값을 본다는 게 보장된다.
"""

from pathlib import Path
from typing import Any

import yaml

CONSTRAINTS_PATH = Path(__file__).with_name("constraints.yaml")

# 최상위 섹션 목록. 하나라도 없거나 빈 껍데기면 로드 시점에 터뜨린다.
# **개별 임계 키의 존재·타당성은 검사하지 않는다** — 그 목록을 여기 적으면 YAML(단일 소스)을
# 코드가 한 번 더 베끼게 되어, 값을 추가할 때마다 두 곳을 고쳐야 한다. 오타 난 키는 그 값을
# 쓰는 노드에서 드러난다. 여기서 잡는 건 "섹션이 통째로 날아갔다" 수준의 사고다.
_REQUIRED_SECTIONS = (
    "version",
    "situation",
    "coverage_days",
    "triggers",
    "split",
    "concentration",
    "variant",
    "costs",
    "cash",
    "grade",
    "demand",
    "warehouse",
    "context",
    "market_quotes",
    "allocation",
    "shelf_life_days",
    "feedback",
    "pending",
)

#: 유일한 스칼라 섹션. 나머지는 전부 매핑이어야 한다.
_SCALAR_SECTIONS = ("version",)


def load_constraints(path: Path | None = None) -> dict[str, Any]:
    """``constraints.yaml``을 읽어 dict로 돌려준다.

    **캐시하지 않는다.** feedback이 오면 ``constraint``를 "constraints.yaml 값 위에
    덮어쓴다"(IO명세 §1). 캐시된 dict를 공유하면 한 노드의 오버라이드가 다른 노드로 새고,
    그 오염은 예외 없이 결과만 조용히 바꾼다. 파일은 80여 줄이라 매번 읽어도 무해하다.

    반환은 평범한 ``dict``다. 데이터클래스를 씌우면 YAML(단일 소스)을 코드가 한 번 더
    베끼게 되어, 값을 추가할 때마다 두 곳을 고쳐야 한다.

    미결값(``pending.inbound_lead_days`` 등)은 ``None``으로 실려 온다 — 규칙 3.
    0으로 바꾸지 않는다. NULL은 계산을 막는 장치이지 불편함이 아니다.

    검사 범위는 **섹션 단위**다 — 필수 섹션이 있고 비어 있지 않은 매핑인지까지. 개별 임계
    키까지 검사하지 않는 이유는 ``_REQUIRED_SECTIONS`` 위 주석에 적어두었다.
    """
    target = CONSTRAINTS_PATH if path is None else path
    with target.open(encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)

    if not isinstance(loaded, dict):
        # 파일이 통째로 비었거나 리스트로 시작하면 여기서 걸린다. 아래 섹션 검사가
        # ``in`` 연산으로 엉뚱하게 통과하는 걸 막는 선행 조건이다.
        raise TypeError(f"constraints must be a mapping, got {type(loaded).__name__}: {target}")

    missing = [section for section in _REQUIRED_SECTIONS if section not in loaded]
    if missing:
        raise ValueError(f"constraints.yaml is missing required sections: {missing} ({target})")

    for section in _REQUIRED_SECTIONS:
        if section in _SCALAR_SECTIONS:
            continue
        value = loaded[section]
        if not isinstance(value, dict) or not value:
            # ``pending: []`` 처럼 구조만 무너진 파일은 키 존재 검사를 그대로 통과한다.
            # 그러면 pending["inbound_lead_days"]가 노드 안에서 늦게 터진다.
            raise ValueError(
                f"constraints.yaml section {section!r} must be a non-empty mapping, "
                f"got {value!r} ({target})"
            )

    return loaded
