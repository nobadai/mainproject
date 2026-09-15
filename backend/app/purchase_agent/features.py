"""새 기능을 **켜고 끄는 자리** — 셋이 서로 독립이고 기본은 꺼짐.

🔴 **왜 기본이 꺼짐인가.** 셋 다 산출물의 모양을 바꾼다. 켠 채로 들어가면 정본 걷기
(``SIM-CHAIN-V13``)와 대조할 기준이 사라지고, *"이 차이가 새 기능 때문인가 다른 변경
때문인가"* 를 못 가른다. 켜는 것은 **조건이 풀린 뒤**의 별도 판단이다.

★ **끄는 것이 무엇인지가 기능마다 다르다.**

``INFORMATION_REQUESTS``
    🔴 **구조화 출력만** 끈다. 「못 판정했다」는 판정도 고지(``risks``)도 **플래그와
    무관하게 그대로 돈다.** 플래그로 고지를 없애면 미결이 조용히 사라지고, 규칙 3
    (*"0으로 채우지 않는다"*)이 출력 층에서 깨진다.

``SPLIT_ALLOCATION`` · ``SELF_REVIEW``
    LLM 호출 자체를 끈다. 꺼진 경로는 각각 규칙 기본안(균등 분할)·검토 0건이라
    산출물이 켜기 전과 **바이트가 같다** — 그것이 회귀 게이트다.

⚠️ **기존 ``PURCHASE_LLM_ENABLED`` 는 그대로 둔다.** 그쪽은 ⑤ 등급 조합의 스위치이고
기본이 **켜짐**이다. 여기 셋과 뜻도 기본값도 달라 한 칸으로 합치지 않는다.

읽는 규약은 팀 4벌과 같다 — ``PURCHASE_<KEY>`` 를 먼저 보고 없으면 ``<KEY>`` 를 본다.
"""

import os

from dotenv import load_dotenv

from app.purchase_agent.llm.runtime import ENV_FILES, ENV_PREFIX

#: 🔴 구조화 요청만 켠다 — 고지는 이 값과 무관하다 (위 머리말).
INFORMATION_REQUESTS = "INFORMATION_REQUESTS_ENABLED"
#: ④ 회차 배분 LLM. 🔴 비율이 아직 ``PROVISIONAL`` 이라 운영에서 안 켠다.
SPLIT_ALLOCATION = "LLM_SPLIT_ALLOCATION_ENABLED"
#: 근거 자기 검토 LLM.
SELF_REVIEW = "LLM_SELF_REVIEW_ENABLED"


def enabled(key: str, *, default: bool = False) -> bool:
    """``PURCHASE_<KEY>`` → ``<KEY>`` → 기본값.

    🔴 **기본이 거짓이다.** 설정을 못 읽었을 때 켜져 있으면, 못 읽은 것과 켠 것을
    구분할 수 없다 (규칙 3 과 같은 결).
    """
    for env_file in ENV_FILES:
        load_dotenv(env_file)
    value = os.getenv(f"{ENV_PREFIX}{key}") or os.getenv(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
