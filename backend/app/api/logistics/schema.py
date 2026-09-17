"""재고 · 물류 탭이 받는 모양.

소유: **물류 파트.**

★ 이 탭은 안에서 넷으로 또 나뉩니다 (한눈에 보기 / 재고·신선도 / 입고 / 출고).
  카드가 여러 장이라 **목록으로 받습니다** — 카드를 하나 더해도 화면은 안 고칩니다.

🔴 **프론트에 나가는 모양은 이것 하나다.** 조회 중간값(`Console*` read model)은
   `app/logistics/schemas.py` 에 있고 프론트까지 안 나간다 — `query.py` 가 그것을
   `Pane` · `Card` · `Stat` 로 옮겨 담아 이 탭을 만든다 (물류 문서 28).

```text
api/logistics/query.py  →  logistics/console_service.py  →  historical_repository · outbound
console_service 결과     →  query.py 가 변환              →  이 파일의 LogisticsTab
```
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.api.primitives import Note, Pane, Source


class LogisticsTab(BaseModel):
    panes: list[Pane] = Field(
        description="네 탭을 다 채워 보낸다 — summary · stock · inbound · outbound"
    )
    selected: str = Field(description="지금 보고 있는 작은 탭")
    #: 🔴 **지금은 늘 `None` 이다** (#812). 종전에는 화면 맨 위에 *"기준일 시점 값이고
    #:    공란은 0 이 아니다"* 라는 안내가 붙었는데, 기준일은 화면의 기준일 줄이 이미
    #:    말하고 «공란 ≠ 0» 은 칸이 «—» 로 직접 보여 주는 사실이라 둘 다 뺐다.
    #:
    #:    ★ **칸은 남겨 둔다.** 이 자리는 *"탭 전체에 걸리는 한 줄"* 이고, 그런 안내가
    #:      필요한 날(예: 실행 자체에 단서가 붙은 날)이 오면 여기에 싣는다. 계약을
    #:      지우면 그날 새 칸을 다시 만들어야 한다.
    principle: Note | None = Field(
        default=None, description="탭 전체에 걸리는 안내 한 줄. 없으면 None"
    )
    source: Source = Field(description="예시값인지 실제 값인지")
