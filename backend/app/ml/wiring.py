"""마스터 등록 — **저쪽이 한 줄로 부를 수 있게.**

## 왜 이 파일이 따로 있나

등록은 마스터 저장소(`app/master/bootstrap.py`)에서 일어난다. 거기에 우리 import 와
설정을 여러 줄 적게 하면, **우리 사정이 바뀔 때마다 저쪽 파일을 고쳐야 한다.**

그래서 **한 줄만 남긴다.**

```python
# app/master/bootstrap.py
from app.ml.wiring import register_ml_agent
register_ml_agent()
```

포트 이름이 바뀌든 준비 단계가 늘든 **여기서 흡수**한다.

## 등록 전에 마스터가 「ml」을 알아야 한다

`AgentName` 이 닫힌 목록이라, 그 값이 없으면 등록·호출 둘 다 터진다. 그 2줄은
우리가 못 고치는 자리다 (`app/ml/` 밖). `app/ml/README.md` 에 적어 넘겼다.

★ **없으면 조용히 넘어간다.** `register_ml_agent()` 가 예외를 던지면 마스터 부팅이
  통째로 죽는다 — 우리 배선이 안 됐다고 저쪽 서버가 안 뜨는 것은 말이 안 된다.
  못 붙으면 `False` 를 돌려주고, 마스터는 «어댑터 미등록» 으로 다룬다
  (`ask_service._run_status` — 미등록은 오류가 아니라 *"그 부서가 오늘 안 돈다"*).
"""

from __future__ import annotations

from app.ml.adapter import AGENT_NAME, ml_port


def master_knows_us() -> bool:
    """마스터 어휘에 우리 이름이 있나. **넣는 것은 저쪽 몫이다.**"""
    try:
        from app.master.envelope import agent_allowed_modes
    except Exception:                                        # noqa: BLE001
        return False
    try:
        return "STATUS_QUERY" in agent_allowed_modes(AGENT_NAME)  # type: ignore[arg-type]
    except Exception:                                        # noqa: BLE001  KeyError 등
        return False


def register_ml_agent() -> bool:
    """마스터 레지스트리에 우리 포트를 건다. 붙었으면 `True`.

    🔴 **실패해도 예외를 안 던진다.** 부팅을 죽이지 않는다 — 우리가 안 붙은 것과
      저쪽 서버가 안 뜨는 것은 무게가 다르다.
    """
    if not master_knows_us():
        return False
    try:
        from app.master.wiring import register
    except Exception:                                        # noqa: BLE001
        return False
    try:
        register(AGENT_NAME, ml_port)                        # type: ignore[arg-type]
    except Exception:                                        # noqa: BLE001
        return False
    return True
