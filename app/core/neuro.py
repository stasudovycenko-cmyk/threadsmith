"""
Модуль 4 - нейрокомментинг.

Механика: краулим свежие посты ниши -> LLM решает, релевантен ли пост,
и пишет коммент голосом юзера -> премодерация в телеге или автопост.

Защита аккаунта юзера (вшито, юзером не отключается):
- один коммент на пост (unique в базе)
- один коммент автору в сутки
- суточный кэп
- без ссылок в комментах - LLM запрещено, плюс постфильтр
- LLM-фильтр релевантности: мимо темы/токсично/реклама -> скип
"""
import json
import logging
import re
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai_cost import AIUsageContext
from app.core import credits, social_brain
from app.core.config import CREDIT_COSTS
from app.core.crypto import decrypt_token
from app.core.llm import LLM_MAX_TOKENS, ask_json
from app.core.threads_api import (
    ThreadsAPIError,
    ThreadsPublishUnknownError,
    create_reply_container_once,
    get_replies,
    is_permission_error,
    publish_reply_container_once,
)
from app.schemas.engagement import (
    CommentStrategy,
    NeuroCommentV2Response,
    PublishClaim,
)
from app.schemas.llm import NeuroCommentResponse

log = logging.getLogger("neuro")

NEURO_SYSTEM_TMPL = """Ты пишешь комментарии в Threads от лица автора с этим голосом:

{profile}

Тебе дают чужой пост из ниши автора. Два шага:

1. РЕШИ, стоит ли комментировать. НЕ комментируй если пост: не по теме ниши
"{niche}", токсичный, реклама/спам, слишком личный (горе, болезнь), или
коммент будет выглядеть натянуто.

2. Если стоит - напиши ОДИН коммент. Правила:
- Голосом автора, используй sample_phrases как камертон
- Добавляй ценность: дополни мысль, дай пример, задай острый вопрос,
  культурно не согласись. НЕ пиши "супер!", "согласен", "полезно"
- 1-3 предложения, до 280 символов
- ЗАПРЕЩЕНО: ссылки, упоминания своих продуктов, "переходи ко мне в профиль"
- Коммент должен вызывать желание глянуть, кто это написал

JSON:
{{"relevant": true/false, "skip_reason": "если false - почему", "comment": "текст или null"}}"""

_LINK_RE = re.compile(r"(https?://|t\.me/|@[\w.]+|www\.)", re.I)
_EMPTY_PRAISE_RE = re.compile(
    r"(?i)^\s*(полностью согласен|согласен|отличный пост|супер|"
    r"great post|totally agree|love this)[!.\s]*$"
)

COMMENT_STRATEGIES: tuple[CommentStrategy, ...] = (
    "useful_addition",
    "personal_observation",
    "clarifying_question",
    "gentle_disagreement",
    "short_insight",
    "specific_support",
    "mini_story",
    "professional_opinion",
)

NEURO_V2_SYSTEM = """Write one natural Threads reply in the creator's voice.
Use only the compact task context, relationship memory and recent comments supplied.
Follow the requested strategy. Add concrete value; never use empty praise,
advertising calls to action, links, profile promotion, aggression or manipulation.
Maximum 280 characters. Return JSON with relevant, skip_reason, strategy, comment.
When a safe, useful reply is impossible, set relevant=false and comment=null."""


async def generate_comment(
    profile: dict,
    niche: str,
    post_text: str,
    author: str,
    *,
    usage_context: AIUsageContext | None = None,
) -> dict:
    response = await ask_json(
        NEURO_SYSTEM_TMPL.format(
            profile=json.dumps(
                profile, ensure_ascii=False, separators=(",", ":")
            ),
            niche=niche,
        ),
        f"Пост от @{author}:\n\n{post_text}",
        max_tokens=LLM_MAX_TOKENS["neuro_comment"],
        response_model=NeuroCommentResponse,
        feature="neuro_comment",
        usage_context=usage_context,
    )
    result = response.model_dump(mode="json")
    # постфильтр: LLM сказали без ссылок, но доверяй и проверяй
    comment = result.get("comment") or ""
    if result.get("relevant") and _LINK_RE.search(comment):
        result = {"relevant": False, "skip_reason": "link in comment", "comment": None}
    return result


async def today_count(
    session: AsyncSession,
    user_id: int,
    account_id: int,
) -> int:
    row = (await session.execute(text("""
        SELECT count(*) FROM neuro_comments
        WHERE user_id = :uid AND created_at::date = current_date
          AND threads_account_id = :account_id
          AND status IN ('pending', 'posted')
    """), {"uid": user_id, "account_id": account_id})).first()
    return row[0]


async def author_commented_today(
    session: AsyncSession,
    user_id: int,
    account_id: int,
    author: str,
) -> bool:
    row = (await session.execute(text("""
        SELECT 1 FROM neuro_comments
        WHERE user_id = :uid AND target_author = :a
          AND threads_account_id = :account_id
          AND created_at::date = current_date
        LIMIT 1
    """), {
        "uid": user_id,
        "account_id": account_id,
        "a": author,
    })).first()
    return row is not None


async def pick_candidates(
    session: AsyncSession,
    user_id: int,
    account_id: int,
    niche: str,
    limit: int = 5,
) -> list:
    """Свежие посты ниши из библиотеки, которые юзер ещё не комментил.
    Свои посты юзера отсекаем по username его threads-аккаунта."""
    return (await session.execute(text("""
        SELECT pl.threads_post_id, pl.author_id, pl.text
        FROM posts_library pl
        WHERE pl.niche = :niche
          AND pl.fetched_at > now() - interval '24 hours'
          AND length(trim(pl.text)) > 50
          AND pl.author_id NOT IN (
              SELECT username FROM threads_accounts WHERE user_id = :uid
          )
          AND NOT EXISTS (
              SELECT 1 FROM neuro_comments nc
              WHERE nc.user_id = :uid
                AND nc.threads_account_id = :account_id
                AND nc.target_post_id = pl.threads_post_id
          )
        ORDER BY pl.virality_score DESC
        LIMIT :lim
    """), {
        "niche": niche,
        "uid": user_id,
        "account_id": account_id,
        "lim": limit,
    })).all()


def choose_strategy(
    used_strategies: list[str],
    *,
    last_strategy: str | None = None,
) -> CommentStrategy:
    counts = {strategy: used_strategies.count(strategy) for strategy in COMMENT_STRATEGIES}
    ordered = sorted(
        COMMENT_STRATEGIES,
        key=lambda strategy: (counts[strategy], strategy == last_strategy, COMMENT_STRATEGIES.index(strategy)),
    )
    return ordered[0]


def safe_comment(comment: str, recent_comments: list[str] | None = None) -> bool:
    normalized = " ".join(comment.split())
    if not normalized or len(normalized) > 280:
        return False
    if _LINK_RE.search(normalized) or _EMPTY_PRAISE_RE.match(normalized):
        return False
    folded = normalized.casefold()
    return all(" ".join(value.split()).casefold() != folded for value in (recent_comments or []))


async def claim_candidate_for_generation(
    session: AsyncSession,
    *,
    user_id: int,
    account_id: int,
) -> dict | None:
    """Atomically reserve one scored candidate before any LLM work."""
    row = (
        await session.execute(text("""
            WITH selected AS (
              SELECT candidate.*, memory.last_strategy
              FROM radar_candidates candidate
              JOIN neuro_settings setting
                ON setting.threads_account_id = candidate.threads_account_id
               AND setting.user_id = candidate.user_id
              LEFT JOIN neuro_author_memory memory
                ON memory.threads_account_id = candidate.threads_account_id
               AND memory.author_key = candidate.author_key
              WHERE candidate.user_id = :user_id
                AND candidate.threads_account_id = :account_id
                AND candidate.status = 'ready'
                AND setting.active
                AND candidate.final_score >= setting.minimum_score
                AND (
                  setting.mode = 'approve'
                  OR (
                    (SELECT coalesce(sum(
                       CASE WHEN today.status IN (
                         'publishing', 'posted', 'unknown'
                       ) AND coalesce(
                         today.posted_at,
                         today.publish_claimed_at,
                         today.created_at
                       )::date = current_date THEN 1 ELSE 0 END
                       + CASE WHEN today.follow_up_status IN (
                         'publishing', 'posted', 'unknown'
                       ) AND coalesce(
                         today.follow_up_claimed_at,
                         today.replied_at,
                         today.created_at
                       )::date = current_date THEN 1 ELSE 0 END
                     ), 0) FROM neuro_comments today
                     WHERE today.user_id = :user_id
                       AND today.threads_account_id = :account_id
                    ) < setting.daily_cap
                    AND NOT EXISTS (
                      SELECT 1 FROM neuro_comments recent
                      WHERE recent.user_id = :user_id
                        AND recent.threads_account_id = :account_id
                        AND recent.status IN (
                          'publishing', 'posted', 'unknown'
                        )
                        AND greatest(
                          coalesce(
                            recent.posted_at,
                            recent.publish_claimed_at,
                            recent.created_at
                          ),
                          coalesce(
                            recent.follow_up_claimed_at,
                            '-infinity'::timestamptz
                          )
                        ) > now() - make_interval(
                          mins => setting.minimum_interval_minutes
                        )
                    )
                  )
                )
                AND (
                  memory.cooldown_until IS NULL
                  OR memory.cooldown_until <= now()
                )
                AND NOT EXISTS (
                  SELECT 1 FROM neuro_comments comment
                  WHERE comment.threads_account_id = candidate.threads_account_id
                    AND comment.target_post_id = candidate.threads_post_id
                )
                AND NOT EXISTS (
                  SELECT 1 FROM neuro_comments account_queue
                  WHERE account_queue.user_id = :user_id
                    AND account_queue.threads_account_id = :account_id
                    AND account_queue.status IN (
                      'generating', 'pending', 'publishing', 'unknown'
                    )
                )
              ORDER BY candidate.final_score DESC, candidate.discovered_at DESC
              FOR UPDATE OF candidate SKIP LOCKED
              LIMIT 1
            ), inserted AS (
              INSERT INTO neuro_comments (
                user_id, threads_account_id, radar_candidate_id,
                target_post_id, target_author, target_author_id,
                author_key, target_text, comment_text, status,
                score, score_reason, generation_claimed_at
              )
              SELECT user_id, threads_account_id, id,
                     threads_post_id, author_username, author_threads_id,
                     author_key, left(post_text, 2000), '', 'generating',
                     final_score, score_reason, now()
              FROM selected
              ON CONFLICT DO NOTHING
              RETURNING id, radar_candidate_id
            )
            UPDATE radar_candidates candidate
            SET status = 'generating', updated_at = now()
            FROM inserted, selected
            WHERE candidate.id = inserted.radar_candidate_id
              AND candidate.id = selected.id
            RETURNING inserted.id AS comment_id,
                      candidate.id AS candidate_id,
                      candidate.post_text,
                      candidate.author_key,
                      candidate.author_username,
                      candidate.final_score,
                      candidate.score_reason,
                      selected.last_strategy
        """), {"user_id": user_id, "account_id": account_id})
    ).mappings().first()
    return dict(row) if row else None


async def claim_variant(
    session: AsyncSession,
    *,
    user_id: int,
    account_id: int,
    comment_id: int,
) -> bool:
    row = (
        await session.execute(text("""
            UPDATE neuro_comments
            SET status = 'generating', generation_variant = generation_variant + 1,
                generation_claimed_at = now()
            WHERE id = :comment_id AND user_id = :user_id
              AND threads_account_id = :account_id AND status = 'pending'
            RETURNING id
        """), {
            "comment_id": comment_id, "user_id": user_id,
            "account_id": account_id,
        })
    ).first()
    return row is not None


async def generate_claimed_comment(
    session: AsyncSession,
    *,
    user_id: int,
    account_id: int,
    comment_id: int,
    requested_strategy: CommentStrategy | None = None,
) -> dict | None:
    row = (
        await session.execute(text("""
            SELECT comment.target_text, comment.target_author,
                   comment.author_key, comment.generation_variant,
                   memory.last_strategy,
                   coalesce(memory.discovered_count, 0),
                   coalesce(memory.comments_posted, 0),
                   memory.author_replied, memory.last_interaction_at
            FROM neuro_comments comment
            LEFT JOIN neuro_author_memory memory
              ON memory.threads_account_id = comment.threads_account_id
             AND memory.author_key = comment.author_key
            WHERE comment.id = :comment_id AND comment.user_id = :user_id
              AND comment.threads_account_id = :account_id
              AND comment.status = 'generating'
        """), {
            "comment_id": comment_id, "user_id": user_id,
            "account_id": account_id,
        })
    ).first()
    if row is None:
        return None
    (post_text, author, author_key, variant, last_strategy, discovered_count,
     comments_posted, author_replied, last_interaction_at) = row
    recent_rows = (
        await session.execute(text("""
            SELECT comment_text, strategy
            FROM neuro_comments
            WHERE user_id = :user_id AND threads_account_id = :account_id
              AND id <> :comment_id AND comment_text <> ''
            ORDER BY created_at DESC LIMIT 12
        """), {
            "user_id": user_id, "account_id": account_id,
            "comment_id": comment_id,
        })
    ).all()
    recent_comments = [str(value[0]) for value in recent_rows]
    strategy = requested_strategy or choose_strategy(
        [str(value[1]) for value in recent_rows if value[1]],
        last_strategy=last_strategy,
    )
    context = await social_brain.build_account_context(
        session,
        user_id=user_id,
        threads_account_id=account_id,
        task="neuro",
        budget_tokens=650,
    )
    feature = "neuro_comment" if int(variant) == 0 else "neuro_variant"
    try:
        await credits.spend_once(
            session,
            user_id,
            account_id,
            CREDIT_COSTS[feature],
            feature,
            f"neuro-comment:{comment_id}:variant:{variant}",
        )
        await session.commit()
        response = await ask_json(
            NEURO_V2_SYSTEM,
            json.dumps({
                "account_context": context.compact_dict(),
                "relationship": {
                    "author": author,
                    "author_key": author_key,
                    "times_discovered": discovered_count,
                    "comments_posted": comments_posted,
                    "author_replied": bool(author_replied),
                    "last_interaction_at": (
                        last_interaction_at.isoformat() if last_interaction_at else None
                    ),
                    "last_strategy": last_strategy,
                },
                "requested_strategy": strategy,
                "recent_comments": recent_comments[:8],
                "post": post_text,
            }, ensure_ascii=False, separators=(",", ":")),
            max_tokens=LLM_MAX_TOKENS["neuro_comment"],
            response_model=NeuroCommentV2Response,
            feature="neuro_comment",
            usage_context=AIUsageContext(
                user_id=user_id,
                threads_account_id=account_id,
                run_id=f"neuro:{uuid.uuid4().hex}:{user_id}:{account_id}",
            ),
        )
        generated = (response.comment or "").strip()
        valid = response.relevant and not response.skip_reason and safe_comment(
            generated, recent_comments
        )
        if not valid:
            await session.execute(text("""
                UPDATE neuro_comments
                SET status = 'skipped', publish_error_code = 'UNSUITABLE_COMMENT',
                    strategy = :strategy, generation_claimed_at = NULL
                WHERE id = :comment_id AND user_id = :user_id
                  AND threads_account_id = :account_id AND status = 'generating'
            """), {
                "strategy": strategy, "comment_id": comment_id,
                "user_id": user_id, "account_id": account_id,
            })
            await session.execute(text("""
                UPDATE radar_candidates SET status = 'filtered', updated_at = now()
                WHERE id = (
                  SELECT radar_candidate_id FROM neuro_comments WHERE id = :comment_id
                ) AND user_id = :user_id AND threads_account_id = :account_id
            """), {
                "comment_id": comment_id, "user_id": user_id,
                "account_id": account_id,
            })
            return None
        await session.execute(text("""
            UPDATE neuro_comments
            SET comment_text = :comment, strategy = :strategy,
                status = 'pending', publish_error_code = NULL,
                generation_claimed_at = NULL
            WHERE id = :comment_id AND user_id = :user_id
              AND threads_account_id = :account_id AND status = 'generating'
        """), {
            "comment": generated, "strategy": strategy,
            "comment_id": comment_id, "user_id": user_id,
            "account_id": account_id,
        })
        await session.execute(text("""
            UPDATE radar_candidates SET status = 'pending', updated_at = now()
            WHERE id = (
              SELECT radar_candidate_id FROM neuro_comments WHERE id = :comment_id
            ) AND user_id = :user_id AND threads_account_id = :account_id
        """), {
            "comment_id": comment_id, "user_id": user_id,
            "account_id": account_id,
        })
        return {"comment": generated, "strategy": strategy}
    except credits.NotEnoughCredits:
        error_code = "NOT_ENOUGH_CREDITS"
    except Exception as error:
        log.warning(
            "neuro generation failed comment=%s account=%s error_type=%s",
            comment_id, account_id, type(error).__name__,
        )
        error_code = "GENERATION_FAILED"
    await session.execute(text("""
        UPDATE neuro_comments SET status = 'failed', publish_error_code = :code,
          generation_claimed_at = NULL
        WHERE id = :comment_id AND user_id = :user_id
          AND threads_account_id = :account_id AND status = 'generating'
    """), {
        "code": error_code, "comment_id": comment_id,
        "user_id": user_id, "account_id": account_id,
    })
    await session.execute(text("""
        UPDATE radar_candidates SET status = 'score_blocked', updated_at = now()
        WHERE id = (
          SELECT radar_candidate_id FROM neuro_comments WHERE id = :comment_id
        ) AND user_id = :user_id AND threads_account_id = :account_id
    """), {
        "comment_id": comment_id, "user_id": user_id,
        "account_id": account_id,
    })
    return None


async def claim_comment_for_publish(
    session: AsyncSession,
    *,
    user_id: int,
    account_id: int,
    comment_id: int,
    require_auto: bool,
) -> PublishClaim | None:
    claim_token = str(uuid.uuid4())
    row = (
        await session.execute(text("""
            WITH locked_setting AS (
              SELECT setting.*
              FROM neuro_settings setting
              WHERE setting.user_id = :user_id
                AND setting.threads_account_id = :account_id
              FOR UPDATE
            ), eligible AS (
              SELECT comment.id
              FROM neuro_comments comment
              JOIN locked_setting setting
                ON setting.threads_account_id = comment.threads_account_id
               AND setting.user_id = comment.user_id
              JOIN threads_accounts account
                ON account.id = comment.threads_account_id
               AND account.user_id = comment.user_id
               AND account.connection_status = 'connected'
               AND account.access_token_enc IS NOT NULL
               AND account.expires_at > now()
              WHERE comment.id = :comment_id
                AND comment.user_id = :user_id
                AND comment.threads_account_id = :account_id
                AND comment.status = 'pending'
                AND comment.score >= setting.minimum_score
                AND (
                  NOT :require_auto
                  OR (setting.active AND setting.mode = 'auto')
                )
                AND (
                  SELECT coalesce(sum(
                    CASE WHEN today.status IN (
                      'publishing', 'posted', 'unknown'
                    ) AND coalesce(
                      today.posted_at,
                      today.publish_claimed_at,
                      today.created_at
                    )::date = current_date THEN 1 ELSE 0 END
                    + CASE WHEN today.follow_up_status IN (
                      'publishing', 'posted', 'unknown'
                    ) AND coalesce(
                      today.follow_up_claimed_at,
                      today.replied_at,
                      today.created_at
                    )::date = current_date THEN 1 ELSE 0 END
                  ), 0) FROM neuro_comments today
                  WHERE today.user_id = :user_id
                    AND today.threads_account_id = :account_id
                ) < setting.daily_cap
                AND NOT EXISTS (
                  SELECT 1 FROM neuro_comments recent
                  WHERE recent.user_id = :user_id
                    AND recent.threads_account_id = :account_id
                    AND recent.status IN ('publishing', 'posted', 'unknown')
                    AND greatest(
                      coalesce(
                        recent.posted_at,
                        recent.publish_claimed_at,
                        recent.created_at
                      ),
                      coalesce(
                        recent.follow_up_claimed_at,
                        '-infinity'::timestamptz
                      )
                    ) > now() - make_interval(mins => setting.minimum_interval_minutes)
                )
            )
            UPDATE neuro_comments comment
            SET status = 'publishing', publish_claim_token = cast(:claim_token as uuid),
                publish_claimed_at = now(), publish_attempts = publish_attempts + 1,
                publish_error_code = NULL
            FROM eligible, threads_accounts account
            WHERE comment.id = eligible.id
              AND account.id = comment.threads_account_id
              AND account.user_id = comment.user_id
            RETURNING comment.id, comment.user_id, comment.threads_account_id,
                      comment.target_post_id, comment.comment_text,
                      account.threads_user_id, account.access_token_enc,
                      comment.author_key, comment.target_author
        """), {
            "user_id": user_id, "account_id": account_id,
            "comment_id": comment_id, "require_auto": require_auto,
            "claim_token": claim_token,
        })
    ).first()
    if row is None:
        return None
    return PublishClaim(
        comment_id=row[0], claim_token=claim_token, user_id=row[1],
        threads_account_id=row[2], target_post_id=row[3],
        comment_text=row[4], threads_user_id=row[5],
        access_token_enc=bytes(row[6]), author_key=row[7],
        author_username=row[8],
    )


async def publish_claimed_comment(
    session: AsyncSession,
    claim: PublishClaim,
) -> str:
    """Execute a durable publish claim once and persist a known/unknown result."""
    token = decrypt_token(claim.access_token_enc)
    try:
        container_id = await create_reply_container_once(
            token, claim.threads_user_id, claim.comment_text,
            claim.target_post_id,
        )
        await session.execute(text("""
            UPDATE neuro_comments SET provider_container_id = :container_id
            WHERE id = :comment_id AND publish_claim_token = cast(:claim_token as uuid)
              AND status = 'publishing'
        """), {
            "container_id": container_id, "comment_id": claim.comment_id,
            "claim_token": claim.claim_token,
        })
        await session.commit()
        published_id = await publish_reply_container_once(
            token, claim.threads_user_id, container_id
        )
    except ThreadsPublishUnknownError:
        await _finish_publish(
            session, claim, status="unknown", error_code="PUBLISH_RESULT_UNKNOWN"
        )
        return "unknown"
    except ThreadsAPIError as error:
        status = "permission_denied" if is_permission_error(error) else "failed"
        code = "PERMISSION_DENIED" if is_permission_error(error) else "THREADS_API_ERROR"
        await _finish_publish(session, claim, status=status, error_code=code)
        return status
    except Exception as error:
        log.warning(
            "neuro publish failed comment=%s account=%s error_type=%s",
            claim.comment_id, claim.threads_account_id, type(error).__name__,
        )
        await _finish_publish(
            session, claim, status="failed", error_code="PUBLISH_FAILED"
        )
        return "failed"

    updated = (
        await session.execute(text("""
            UPDATE neuro_comments
            SET status = 'posted', posted_at = now(),
                published_threads_id = :published_id,
                publish_error_code = NULL, reply_poll_status = 'pending'
            WHERE id = :comment_id
              AND publish_claim_token = cast(:claim_token as uuid)
              AND status = 'publishing'
            RETURNING radar_candidate_id, strategy
        """), {
            "published_id": published_id, "comment_id": claim.comment_id,
            "claim_token": claim.claim_token,
        })
    ).first()
    if updated:
        candidate_id, strategy = updated
        await session.execute(text("""
            UPDATE radar_candidates SET status = 'commented', updated_at = now()
            WHERE id = :candidate_id AND user_id = :user_id
              AND threads_account_id = :account_id
        """), {
            "candidate_id": candidate_id, "user_id": claim.user_id,
            "account_id": claim.threads_account_id,
        })
        await session.execute(text("""
            INSERT INTO neuro_author_memory (
              user_id, threads_account_id, author_key,
              author_username, comments_posted, last_neuro_comment_id,
              last_strategy, last_interaction_at, cooldown_until
            ) VALUES (
              :user_id, :account_id, :author_key,
              :author_username, 1, :comment_id,
              :strategy, now(), now() + interval '24 hours'
            )
            ON CONFLICT (threads_account_id, author_key) DO UPDATE SET
              comments_posted = neuro_author_memory.comments_posted + 1,
              last_neuro_comment_id = excluded.last_neuro_comment_id,
              last_strategy = excluded.last_strategy,
              last_interaction_at = now(),
              cooldown_until = now() + interval '24 hours',
              updated_at = now()
        """), {
            "user_id": claim.user_id, "account_id": claim.threads_account_id,
            "author_key": claim.author_key,
            "author_username": claim.author_username,
            "comment_id": claim.comment_id, "strategy": strategy,
        })
    await session.commit()
    return "posted"


async def _finish_publish(
    session: AsyncSession,
    claim: PublishClaim,
    *,
    status: str,
    error_code: str,
) -> None:
    await session.execute(text("""
        UPDATE neuro_comments
        SET status = :status, publish_error_code = :error_code
        WHERE id = :comment_id
          AND publish_claim_token = cast(:claim_token as uuid)
          AND status = 'publishing'
    """), {
        "status": status, "error_code": error_code,
        "comment_id": claim.comment_id, "claim_token": claim.claim_token,
    })
    if status == "permission_denied":
        await session.execute(text("""
            UPDATE neuro_settings SET active = false
            WHERE user_id = :user_id AND threads_account_id = :account_id
        """), {
            "user_id": claim.user_id,
            "account_id": claim.threads_account_id,
        })
    await session.commit()


async def reject_comment(
    session: AsyncSession,
    *,
    user_id: int,
    account_id: int,
    comment_id: int,
) -> bool:
    row = (
        await session.execute(text("""
            UPDATE neuro_comments SET status = 'rejected'
            WHERE id = :comment_id AND user_id = :user_id
              AND threads_account_id = :account_id AND status = 'pending'
            RETURNING radar_candidate_id
        """), {
            "comment_id": comment_id, "user_id": user_id,
            "account_id": account_id,
        })
    ).first()
    if row:
        await session.execute(text("""
            UPDATE radar_candidates SET status = 'rejected', updated_at = now()
            WHERE id = :candidate_id AND user_id = :user_id
              AND threads_account_id = :account_id
        """), {
            "candidate_id": row[0], "user_id": user_id,
            "account_id": account_id,
        })
    return row is not None


async def exclude_comment_author(
    session: AsyncSession,
    *,
    user_id: int,
    account_id: int,
    comment_id: int,
) -> bool:
    row = (
        await session.execute(text("""
            SELECT author_key, target_author FROM neuro_comments
            WHERE id = :comment_id AND user_id = :user_id
              AND threads_account_id = :account_id
        """), {
            "comment_id": comment_id, "user_id": user_id,
            "account_id": account_id,
        })
    ).first()
    if not row:
        return False
    author_value = str(row[1] or row[0]).casefold()
    await session.execute(text("""
        UPDATE neuro_settings
        SET excluded_authors = ARRAY(
          SELECT DISTINCT value FROM unnest(excluded_authors || ARRAY[:author]) value
        )
        WHERE user_id = :user_id AND threads_account_id = :account_id
    """), {
        "author": author_value, "user_id": user_id,
        "account_id": account_id,
    })
    await reject_comment(
        session, user_id=user_id, account_id=account_id, comment_id=comment_id
    )
    return True


async def poll_account_replies(
    session: AsyncSession,
    *,
    user_id: int,
    account_id: int,
    token: str,
    own_username: str | None,
    limit: int = 5,
) -> list[dict]:
    rows = (
        await session.execute(text("""
            SELECT id, published_threads_id, author_key
            FROM neuro_comments
            WHERE user_id = :user_id AND threads_account_id = :account_id
              AND status = 'posted' AND published_threads_id IS NOT NULL
              AND author_replied = false
              AND reply_poll_status <> 'permission_denied'
              AND (reply_checked_at IS NULL
                   OR reply_checked_at < now() - interval '30 minutes')
            ORDER BY posted_at DESC LIMIT :limit
        """), {
            "user_id": user_id, "account_id": account_id, "limit": limit,
        })
    ).all()
    found: list[dict] = []
    for comment_id, published_id, author_key in rows:
        try:
            replies = await get_replies(token, published_id)
        except ThreadsAPIError as error:
            if is_permission_error(error):
                await session.execute(text("""
                    UPDATE neuro_comments
                    SET reply_poll_status = 'permission_denied',
                        reply_checked_at = now()
                    WHERE id = :comment_id AND user_id = :user_id
                      AND threads_account_id = :account_id
                """), {
                    "comment_id": comment_id, "user_id": user_id,
                    "account_id": account_id,
                })
                break
            continue
        external = next(
            (
                reply for reply in replies
                if not own_username
                or str(reply.get("username") or "").casefold() != own_username.casefold()
            ),
            None,
        )
        if external:
            await session.execute(text("""
                UPDATE neuro_comments
                SET author_replied = true, reply_poll_status = 'replied',
                    reply_checked_at = now(), reply_threads_id = :reply_id,
                    reply_text = :reply_text, replied_at = now()
                WHERE id = :comment_id AND user_id = :user_id
                  AND threads_account_id = :account_id
            """), {
                "reply_id": external.get("id"),
                "reply_text": str(external.get("text") or "")[:1000],
                "comment_id": comment_id, "user_id": user_id,
                "account_id": account_id,
            })
            await session.execute(text("""
                UPDATE neuro_author_memory
                SET author_replied = true, last_interaction_at = now(), updated_at = now()
                WHERE user_id = :user_id AND threads_account_id = :account_id
                  AND author_key = :author_key
            """), {
                "user_id": user_id, "account_id": account_id,
                "author_key": author_key,
            })
            found.append({
                "comment_id": comment_id,
                "reply_id": external.get("id"),
                "reply_text": str(external.get("text") or ""),
                "username": external.get("username"),
            })
        else:
            await session.execute(text("""
                UPDATE neuro_comments
                SET reply_poll_status = 'checked', reply_checked_at = now()
                WHERE id = :comment_id AND user_id = :user_id
                  AND threads_account_id = :account_id
            """), {
                "comment_id": comment_id, "user_id": user_id,
                "account_id": account_id,
            })
    return found


async def generate_follow_up(
    session: AsyncSession,
    *,
    user_id: int,
    account_id: int,
    comment_id: int,
) -> str | None:
    row = (
        await session.execute(text("""
            UPDATE neuro_comments
            SET follow_up_status = 'generating'
            WHERE id = :comment_id AND user_id = :user_id
              AND threads_account_id = :account_id
              AND status = 'posted' AND author_replied
              AND reply_threads_id IS NOT NULL
              AND follow_up_count = 0
              AND follow_up_status IS NULL
            RETURNING target_author, target_text, comment_text, reply_text
        """), {
            "comment_id": comment_id, "user_id": user_id,
            "account_id": account_id,
        })
    ).first()
    if not row:
        return None
    author, target_text, previous_comment, reply_text = row
    context = await social_brain.build_account_context(
        session,
        user_id=user_id,
        threads_account_id=account_id,
        task="neuro",
        budget_tokens=500,
    )
    try:
        await credits.spend_once(
            session,
            user_id,
            account_id,
            CREDIT_COSTS["neuro_variant"],
            "neuro_variant",
            f"neuro-follow-up:{comment_id}:v1",
        )
        await session.commit()
        response = await ask_json(
            NEURO_V2_SYSTEM,
            json.dumps({
                "account_context": context.compact_dict(),
                "requested_strategy": "short_insight",
                "conversation": {
                    "original_author": author,
                    "post": target_text,
                    "our_comment": previous_comment,
                    "their_reply": reply_text,
                },
                "instruction": "Reply directly to their latest reply; do not restart the topic.",
            }, ensure_ascii=False, separators=(",", ":")),
            max_tokens=LLM_MAX_TOKENS["neuro_comment"],
            response_model=NeuroCommentV2Response,
            feature="neuro_comment",
            usage_context=AIUsageContext(
                user_id=user_id,
                threads_account_id=account_id,
                run_id=f"neuro-follow:{uuid.uuid4().hex}:{user_id}:{account_id}",
            ),
        )
        follow_up = (response.comment or "").strip()
        if not response.relevant or not safe_comment(follow_up, [previous_comment]):
            raise ValueError("unsafe follow-up")
        await session.execute(text("""
            UPDATE neuro_comments
            SET follow_up_text = :follow_up, follow_up_status = 'pending'
            WHERE id = :comment_id AND user_id = :user_id
              AND threads_account_id = :account_id
              AND follow_up_status = 'generating' AND follow_up_count = 0
        """), {
            "follow_up": follow_up, "comment_id": comment_id,
            "user_id": user_id, "account_id": account_id,
        })
        return follow_up
    except Exception as error:
        log.warning(
            "follow-up generation failed comment=%s account=%s error_type=%s",
            comment_id, account_id, type(error).__name__,
        )
        await session.execute(text("""
            UPDATE neuro_comments SET follow_up_status = 'failed'
            WHERE id = :comment_id AND user_id = :user_id
              AND threads_account_id = :account_id
              AND follow_up_status = 'generating'
        """), {
            "comment_id": comment_id, "user_id": user_id,
            "account_id": account_id,
        })
        return None


async def claim_follow_up_publish(
    session: AsyncSession,
    *,
    user_id: int,
    account_id: int,
    comment_id: int,
    require_auto: bool = False,
) -> dict | None:
    row = (
        await session.execute(text("""
            WITH locked_setting AS (
              SELECT setting.* FROM neuro_settings setting
              WHERE setting.user_id = :user_id
                AND setting.threads_account_id = :account_id
              FOR UPDATE
            )
            UPDATE neuro_comments comment
            SET follow_up_status = 'publishing', follow_up_claimed_at = now(),
                follow_up_error_code = NULL
            FROM threads_accounts account, locked_setting setting
            WHERE comment.id = :comment_id AND comment.user_id = :user_id
              AND comment.threads_account_id = :account_id
              AND comment.status = 'posted'
              AND comment.follow_up_status = 'pending'
              AND comment.follow_up_count = 0
              AND comment.reply_threads_id IS NOT NULL
              AND account.id = comment.threads_account_id
              AND account.user_id = comment.user_id
              AND account.connection_status = 'connected'
              AND account.access_token_enc IS NOT NULL
              AND account.expires_at > now()
              AND (
                NOT :require_auto
                OR (setting.active AND setting.mode = 'auto'
                    AND setting.auto_follow_up)
              )
              AND (
                SELECT coalesce(sum(
                  CASE WHEN existing.status IN (
                    'publishing', 'posted', 'unknown'
                  ) AND coalesce(
                    existing.posted_at,
                    existing.publish_claimed_at,
                    existing.created_at
                  )::date = current_date THEN 1 ELSE 0 END
                  + CASE WHEN existing.follow_up_status IN (
                    'publishing', 'posted', 'unknown'
                  ) AND coalesce(
                    existing.follow_up_claimed_at,
                    existing.replied_at,
                    existing.created_at
                  )::date = current_date THEN 1 ELSE 0 END
                ), 0)
                FROM neuro_comments existing
                WHERE existing.user_id = :user_id
                  AND existing.threads_account_id = :account_id
              ) < setting.daily_cap
              AND NOT EXISTS (
                SELECT 1 FROM neuro_comments recent
                WHERE recent.user_id = :user_id
                  AND recent.threads_account_id = :account_id
                  AND recent.status IN ('publishing', 'posted', 'unknown')
                  AND greatest(
                    coalesce(
                      recent.posted_at,
                      recent.publish_claimed_at,
                      recent.created_at
                    ),
                    coalesce(
                      recent.follow_up_claimed_at,
                      '-infinity'::timestamptz
                    )
                  ) > now() - make_interval(
                    mins => setting.minimum_interval_minutes
                  )
              )
            RETURNING comment.id, comment.reply_threads_id,
                      comment.follow_up_text, account.threads_user_id,
                      account.access_token_enc
        """), {
            "comment_id": comment_id, "user_id": user_id,
            "account_id": account_id, "require_auto": require_auto,
        })
    ).first()
    if not row:
        return None
    return {
        "comment_id": row[0], "reply_to_id": row[1], "text": row[2],
        "threads_user_id": row[3], "access_token_enc": bytes(row[4]),
        "user_id": user_id, "account_id": account_id,
    }


async def publish_follow_up(session: AsyncSession, claim: dict) -> str:
    token = decrypt_token(claim["access_token_enc"])
    published_id = None
    error_code = None
    try:
        container_id = await create_reply_container_once(
            token,
            claim["threads_user_id"],
            claim["text"],
            claim["reply_to_id"],
        )
        await session.execute(text("""
            UPDATE neuro_comments SET follow_up_container_id = :container_id
            WHERE id = :comment_id AND user_id = :user_id
              AND threads_account_id = :account_id
              AND follow_up_status = 'publishing' AND follow_up_count = 0
        """), {
            "container_id": container_id, "comment_id": claim["comment_id"],
            "user_id": claim["user_id"], "account_id": claim["account_id"],
        })
        await session.commit()
        published_id = await publish_reply_container_once(
            token, claim["threads_user_id"], container_id
        )
    except ThreadsPublishUnknownError:
        status = "unknown"
        error_code = "PUBLISH_RESULT_UNKNOWN"
    except ThreadsAPIError as error:
        status = "permission_denied" if is_permission_error(error) else "failed"
        error_code = (
            "PERMISSION_DENIED" if is_permission_error(error)
            else "THREADS_API_ERROR"
        )
    except Exception as error:
        log.warning(
            "follow-up publish failed comment=%s account=%s error_type=%s",
            claim["comment_id"], claim["account_id"], type(error).__name__,
        )
        status = "failed"
        error_code = "PUBLISH_FAILED"
    else:
        status = "posted"
    await session.execute(text("""
        UPDATE neuro_comments
        SET follow_up_status = :status,
            follow_up_threads_id = :published_id,
            follow_up_error_code = :error_code,
            follow_up_count = CASE
              WHEN :status = 'posted' THEN 1 ELSE follow_up_count
            END
        WHERE id = :comment_id AND user_id = :user_id
          AND threads_account_id = :account_id
          AND follow_up_status = 'publishing' AND follow_up_count = 0
    """), {
        "status": status, "published_id": published_id,
        "error_code": error_code, "comment_id": claim["comment_id"],
        "user_id": claim["user_id"], "account_id": claim["account_id"],
    })
    await session.commit()
    return status


async def recover_stale_claims(session: AsyncSession) -> dict[str, int]:
    """Never retry a publish that may already have reached Threads."""
    generation_result = await session.execute(text("""
        WITH recovered AS (
          UPDATE neuro_comments
          SET status = CASE
                WHEN comment_text <> '' THEN 'pending' ELSE 'failed'
              END,
              publish_error_code = CASE
                WHEN comment_text <> '' THEN NULL ELSE 'STALE_GENERATION_CLAIM'
              END,
              generation_claimed_at = NULL
          WHERE status = 'generating'
            AND generation_claimed_at < now() - interval '15 minutes'
          RETURNING radar_candidate_id, user_id, threads_account_id,
                    CASE WHEN comment_text <> '' THEN 'pending'
                         ELSE 'score_blocked' END AS candidate_status
        )
        UPDATE radar_candidates candidate
        SET status = recovered.candidate_status, updated_at = now()
        FROM recovered
        WHERE candidate.id = recovered.radar_candidate_id
          AND candidate.user_id = recovered.user_id
          AND candidate.threads_account_id = recovered.threads_account_id
    """))
    publish_result = await session.execute(text("""
        UPDATE neuro_comments
        SET status = 'unknown', publish_error_code = 'STALE_PUBLISH_CLAIM'
        WHERE status = 'publishing'
          AND publish_claimed_at < now() - interval '15 minutes'
    """))
    follow_result = await session.execute(text("""
        UPDATE neuro_comments
        SET follow_up_status = 'unknown',
            follow_up_error_code = 'STALE_PUBLISH_CLAIM'
        WHERE follow_up_status = 'publishing'
          AND follow_up_claimed_at < now() - interval '15 minutes'
    """))
    return {
        "generation_recovered": max(0, int(generation_result.rowcount or 0)),
        "publish_unknown": max(0, int(publish_result.rowcount or 0)),
        "follow_up_unknown": max(0, int(follow_result.rowcount or 0)),
    }
