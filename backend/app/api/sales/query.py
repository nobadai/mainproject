"""판매 탭 — 값을 읽어오는 곳.

╔══════════════════════════════════════════════════════════════════════════╗
║  ★ 판매 파트가 채우는 파일입니다. `build()` 안쪽만 바꾸면 됩니다.          ║
║                                                                          ║
║  지금은 **예시값**입니다 (`Source.filled = False`) — 화면에 「예시값」      ║
║  딱지가 붙습니다.                                                         ║
║                                                                          ║
║  읽을 곳: sales · sale_items · receivables.                               ║
║  DB 는 `app/sales/` 것을 그대로 쓰세요.                                    ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

from datetime import date

from app.api.primitives import Card, Chart, Column, Note, Series, Source, Stat, Table
from app.api.sales.schema import SalesTab

#: 수금 예정 분포 (백만원). 데모 실측값.
_AR = [5.218, 5.258, 5.258, 5.218, 5.218, 5.218, 5.218, 4.976, 4.976,
       4.976, 4.748, 4.748, 4.626, 4.429, 4.460]


def build(as_of: date) -> SalesTab:
    return SalesTab(
        stats=[
            Stat(label="총 판매금액", value="7,305", unit="만원",
                 detail="12월 판매 15건 합계", tone="warn", raw=7305),
            Stat(label="총 판매량", value="28.2", unit="톤",
                 detail="건당 1,877kg · 총 28,155kg", tone="good", raw=28.2),
            Stat(label="공헌이익", value="1,461", unit="만원",
                 detail="매출에서 변동비를 뺀 금액 · 약 20%", tone="good", raw=1461),
            Stat(label="아직 받을 돈", value="7,305", unit="만원",
                 detail="15건 모두 미수 상태 · D+30 조건", tone="warn", raw=7305),
        ],
        read_only=Note(
            tone="info",
            text=("**이 화면은 조회 전용입니다.** 시나리오 실행 없이 저장된 판매 · 수금 "
                  "결과만 봅니다 — «얼마 팔았는지, 언제 돈을 받는지»를 바로 확인하는 데 "
                  "집중합니다."),
        ),
        cards=[
            Card(
                key="summary", title="이번 달 판매를 한눈에",
                subtitle="복잡한 원장 대신 먼저 확인할 핵심 숫자만",
                source_ref="sales · sale_items · receivables",
                stats=[
                    Stat(label="판매 처리", value="15건 모두 출고 완료",
                         detail="order_status = DELIVERED"),
                    Stat(label="수금 일정", value="2026-01-02 ~ 01-30",
                         detail="2일 간격으로 회수 예정"),
                    Stat(label="가장 큰 매출 품목", value="배추 3,770만원",
                         detail="전체 매출의 약 51.6%", tone="good"),
                ],
            ),
            Card(
                key="recent", title="최근 판매 내역", source_ref="sales",
                table=Table(
                    columns=[
                        Column(key="d", label="판매일", mono=True),
                        Column(key="no", label="판매번호", mono=True),
                        Column(key="qty", label="판매량", align="right", mono=True),
                        Column(key="amount", label="판매금액", align="right", mono=True),
                        Column(key="margin", label="공헌이익", align="right", mono=True),
                        Column(key="due", label="수금 예정일", mono=True),
                        Column(key="state", label="수금 상태"),
                    ],
                    rows=[
                        {"d": "2025-12-31", "no": "SO-20251231-01", "qty": "1,860 kg",
                         "amount": "446만원", "margin": "89만원", "due": "2026-01-30",
                         "state": "미수"},
                        {"d": "2025-12-29", "no": "SO-20251229-01", "qty": "1,848 kg",
                         "amount": "443만원", "margin": "88만원", "due": "2026-01-28",
                         "state": "미수"},
                        {"d": "2025-12-27", "no": "SO-20251227-01", "qty": "1,930 kg",
                         "amount": "463만원", "margin": "93만원", "due": "2026-01-26",
                         "state": "미수"},
                    ],
                    empty_text="이 기간에 판매가 없습니다",
                ),
            ),
            Card(
                key="ar", title="언제 돈이 들어오나",
                subtitle="수금 예정일별 금액", source_ref="receivables",
                chart=Chart(
                    label="수금 예정 분포", y_min=0, y_max=6, y_ticks=[0, 2, 4, 6],
                    y_unit="M",
                    series=[Series(name="수금 예정", data=list(_AR), tone="info")],
                    x_labels=[
                        {0: "1/02", 4: "1/10", 9: "1/20", 14: "1/30"}.get(i, "")
                        for i in range(len(_AR))
                    ],
                    note=Note(
                        tone="warn",
                        text=("15건 모두 아직 **받지 못한 돈**입니다. 12월에 실제로 들어온 "
                              "수금은 0원입니다."),
                    ),
                ),
            ),
            Card(
                key="reading", title="알기 쉬운 해석",
                bullets=[
                    "판 것은 7,305만원인데 **손에 들어온 돈은 0원**입니다 — D+30 조건이라 그렇습니다",
                    "공헌이익 1,461만원은 «매출에서 변동비를 뺀 것»이고, 고정비는 아직 안 뺐습니다",
                    "배추 한 품목이 매출의 절반(51.6%)입니다 — 한 품목에 몰려 있습니다",
                ],
            ),
        ],
        source=Source(
            filled=False, owner="판매",
            note="app/api/sales/query.py 의 build() 를 채우면 실제 값이 됩니다",
        ),
    )
