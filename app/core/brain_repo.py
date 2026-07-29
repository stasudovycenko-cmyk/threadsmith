"""Storage boundary for account-scoped Social Brain state."""

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.brain_safety import assert_safe_payload
from app.schemas.social_brain import (
    BrainPattern,
    BrainRecord,
    BrainSection,
)


class BrainError(Exception):
    """Base error for Social Brain storage operations."""


class BrainOwnershipError(BrainError, ValueError):
    """The requested Threads account is not owned by the user."""


class BrainNotFoundError(BrainError, LookupError):
    """The requested Brain does not exist or is outside the owner scope."""


_BRAIN_COLUMNS = """
    id,
    user_id,
    threads_account_id,
    dna,
    audience,
    goals,
    constraints,
    performance,
    version,
    created_at,
    updated_at
"""

_GET_BRAIN_SQL = text(f"""
    SELECT {_BRAIN_COLUMNS}
    FROM brains
    WHERE id = :brain_id
""")

_GET_OWNED_BRAIN_SQL = text(f"""
    SELECT {_BRAIN_COLUMNS}
    FROM brains
    WHERE id = :brain_id
      AND user_id = :uid
      AND threads_account_id = :account_id
""")

_GET_BY_ACCOUNT_SQL = text(f"""
    SELECT {_BRAIN_COLUMNS}
    FROM brains
    WHERE user_id = :uid
      AND threads_account_id = :account_id
""")

_CREATE_BRAIN_SQL = text(f"""
    INSERT INTO brains (
        user_id,
        threads_account_id,
        dna,
        audience,
        goals,
        constraints,
        performance,
        version,
        created_at,
        updated_at
    )
    SELECT
        :uid,
        :account_id,
        '{{}}'::jsonb,
        '{{}}'::jsonb,
        '{{}}'::jsonb,
        '{{}}'::jsonb,
        '{{}}'::jsonb,
        1,
        now(),
        now()
    FROM threads_accounts account
    WHERE account.id = :account_id
      AND account.user_id = :uid
    ON CONFLICT (user_id, threads_account_id) DO NOTHING
    RETURNING {_BRAIN_COLUMNS}
""")

_INCREMENT_VERSION_SQL = text(f"""
    UPDATE brains
    SET version = version + 1,
        updated_at = now()
    WHERE id = :brain_id
    RETURNING {_BRAIN_COLUMNS}
""")

_INCREMENT_OWNED_VERSION_SQL = text(f"""
    UPDATE brains
    SET version = version + 1,
        updated_at = now()
    WHERE id = :brain_id
      AND user_id = :uid
      AND threads_account_id = :account_id
    RETURNING {_BRAIN_COLUMNS}
""")

_UPDATE_SECTION_SQL = {
    section: text(f"""
        UPDATE brains
        SET {section} = CAST(:section_value AS jsonb),
            version = version + 1,
            updated_at = now()
        WHERE id = :brain_id
        RETURNING {_BRAIN_COLUMNS}
    """)
    for section in (
        "dna",
        "audience",
        "goals",
        "constraints",
        "performance",
    )
}

_UPDATE_OWNED_SECTION_SQL = {
    section: text(f"""
        UPDATE brains
        SET {section} = CAST(:section_value AS jsonb),
            version = version + 1,
            updated_at = now()
        WHERE id = :brain_id
          AND user_id = :uid
          AND threads_account_id = :account_id
        RETURNING {_BRAIN_COLUMNS}
    """)
    for section in (
        "dna",
        "audience",
        "goals",
        "constraints",
        "performance",
    )
}

_GET_PATTERNS_SQL = text("""
    SELECT
        id,
        brain_id,
        kind,
        key,
        metric,
        lift,
        samples,
        confidence,
        updated_at
    FROM brain_patterns
    WHERE brain_id = :brain_id
      AND samples >= :min_samples
      AND confidence >= :min_confidence
    ORDER BY confidence DESC, samples DESC, abs(lift) DESC, id
    LIMIT :pattern_limit
""")


def _brain_from_result(result: Any) -> BrainRecord | None:
    row = result.mappings().first()
    if row is None:
        return None
    return BrainRecord.model_validate(dict(row))


def _owner_params(
    user_id: int | None,
    account_id: int | None,
) -> dict[str, int] | None:
    if user_id is None and account_id is None:
        return None
    if user_id is None or account_id is None:
        raise ValueError(
            "user_id and account_id must be provided together"
        )
    return {"uid": user_id, "account_id": account_id}


class BrainRepo:
    """Owns all writes to the aggregate `brains` table."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_or_create(
        self,
        user_id: int,
        account_id: int,
    ) -> BrainRecord:
        params = {"uid": user_id, "account_id": account_id}
        created = _brain_from_result(
            await self.session.execute(_CREATE_BRAIN_SQL, params)
        )
        if created is not None:
            return created

        existing = await self.get_by_account(user_id, account_id)
        if existing is not None:
            return existing
        raise BrainOwnershipError(
            f"Threads account {account_id} is not owned by user "
            f"{user_id}"
        )

    async def get(
        self,
        brain_id: int,
        *,
        user_id: int | None = None,
        account_id: int | None = None,
    ) -> BrainRecord | None:
        owner = _owner_params(user_id, account_id)
        if owner is None:
            statement = _GET_BRAIN_SQL
            params = {"brain_id": brain_id}
        else:
            statement = _GET_OWNED_BRAIN_SQL
            params = {"brain_id": brain_id, **owner}
        return _brain_from_result(
            await self.session.execute(statement, params)
        )

    async def get_by_account(
        self,
        user_id: int,
        account_id: int,
    ) -> BrainRecord | None:
        result = await self.session.execute(
            _GET_BY_ACCOUNT_SQL,
            {"uid": user_id, "account_id": account_id},
        )
        return _brain_from_result(result)

    async def update_section(
        self,
        brain_id: int,
        section: BrainSection,
        value: dict[str, Any],
        *,
        user_id: int | None = None,
        account_id: int | None = None,
    ) -> BrainRecord:
        if section not in _UPDATE_SECTION_SQL:
            raise ValueError(f"unsupported Brain section: {section}")
        assert_safe_payload(value, section)
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        owner = _owner_params(user_id, account_id)
        params: dict[str, Any] = {
            "brain_id": brain_id,
            "section_value": serialized,
        }
        if owner is None:
            statement = _UPDATE_SECTION_SQL[section]
        else:
            statement = _UPDATE_OWNED_SECTION_SQL[section]
            params.update(owner)
        brain = _brain_from_result(
            await self.session.execute(statement, params)
        )
        if brain is None:
            raise BrainNotFoundError(
                f"Brain {brain_id} does not exist in the requested scope"
            )
        return brain

    async def increment_version(
        self,
        brain_id: int,
        *,
        user_id: int | None = None,
        account_id: int | None = None,
    ) -> BrainRecord:
        owner = _owner_params(user_id, account_id)
        params: dict[str, Any] = {"brain_id": brain_id}
        if owner is None:
            statement = _INCREMENT_VERSION_SQL
        else:
            statement = _INCREMENT_OWNED_VERSION_SQL
            params.update(owner)
        brain = _brain_from_result(
            await self.session.execute(statement, params)
        )
        if brain is None:
            raise BrainNotFoundError(
                f"Brain {brain_id} does not exist in the requested scope"
            )
        return brain

    async def get_patterns(
        self,
        brain_id: int,
        *,
        min_samples: int,
        min_confidence: float,
        limit: int,
    ) -> list[BrainPattern]:
        result = await self.session.execute(
            _GET_PATTERNS_SQL,
            {
                "brain_id": brain_id,
                "min_samples": min_samples,
                "min_confidence": min_confidence,
                "pattern_limit": limit,
            },
        )
        return [
            BrainPattern.model_validate(dict(row))
            for row in result.mappings().all()
        ]
