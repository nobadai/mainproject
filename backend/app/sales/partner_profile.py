"""거래처 기본정보 쓰기 — **스키마가 가진 칸만.**

★ 판매가 소유한다. 거래처는 판매가 관계를 맺는 상대이고, 그 기본정보를 고치는 일은
  판매 업무다. 실행(`sim_run_id`)과 무관한 **원장 행**이라 실행 축을 받지 않는다 —
  받으면 «A 실행의 거래처 이름» 같은 없는 개념이 생긴다.

🔴 **여신 한도를 여기서 고치지 않는다.** 그 정본은 재무의 `partner_credit_limits` 이고
   유효기간·근거등급·승인자를 가진 계약 행이다. 거래처 기본정보에 `credit_limit` 칸을
   하나 더 두면 **두 곳이 서로 다른 한도를 말하는 날**이 오고, 그때 어느 쪽으로 판정이
   났는지 아무도 답할 수 없다.

🔴 **없는 칸을 만들지 않는다.** 프로토타입이 요구한 담당자·전화·이메일은 `partners` 에
   없다. 여기서 `note` 에 밀어 넣으면 그 값은 검색도 검증도 안 되는 문자열이 된다 —
   필요하면 마이그레이션으로 칸을 내는 것이 맞고, 그것은 이 판의 일이 아니다.
"""

from typing import Any

from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field

from app.sales.db import execute_returning_one, fetch_all, get_db_schema

#: 고칠 수 있는 칸. **표에 있는 것만** 적는다.
_EDITABLE = (
    "partner_name",
    "partner_type",
    "client_type",
    "factory_region",
    "factory_city",
    "factory_area",
    "sales_collection_days",
    "pricing_contract_type",
    "active",
    "note",
)

#: 받으면 **거절**하는 이름. 조용히 무시하면 사용자는 고쳐진 줄 안다.
FOREIGN_FIELDS = {
    "credit_limit": "여신 한도는 재무 정본입니다 (partner_credit_limits).",
    "credit_limit_krw": "여신 한도는 재무 정본입니다 (partner_credit_limits).",
    "contact": "담당자 칸이 partners 에 없습니다 — 마이그레이션이 필요합니다.",
    "phone": "전화 칸이 partners 에 없습니다 — 마이그레이션이 필요합니다.",
    "email": "이메일 칸이 partners 에 없습니다 — 마이그레이션이 필요합니다.",
}


class PartnerProfileUpdate(BaseModel):
    """거래처 기본정보 수정 입력. **안 준 칸은 안 고친다.**

    ⚠️ `None` 과 «안 줬다» 를 가른다 — `extra="forbid"` 라 모르는 칸은 거절되고,
      준 칸만 `UPDATE` 에 들어간다. 전체 덮어쓰기가 아니다.
    """

    model_config = ConfigDict(extra="forbid")

    partner_name: str | None = Field(default=None, min_length=1)
    partner_type: str | None = Field(default=None, min_length=1)
    client_type: str | None = None
    factory_region: str | None = None
    factory_city: str | None = None
    factory_area: str | None = None
    sales_collection_days: int | None = Field(default=None, ge=0)
    pricing_contract_type: str | None = None
    active: bool | None = None
    note: str | None = None


class PartnerProfile(BaseModel):
    """저장된 거래처 기본정보 그대로."""

    model_config = ConfigDict(extra="forbid")

    partner_id: str
    partner_name: str
    partner_type: str
    client_type: str | None
    factory_region: str | None
    factory_city: str | None
    factory_area: str | None
    sales_collection_days: int | None
    pricing_contract_type: str | None
    active: bool
    provisional: bool
    note: str | None
    #: 🔴 여신은 여기 없다. 재무에 물어야 한다는 사실을 칸으로 말한다.
    credit_source: str = "finance:partner_credit_limits"


def get_partner_profile(*, partner_id: str) -> PartnerProfile | None:
    """거래처 한 건. 없으면 `None` 이다."""
    schema = get_db_schema()
    statement = sql.SQL(
        """
        SELECT partner_id, partner_name, partner_type, client_type, factory_region,
               factory_city, factory_area, sales_collection_days, pricing_contract_type,
               active, provisional, note
        FROM {}.partners WHERE partner_id = %s
        """
    ).format(sql.Identifier(schema))
    rows = fetch_all(statement, [partner_id])
    return None if not rows else PartnerProfile(**rows[0])


def update_partner_profile(
    *, partner_id: str, update: PartnerProfileUpdate
) -> PartnerProfile | None:
    """준 칸만 고치고 **저장된 결과를 돌려준다.**

    ★ 돌려주는 것은 입력이 아니라 `RETURNING` 이다 — 화면이 «고쳐졌다고 믿는 값» 대신
      **저장된 값**을 보게 된다. 둘이 다를 수 있고, 다를 때 알아야 한다.
    """
    changes = update.model_dump(exclude_unset=True)
    if not changes:
        return get_partner_profile(partner_id=partner_id)
    schema = get_db_schema()
    assignments = [
        sql.SQL("{} = %s").format(sql.Identifier(name))
        for name in changes
        if name in _EDITABLE
    ]
    params: list[Any] = [changes[name] for name in changes if name in _EDITABLE]
    if not assignments:
        return get_partner_profile(partner_id=partner_id)
    params.append(partner_id)
    statement = (
        sql.SQL("UPDATE {}.partners SET ").format(sql.Identifier(schema))
        + sql.SQL(", ").join(assignments)
        + sql.SQL(
            """
            WHERE partner_id = %s
            RETURNING partner_id, partner_name, partner_type, client_type,
                      factory_region, factory_city, factory_area,
                      sales_collection_days, pricing_contract_type,
                      active, provisional, note
            """
        )
    )
    try:
        row = execute_returning_one(statement, params)
    except RuntimeError:
        #  `execute_returning_one` 은 행이 없으면 예외다 — 없는 거래처라는 뜻이다.
        return None
    return PartnerProfile(**row)
