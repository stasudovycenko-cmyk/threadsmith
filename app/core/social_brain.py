"""Public facade for the Social Brain v1 service layer."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.brain_repo import (
    BrainError,
    BrainNotFoundError,
    BrainOwnershipError,
    BrainRepo,
)
from app.core.brain_writer import BrainWriter
from app.core.context_builder import (
    PATTERN_CONTEXT_LIMIT,
    PATTERN_MIN_CONFIDENCE,
    PATTERN_MIN_SAMPLES,
    ContextBuilder,
    estimate_text_tokens,
    estimate_tokens,
)
from app.core.feedback_loop import FeedbackLoop
from app.schemas.social_brain import BrainTask, BrainTaskContext

# Compatibility name for callers that handled the previous account error.
SocialBrainAccountError = BrainOwnershipError


async def build_account_context(
    session: AsyncSession,
    *,
    user_id: int,
    threads_account_id: int,
    task: BrainTask,
    budget_tokens: int,
) -> BrainTaskContext:
    """Build one account context through the public Brain service boundary."""
    repo = BrainRepo(session)
    writer = BrainWriter(session, repo)
    brain = await writer.apply_backfill(user_id, threads_account_id)
    return await ContextBuilder(repo).build_context(
        brain.id,
        task,
        budget_tokens,
    )

__all__ = [
    "BrainError",
    "BrainNotFoundError",
    "BrainOwnershipError",
    "BrainRepo",
    "BrainWriter",
    "ContextBuilder",
    "FeedbackLoop",
    "PATTERN_CONTEXT_LIMIT",
    "PATTERN_MIN_CONFIDENCE",
    "PATTERN_MIN_SAMPLES",
    "SocialBrainAccountError",
    "build_account_context",
    "estimate_text_tokens",
    "estimate_tokens",
]
