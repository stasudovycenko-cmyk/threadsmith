"""Public facade for the Social Brain v1 service layer."""

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

# Compatibility name for callers that handled the previous account error.
SocialBrainAccountError = BrainOwnershipError

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
    "estimate_text_tokens",
    "estimate_tokens",
]
