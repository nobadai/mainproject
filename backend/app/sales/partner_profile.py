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
from pydantic import BaseModel, ConfigDict, Field, model_validator

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


#: 새 거래처에 쓸 수 있는 칸. **`_EDITABLE` 에 `partner_id` 만 더한다.**
#:
#: ★ 두 목록이 갈리면 «수정은 되는데 생성은 안 되는 칸» 이 생긴다. 생성은 식별자를
#:   받아야 하므로 그 하나만 다르다.
_CREATABLE = ("partner_id", *_EDITABLE)

#: `partners_partner_type_check` 가 실제로 허용하는 값 (2026-09-14 실측).
#:
#: 🔴 **여기 없는 값을 보내면 DB 제약이 거절한다.** 그 예외는 psycopg 원문이라
#:    사용자에게 그대로 보이면 «CHECK constraint» 라는 말이 화면에 뜬다. 미리 거른다.
PARTNER_TYPES = ("CUSTOMER", "SUPPLIER", "LOGISTICS_PROVIDER", "MARKET_REFERENCE", "OTHER")


class PartnerAlreadyExists(ValueError):
    """같은 `partner_id` 가 이미 있다. **덮어쓰지 않는다.**

    ★ `INSERT … ON CONFLICT DO UPDATE` 로 조용히 덮으면 «새 거래처를 만들었다» 는
      화면이 실제로는 **남의 거래처 이름을 바꾼 것**이 된다.
    """


class PartnerProfileCreate(BaseModel):
    """새 거래처 입력. **표에 있는 칸만, 기본값은 DB 가 가진 것과 같게.**

    🔴 **`provisional` 을 받지 않는다.** 그 칸은 «이 행이 잠정 자료인가» 라는 자료
       등급이고, 화면에서 고를 값이 아니다 — DB 기본값(`false`) 그대로 둔다.

    🔴 **여신 한도 칸이 없다.** 정본은 재무의 `partner_credit_limits` 다
       (`FOREIGN_FIELDS` 가 이름으로 거절한다).
    """

    model_config = ConfigDict(extra="forbid")

    #: 사용자에게는 «내부 거래처 코드» 로 보인다. **형식을 코드가 만들지 않는다** —
    #: 저장소에 `partner_id` 생성 규칙이 없어(2026-09-14 전수 확인) 임의 형식을
    #: 지어내면 그날부터 그것이 규칙이 된다.
    partner_id: str = Field(min_length=1, max_length=64)
    partner_name: str = Field(min_length=1)
    partner_type: str = Field(min_length=1)
    client_type: str | None = None
    factory_region: str | None = None
    factory_city: str | None = None
    factory_area: str | None = None
    sales_collection_days: int | None = Field(default=None, ge=0, le=365)
    pricing_contract_type: str | None = None
    active: bool = True
    note: str | None = None

    @model_validator(mode="after")
    def partner_type_is_one_the_table_allows(self) -> "PartnerProfileCreate":
        if self.partner_type not in PARTNER_TYPES:
            raise ValueError(
                "거래처 유형은 " + " · ".join(PARTNER_TYPES) + " 중 하나여야 합니다."
            )
        return self


def create_partner_profile(*, create: PartnerProfileCreate) -> PartnerProfile:
    """거래처 한 건을 만들고 **저장된 행을 돌려준다.**

    ★ `update_partner_profile` 과 같은 규율이다 — 돌려주는 것은 입력이 아니라
      `RETURNING` 이다. 기본값(`provisional`)은 DB 가 채우므로 그 값도 함께 온다.

    🔴 **`ON CONFLICT` 를 쓰지 않는다.** 같은 코드가 이미 있으면 만들지 못한 것이고,
       그 사실이 사용자에게 가야 한다 (`PartnerAlreadyExists`).
    """
    values = create.model_dump()
    columns = [name for name in _CREATABLE if name in values]
    statement = (
        sql.SQL("INSERT INTO {}.partners (").format(sql.Identifier(get_db_schema()))
        + sql.SQL(", ").join(sql.Identifier(name) for name in columns)
        + sql.SQL(") VALUES (")
        + sql.SQL(", ").join(sql.Placeholder() for _ in columns)
        + sql.SQL(
            """
            )
            ON CONFLICT (partner_id) DO NOTHING
            RETURNING partner_id, partner_name, partner_type, client_type,
                      factory_region, factory_city, factory_area,
                      sales_collection_days, pricing_contract_type,
                      active, provisional, note
            """
        )
    )
    try:
        row = execute_returning_one(statement, [values[name] for name in columns])
    except RuntimeError as error:
        #  `DO NOTHING` 이라 충돌하면 행이 안 나온다 — 그것이 «이미 있다» 의 신호다.
        raise PartnerAlreadyExists(create.partner_id) from error
    return PartnerProfile(**row)
