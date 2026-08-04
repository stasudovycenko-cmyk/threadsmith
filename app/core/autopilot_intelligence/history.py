"""Read-only history facade kept separate from evaluation."""

from app.core.autopilot_intelligence.models import DecisionRun
from app.core.autopilot_intelligence.repository import DecisionRepository


class DecisionHistory:
    def __init__(self, repository: DecisionRepository):
        self.repository = repository

    async def latest(self, user_id: int, account_id: int) -> DecisionRun | None:
        return await self.repository.latest(user_id, account_id)

    async def page(
        self,
        user_id: int,
        account_id: int,
        *,
        limit: int = 5,
        offset: int = 0,
    ) -> list[DecisionRun]:
        return await self.repository.history(
            user_id,
            account_id,
            limit=limit,
            offset=offset,
        )
