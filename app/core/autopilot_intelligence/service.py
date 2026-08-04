"""Transactional application service for Autopilot Intelligence."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.autopilot_intelligence.context import DecisionContextBuilder
from app.core.autopilot_intelligence.engine import AutopilotIntelligenceEngine
from app.core.autopilot_intelligence.history import DecisionHistory
from app.core.autopilot_intelligence.models import (
    DecisionContext,
    DecisionResult,
    DecisionRun,
)
from app.core.autopilot_intelligence.repository import DecisionRepository


class DecisionBusyError(RuntimeError):
    pass


class AutopilotIntelligenceService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        context_builder: DecisionContextBuilder | None = None,
        engine: AutopilotIntelligenceEngine | None = None,
        repository: DecisionRepository | None = None,
    ):
        self.session = session
        self.context_builder = context_builder or DecisionContextBuilder(session)
        self.engine = engine or AutopilotIntelligenceEngine()
        self.repository = repository or DecisionRepository(session)
        self.history = DecisionHistory(self.repository)

    async def build_context(
        self,
        user_id: int,
        account_id: int,
    ) -> DecisionContext:
        return await self.context_builder.build(user_id, account_id)

    def evaluate(self, context: DecisionContext) -> DecisionResult:
        return self.engine.evaluate(context)

    async def evaluate_account(
        self,
        user_id: int,
        account_id: int,
    ) -> DecisionRun:
        locked = (
            await self.session.execute(text("""
                SELECT pg_try_advisory_xact_lock(
                  hashtextextended(:lock_scope, 0)
                )
            """), {
                "lock_scope": f"autopilot_intelligence:{user_id}:{account_id}"
            })
        ).scalar_one()
        if not locked:
            latest = await self.repository.latest(user_id, account_id)
            if latest is not None:
                return latest
            raise DecisionBusyError("Decision evaluation is already running")
        context = await self.build_context(user_id, account_id)
        result = self.evaluate(context)
        run = await self.repository.save(
            user_id,
            account_id,
            context.context_hash(),
            result,
        )
        await self.repository.prune_history(user_id, account_id)
        return run
