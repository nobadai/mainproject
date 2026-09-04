"""``constraints.yaml`` 로더 — 도메인 임계·상수의 단일 진입점 (CLAUDE.md 규칙 7).

임계를 코드에 하드코딩하지 않는다는 규칙은 "YAML에 적어두었다"만으로는 지켜지지 않는다.
읽는 경로가 하나여야 self_check·Critic·노드가 같은 값을 본다는 게 보장된다.
"""

from collections.abc import Mapping
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


class ThresholdNotDeclared(LookupError):
    """그 품목의 ``ci_width_threshold`` 가 선언에 없다 — **기본값으로 떨어지지 않는다.**

    🔴 **왜 예외인가.** 임계가 없다는 것은 *"0.08 이다"* 가 아니라 **"아직 안 정했다"**
      이고(규칙 3), 안 정한 임계로 낸 stable/uncertain 은 근거가 없는 판정이다.
      ``dict.get(item, 0.08)`` 한 줄이면 그날 아무 일도 안 일어난 것처럼 돌아간다 —
      **에러 없이 결과만 조용히 틀어지는** 종류다.

    ``missing_data_name`` 은 **선언 파일 경로**다. payload 경로가 아니다::

        forecast.daily                      ← 마스터가 다시 보내면 풀린다
        constraints_yaml.situation.…        ← 우리가 값을 정해야 풀린다

    ⚠️ **``constraints.`` 로 시작하지 않는다.** ``missing_data`` 의 ``constraints.*`` 는
      payload 안의 부서 제약(``constraints.finance`` · ``constraints.inventory``)이라,
      같은 접두사를 쓰면 마스터가 **재무·물류에 다시 물으러 간다.** 고칠 사람이 다르다.
    """

    def __init__(self, item: str) -> None:
        self.item = item
        self.missing_data_name = threshold_missing_data_name(item)
        super().__init__(threshold_not_declared_reason(item))


def threshold_missing_data_name(item: str) -> str:
    """``missing_data`` 에 실을 이름. **문자열을 두 곳에서 짓지 않는다.**

    어댑터가 *"이 이름이 목록에 있나"* 로 사유를 고르는데, 그쪽이 문자열을 다시 지으면
    한쪽 오타가 **조용히 일반 사유로 떨어진다** — 에러가 안 나는 종류라 아무도 모른다.
    """
    return f"constraints_yaml.situation.ci_width_threshold.{item}"


def threshold_not_declared_reason(item: str) -> str:
    """봉투 ``reasoning`` 에 나가는 사유. 사람이 읽는 자리라 내부 용어를 안 쓴다.

    ``ThresholdNotDeclared`` 의 메시지와 **같은 문장**이다 — 로그와 회신이 다른 말을
    하면 둘을 대조하는 사람이 두 사건으로 읽는다.
    """
    return f"임계가 정해지지 않아 상황을 판정할 수 없다 — {item}"


def ci_width_threshold(item: str, constraints: Mapping[str, Any]) -> float:
    """품목의 ``ci_width_threshold``. 없으면 ``ThresholdNotDeclared``.

    🔴 **읽는 자리를 하나로 모은다.** ①(판정)·어댑터(근거 문장·문 앞 검사) 셋이 같은
      값을 보는데, 각자 ``constraints["situation"]["ci_width_threshold"][item]`` 을
      적으면 *"없을 때 어떻게 하는가"* 가 세 곳에 생긴다. 한 곳만 ``.get`` 으로 바뀌면
      그 경로만 조용히 기본값을 탄다 — 규칙 7 이 임계에 대해 말하는 것과 같다.

    ★ **없는 키와 ``null`` 이 같은 자리로 온다.** 둘 다 *"아직 안 정했다"* 이고
      (규칙 3 · ``shelf_life_days`` 의 ``null`` 과 같은 뜻), 판정을 막는다는 결과도
      같다. 조건을 둘로 가르면 한쪽만 고치는 날이 온다.

    ⚠️ 스칼라로 되돌아간 선언(``ci_width_threshold: 0.08``)도 여기서 걸린다. 그때는
      **전 품목이 같이** 걸려야 맞다 — 한 품목만 통과시키면 나머지가 그 값을 물려받는
      꼴이 되고, 그건 위에서 막으려던 기본값과 같다.
    """
    declared = constraints["situation"]["ci_width_threshold"]
    if not isinstance(declared, Mapping):
        raise ThresholdNotDeclared(item)
    value = declared.get(item)
    if value is None:
        raise ThresholdNotDeclared(item)
    return value
