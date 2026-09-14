"""화면이 읽는 실행과 기준일. **발표용 임시 설정이다.**

`app/api/dashboard/AGENTS.md` 의 「아직 안 정한 것 — `sim_run_id`」 에서 ㉰ (설정값으로
하나 못 박는다) 를 골랐다. 화면 API 가 읽는 실행은 이 파일 한 자리에서만 정한다.

```text
지금           SIM-CHAIN-V13     2026-01-26   1~3월 중간 정본
최종 실행 뒤   SIM-CHAIN-FINAL   2026-09-20   2026-01-01~09-20 한 줄기
```

★ **최종 실행 SIM-CHAIN-FINAL 이 끝나면 아래 두 줄만 바꾼다.**

    SHOWN_SIM_RUN_ID = "SIM-CHAIN-FINAL"
    SHOWN_AS_OF = date(2026, 9, 20)

  그 전까지는 1~3월 중간 정본 V13 을 본다.

★ 프론트 기준일 `frontend/src/lib/demo_as_of.ts` 의 코드 기본값은 `SHOWN_AS_OF` 와 같은
  값이어야 한다 (`tests/api/test_shown_run.py` 가 잡는다).

★ 발표 뒤에는 주소 파라미터 방식(㉮)으로 올린다. 그때 이 파일은 지운다.

🔴 **화면에서 번인 상수(`ledger_repository.BURN_IN_SIM_RUN_ID`)를 다시 쓰지 않는다.**
   번인은 2025-12 한 달치라 발표 숫자와 다른 장부를 보여 준다.

🔴 **환경변수로 덮어쓰지 않는다.** 덮어쓸 길을 두면 값의 주인이 둘이 되어 화면과
   검사가 서로 다른 실행을 보게 된다.
"""

from __future__ import annotations

from datetime import date

SHOWN_SIM_RUN_ID = "SIM-CHAIN-V13"
SHOWN_AS_OF = date(2026, 1, 26)
