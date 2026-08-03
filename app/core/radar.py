"""
Модуль 1 - Радар.

ГЛАВНОЕ ОГРАНИЧЕНИЕ API: метрик чужих постов нет. keyword_search отдаёт
только контент. Insights - только по своим постам.

Поэтому виральность чужих постов - ПРОКСИ:
  virality_score = вес позиции в TOP-выдаче + бонус за свежесть.
Threads сам ранжирует TOP по популярности - крадём их ранжирование.
Реальные метрики (лайки/просмотры) - за интерфейсом fetch_public_metrics():
на MVP он пустой, потом туда встаёт скрейпер-API отдельным бизнес-решением.
Схема готова: metrics_json в posts_library ждёт данные из любого источника.

Квота поиска: 2200/юзер/24ч по докам. Краулер жжём консервативно -
до CRAWL_BUDGET запросов с токена в сутки, round-robin по аккаунтам
с наименьшим расходом. Юзерские поиски (за кредиты) идут с токена юзера.
"""
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai_cost import AIUsageContext
from app.core import credits, social_brain
from app.core.config import CREDIT_COSTS
from app.core.llm import LLM_MAX_TOKENS, ask_json
from app.core.threads_api import ThreadsAPIError, is_permission_error, keyword_search
from app.schemas.engagement import (
    DeterministicScore,
    RadarSearchSummary,
    RadarSemanticScoreResponse,
)
from app.schemas.llm import RadarAnalysisResponse

log = logging.getLogger("radar")

CRAWL_BUDGET_PER_ACC = 50  # запросов/сутки с одного токена на краулер
RADAR_SEMANTIC_LIMIT_PER_RUN = 3
_WORD_RE = re.compile(r"[\w-]+", re.UNICODE)
_URL_RE = re.compile(r"https?://|www\.|t\.me/", re.I)
_SPAM_RE = re.compile(
    r"(?i)(guaranteed income|crypto giveaway|free money|заработок без вложений|"
    r"гарантированн(?:ый|ая) доход|ставки на спорт|казино)"
)
_PROHIBITED_RE = re.compile(
    r"(?i)(купить наркотики|продажа оружия|child sexual|террористическ)"
)

RADAR_SEMANTIC_SYSTEM = """You score a public Threads post for a creator.
Use only the compact account context and post supplied. Do not write a comment.
Return JSON with: relevant (boolean), topical_relevance (0..100),
conversation_potential (0..100), safe (boolean), reason (short string).
Reject spam, unsafe content, and posts that do not fit the creator's niche."""


async def fetch_public_metrics(post_ids: list[str]) -> dict[str, dict]:
    """Заглушка под внешний источник метрик (скрейпер-API).
    Возвращает {post_id: {views, likes, ...}}. На MVP - пусто."""
    return {}


def proxy_virality(rank: int, posted_at: datetime | None) -> float:
    """Позиция в TOP-выдаче (0 = первый) + свежесть."""
    score = max(0.0, 100.0 - rank * 3)
    if posted_at:
        age_h = (datetime.now(timezone.utc) - posted_at).total_seconds() / 3600
        if age_h < 48:
            score += 20 * (1 - age_h / 48)
    return round(score, 2)


async def _bump_quota(session: AsyncSession, acc_id: int, n: int = 1):
    await session.execute(text("""
        INSERT INTO search_quota (threads_account_id, window_start, used)
        VALUES (:acc, current_date, :n)
        ON CONFLICT (threads_account_id, window_start)
        DO UPDATE SET used = search_quota.used + :n
    """), {"acc": acc_id, "n": n})


async def pick_crawler_account(session: AsyncSession):
    """Аккаунт с наименьшим расходом квоты сегодня и живым токеном."""
    row = (await session.execute(text("""
        SELECT ta.id, ta.access_token_enc,
               coalesce(sq.used, 0) AS used
        FROM threads_accounts ta
        LEFT JOIN search_quota sq
          ON sq.threads_account_id = ta.id AND sq.window_start = current_date
        WHERE ta.expires_at > now()
          AND ta.connection_status = 'connected'
          AND ta.access_token_enc IS NOT NULL
          AND coalesce(sq.used, 0) < :budget
        ORDER BY used ASC LIMIT 1
    """), {"budget": CRAWL_BUDGET_PER_ACC})).first()
    return row


async def search_and_store(session: AsyncSession, token: str, acc_id: int,
                           niche: str, query: str) -> list[dict]:
    """Один поисковый запрос: дёрнули API, положили в библиотеку, вернули посты."""
    posts = await keyword_search(token, query, search_type="TOP")
    await _bump_quota(session, acc_id)

    for rank, p in enumerate(posts):
        if p.get("is_reply"):
            continue
        posted_at = None
        if p.get("timestamp"):
            posted_at = datetime.fromisoformat(
                p["timestamp"].replace("+0000", "+00:00"))
        await session.execute(text("""
            INSERT INTO authors (threads_author_id, username, updated_at)
            VALUES (:aid, :un, now())
            ON CONFLICT (threads_author_id) DO NOTHING
        """), {"aid": p["username"], "un": p["username"]})
        await session.execute(text("""
            INSERT INTO posts_library
                (threads_post_id, niche, author_id, text, metrics_json,
                 virality_score, fetched_at)
            VALUES (:pid, :niche, :aid, :txt, :mj, :vs, now())
            ON CONFLICT (threads_post_id) DO UPDATE SET
                virality_score = greatest(posts_library.virality_score, :vs),
                fetched_at = now()
        """), {"pid": p["id"], "niche": niche, "aid": p["username"],
               "txt": p.get("text", ""),
               "mj": json.dumps({"permalink": p.get("permalink"),
                                 "has_replies": p.get("has_replies")}),
               "vs": proxy_virality(rank, posted_at)})
    return posts


async def top_posts(session: AsyncSession, niche: str,
                    limit: int = 7) -> list:
    """Топ ниши из накопленной библиотеки, свежее - выше."""
    return (await session.execute(text("""
        SELECT threads_post_id, author_id, text, virality_score,
               metrics_json->>'permalink' AS permalink
        FROM posts_library
        WHERE niche = :niche AND fetched_at > now() - interval '14 days'
          AND length(trim(text)) > 30
        ORDER BY virality_score DESC, fetched_at DESC
        LIMIT :lim
    """), {"niche": niche, "lim": limit})).all()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace("+0000", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _author_parts(post: dict) -> tuple[str, str | None, str | None]:
    owner = post.get("owner") if isinstance(post.get("owner"), dict) else {}
    author_id = str(owner.get("id") or "").strip() or None
    username = str(post.get("username") or owner.get("username") or "").strip() or None
    key = author_id or (username or "unknown").casefold()
    return key, author_id, username


def _language_matches(text_value: str, language: str) -> bool:
    if language == "any":
        return True
    letters = [char for char in text_value if char.isalpha()]
    if not letters:
        return False
    cyrillic = sum("а" <= char.casefold() <= "я" or char.casefold() == "ё" for char in letters)
    ratio = cyrillic / len(letters)
    return ratio >= 0.3 if language == "ru" else ratio < 0.3


def _words(value: str) -> set[str]:
    return {word.casefold() for word in _WORD_RE.findall(value) if len(word) >= 2}


def _topic_overlap(post_words: set[str], topic_words: set[str]) -> int:
    matches = 0
    for topic_word in topic_words:
        if topic_word in post_words:
            matches += 1
            continue
        if len(topic_word) >= 6 and any(
            len(post_word) >= 6
            and post_word[:7] == topic_word[:7]
            for post_word in post_words
        ):
            matches += 1
    return matches


def deterministic_score(
    post: dict,
    *,
    niche: str,
    keywords: list[str],
    rank: int = 0,
    duplicate_hits: int = 0,
    author_comments: int = 0,
    now: datetime | None = None,
) -> DeterministicScore:
    """Cheap, explainable gate used before any semantic LLM call."""
    now = now or _utc_now()
    body = str(post.get("text") or "").strip()
    if not body:
        return DeterministicScore(
            total=0, topical_relevance=0, freshness=0,
            engagement_potential=0, conversation_potential=0,
            author_penalty=0, duplicate_penalty=0, safe=False,
            filter_reason="empty_text", summary="empty post",
        )
    if _SPAM_RE.search(body) or _PROHIBITED_RE.search(body):
        return DeterministicScore(
            total=0, topical_relevance=0, freshness=0,
            engagement_potential=0, conversation_potential=0,
            author_penalty=0, duplicate_penalty=0, safe=False,
            filter_reason="unsafe_or_spam", summary="unsafe or spam",
        )

    post_words = _words(body)
    topic_words = _words(" ".join([niche, *keywords]))
    overlap = _topic_overlap(post_words, topic_words)
    topical = min(35, overlap * 9 + (5 if overlap else 0))

    published_at = _parse_timestamp(post.get("timestamp"))
    freshness = 5
    if published_at:
        age_hours = max(0.0, (now - published_at).total_seconds() / 3600)
        freshness = max(0, round(20 * (1 - min(age_hours, 72) / 72)))

    metrics = post.get("metrics") if isinstance(post.get("metrics"), dict) else post
    interactions = sum(
        int(metrics.get(key) or 0)
        for key in ("likes", "replies", "reposts", "quotes", "shares")
        if isinstance(metrics.get(key), (int, float))
    )
    engagement = min(15, max(0, 10 - rank) + min(5, interactions))
    conversation = 5
    if "?" in body:
        conversation += 7
    if post.get("has_replies") or int(metrics.get("replies") or 0) > 0:
        conversation += 5
    if 80 <= len(body) <= 700:
        conversation += 3
    conversation = min(20, conversation)

    author_penalty = min(20, max(0, author_comments) * 10)
    duplicate_penalty = min(10, max(0, duplicate_hits) * 5)
    total = max(
        0,
        min(
            100,
            topical + freshness + engagement + conversation
            - author_penalty - duplicate_penalty,
        ),
    )
    return DeterministicScore(
        total=total,
        topical_relevance=topical,
        freshness=freshness,
        engagement_potential=engagement,
        conversation_potential=conversation,
        author_penalty=author_penalty,
        duplicate_penalty=duplicate_penalty,
        safe=True,
        filter_reason="off_topic" if topical == 0 else None,
        summary=(
            f"topic={topical}, fresh={freshness}, engagement={engagement}, "
            f"conversation={conversation}, penalties={author_penalty + duplicate_penalty}"
        ),
    )


def prefilter_reason(
    post: dict,
    *,
    own_threads_user_id: str,
    own_username: str | None,
    excluded_authors: set[str],
    language: str,
    max_age_hours: int,
    now: datetime | None = None,
) -> str | None:
    body = str(post.get("text") or "").strip()
    author_key, author_id, username = _author_parts(post)
    excluded = {value.casefold() for value in excluded_authors}
    if post.get("is_reply"):
        return "reply"
    if author_key == "unknown":
        return "missing_author"
    if author_id == own_threads_user_id or (
        own_username and username and username.casefold() == own_username.casefold()
    ):
        return "own_post"
    if author_key.casefold() in excluded or (username and username.casefold() in excluded):
        return "excluded_author"
    if not body:
        return "empty_text"
    if not _language_matches(body, language):
        return "language"
    published_at = _parse_timestamp(post.get("timestamp"))
    if published_at and (now or _utc_now()) - published_at > timedelta(hours=max_age_hours):
        return "too_old"
    if _SPAM_RE.search(body):
        return "spam"
    if _PROHIBITED_RE.search(body):
        return "prohibited"
    return None


async def discover_account_posts(
    session: AsyncSession,
    *,
    user_id: int,
    account_id: int,
    token: str,
) -> RadarSearchSummary:
    """Discover and store candidates using only the owned account's token/settings."""
    setting = (
        await session.execute(text("""
            SELECT rs.niche, rs.keywords, rs.language, rs.max_age_hours,
                   ns.excluded_authors, account.threads_user_id,
                   account.username,
                   coalesce((
                     SELECT quota.used FROM search_quota quota
                     WHERE quota.threads_account_id = account.id
                       AND quota.window_start = current_date
                   ), 0)
            FROM radar_settings rs
            JOIN threads_accounts account
              ON account.id = rs.threads_account_id
             AND account.user_id = rs.user_id
             AND account.connection_status = 'connected'
             AND account.access_token_enc IS NOT NULL
             AND account.expires_at > now()
            JOIN neuro_settings ns
              ON ns.threads_account_id = rs.threads_account_id
             AND ns.user_id = rs.user_id
            WHERE rs.user_id = :user_id
              AND rs.threads_account_id = :account_id
        """), {"user_id": user_id, "account_id": account_id})
    ).first()
    if setting is None:
        raise ValueError("owned connected account settings not found")
    (niche, configured_keywords, language, max_age_hours, excluded,
     own_id, own_username, quota_used) = setting
    remaining_quota = max(0, CRAWL_BUDGET_PER_ACC - int(quota_used))
    keywords = [
        str(value).strip()
        for value in (configured_keywords or [])
        if str(value).strip()
    ][:min(5, remaining_quota)]
    run = (
        await session.execute(text("""
            INSERT INTO radar_search_runs (user_id, threads_account_id, keywords)
            VALUES (:user_id, :account_id, :keywords)
            RETURNING id
        """), {"user_id": user_id, "account_id": account_id, "keywords": keywords})
    ).first()
    run_id = int(run[0])
    summary = RadarSearchSummary(run_id=run_id, searched_keywords=len(keywords))
    if not keywords:
        error_code = (
            "SEARCH_QUOTA_EXHAUSTED" if configured_keywords else "NO_KEYWORDS"
        )
        await session.execute(text("""
            UPDATE radar_search_runs
            SET status = 'failed', error_code = :error_code, finished_at = now()
            WHERE id = :run_id AND user_id = :user_id
              AND threads_account_id = :account_id
        """), {
            "error_code": error_code, "run_id": run_id,
            "user_id": user_id, "account_id": account_id,
        })
        return summary.model_copy(
            update={"status": "failed", "error_code": error_code}
        )
    grouped: dict[str, dict] = {}

    try:
        for keyword in keywords:
            posts = await keyword_search(token, keyword, search_type="RECENT")
            await _bump_quota(session, account_id)
            summary.results_seen += len(posts)
            for rank, source in enumerate(posts):
                post_id = str(source.get("id") or "").strip()
                if not post_id:
                    summary.filtered += 1
                    continue
                if post_id in grouped:
                    grouped[post_id]["keywords"].add(keyword)
                    grouped[post_id]["duplicate_hits"] += 1
                    summary.duplicates += 1
                    continue
                grouped[post_id] = {
                    "post": source,
                    "keywords": {keyword},
                    "duplicate_hits": 0,
                    "rank": rank,
                }
    except ThreadsAPIError as error:
        status = "permission_denied" if is_permission_error(error) else "failed"
        code = "PERMISSION_DENIED" if is_permission_error(error) else "THREADS_API_ERROR"
        await session.execute(text("""
            UPDATE radar_search_runs
            SET status = :status, error_code = :code, finished_at = now(),
                results_seen = :seen
            WHERE id = :run_id AND user_id = :user_id
              AND threads_account_id = :account_id
        """), {
            "status": status, "code": code, "seen": summary.results_seen,
            "run_id": run_id, "user_id": user_id, "account_id": account_id,
        })
        return summary.model_copy(update={"status": status, "error_code": code})

    excluded_authors = {str(value).casefold() for value in (excluded or [])}
    for post_id, item in grouped.items():
        post = item["post"]
        reason = prefilter_reason(
            post,
            own_threads_user_id=str(own_id),
            own_username=own_username,
            excluded_authors=excluded_authors,
            language=language,
            max_age_hours=max_age_hours,
        )
        if reason:
            summary.filtered += 1
            continue
        author_key, author_id, username = _author_parts(post)
        prior = (
            await session.execute(text("""
                SELECT status FROM radar_candidates
                WHERE user_id = :user_id AND threads_account_id = :account_id
                  AND threads_post_id = :post_id
            """), {
                "user_id": user_id, "account_id": account_id, "post_id": post_id,
            })
        ).first()
        if prior and prior[0] in ("rejected", "commented", "generating", "pending"):
            summary.filtered += 1
            continue
        memory = (
            await session.execute(text("""
                SELECT comments_posted FROM neuro_author_memory
                WHERE user_id = :user_id AND threads_account_id = :account_id
                  AND author_key = :author_key
            """), {
                "user_id": user_id, "account_id": account_id, "author_key": author_key,
            })
        ).first()
        score = deterministic_score(
            post,
            niche=niche,
            keywords=list(item["keywords"]),
            rank=item["rank"],
            duplicate_hits=item["duplicate_hits"],
            author_comments=int(memory[0]) if memory else 0,
        )
        candidate_status = "discovered" if score.safe and not score.filter_reason else "filtered"
        metrics = {
            key: post[key]
            for key in ("likes", "replies", "reposts", "quotes", "shares", "views", "has_replies")
            if key in post
        }
        saved = (
            await session.execute(text("""
                INSERT INTO radar_candidates (
                  user_id, threads_account_id, threads_post_id,
                  author_key, author_threads_id, author_username,
                  post_text, permalink, published_at, found_keywords,
                  duplicate_hits, metrics_json, deterministic_score,
                  score_reason, status, filtered_reason
                ) VALUES (
                  :user_id, :account_id, :post_id,
                  :author_key, :author_id, :username,
                  :post_text, :permalink, :published_at, :keywords,
                  :duplicate_hits, cast(:metrics as jsonb), :score,
                  :score_reason, :status, :filtered_reason
                )
                ON CONFLICT (threads_account_id, threads_post_id) DO UPDATE SET
                  last_seen_at = now(), updated_at = now(),
                  found_keywords = ARRAY(
                    SELECT DISTINCT value FROM unnest(
                      radar_candidates.found_keywords || excluded.found_keywords
                    ) value
                  ),
                  duplicate_hits = radar_candidates.duplicate_hits + excluded.duplicate_hits,
                  metrics_json = excluded.metrics_json,
                  deterministic_score = excluded.deterministic_score,
                  score_reason = excluded.score_reason,
                  filtered_reason = excluded.filtered_reason,
                  status = CASE
                    WHEN radar_candidates.status IN (
                      'scoring', 'ready', 'generating', 'pending',
                      'commented', 'rejected', 'score_blocked'
                    ) THEN radar_candidates.status
                    ELSE excluded.status
                  END
                RETURNING id
            """), {
                "user_id": user_id, "account_id": account_id, "post_id": post_id,
                "author_key": author_key, "author_id": author_id, "username": username,
                "post_text": str(post.get("text") or "")[:5000],
                "permalink": post.get("permalink"),
                "published_at": _parse_timestamp(post.get("timestamp")),
                "keywords": list(item["keywords"]),
                "duplicate_hits": item["duplicate_hits"],
                "metrics": json.dumps(metrics), "score": score.total,
                "score_reason": score.summary, "status": candidate_status,
                "filtered_reason": score.filter_reason,
            })
        ).first()
        if saved:
            summary.candidates_saved += 1
            await session.execute(text("""
                INSERT INTO neuro_author_memory (
                  user_id, threads_account_id, author_key,
                  author_threads_id, author_username, discovered_count
                ) VALUES (
                  :user_id, :account_id, :author_key,
                  :author_id, :username, 1
                )
                ON CONFLICT (threads_account_id, author_key) DO UPDATE SET
                  discovered_count = neuro_author_memory.discovered_count + 1,
                  author_threads_id = coalesce(
                    excluded.author_threads_id,
                    neuro_author_memory.author_threads_id
                  ),
                  author_username = coalesce(
                    excluded.author_username,
                    neuro_author_memory.author_username
                  ),
                  updated_at = now()
            """), {
                "user_id": user_id, "account_id": account_id,
                "author_key": author_key, "author_id": author_id,
                "username": username,
            })

    await session.execute(text("""
        UPDATE radar_search_runs
        SET status = 'success', finished_at = now(),
            results_seen = :seen, candidates_saved = :saved,
            filtered_count = :filtered, duplicate_count = :duplicates
        WHERE id = :run_id AND user_id = :user_id
          AND threads_account_id = :account_id
    """), {
        "seen": summary.results_seen, "saved": summary.candidates_saved,
        "filtered": summary.filtered, "duplicates": summary.duplicates,
        "run_id": run_id, "user_id": user_id, "account_id": account_id,
    })
    return summary


async def claim_semantic_candidates(
    session: AsyncSession,
    *,
    user_id: int,
    account_id: int,
    limit: int = RADAR_SEMANTIC_LIMIT_PER_RUN,
) -> list[dict]:
    rows = (
        await session.execute(text("""
            WITH claimed AS (
              SELECT candidate.id
              FROM radar_candidates candidate
              WHERE candidate.user_id = :user_id
                AND candidate.threads_account_id = :account_id
                AND (
                  candidate.status = 'discovered'
                  OR (candidate.status = 'scoring'
                      AND candidate.semantic_claimed_at < now() - interval '15 minutes')
                )
              ORDER BY candidate.deterministic_score DESC,
                       candidate.discovered_at DESC
              FOR UPDATE SKIP LOCKED
              LIMIT :limit
            )
            UPDATE radar_candidates candidate
            SET status = 'scoring', semantic_claimed_at = now(), updated_at = now()
            FROM claimed
            WHERE candidate.id = claimed.id
            RETURNING candidate.id, candidate.post_text,
                      candidate.author_username, candidate.deterministic_score
        """), {
            "user_id": user_id, "account_id": account_id, "limit": limit,
        })
    ).mappings().all()
    return [dict(row) for row in rows]


async def semantic_score_candidates(
    session: AsyncSession,
    *,
    user_id: int,
    account_id: int,
    limit: int = RADAR_SEMANTIC_LIMIT_PER_RUN,
) -> int:
    claimed = await claim_semantic_candidates(
        session, user_id=user_id, account_id=account_id, limit=limit
    )
    if not claimed:
        return 0
    context = await social_brain.build_account_context(
        session,
        user_id=user_id,
        threads_account_id=account_id,
        task="radar",
        budget_tokens=500,
    )
    settings_row = (
        await session.execute(text("""
            SELECT ns.minimum_score, rs.niche, rs.keywords
            FROM neuro_settings ns
            JOIN radar_settings rs
              ON rs.threads_account_id = ns.threads_account_id
             AND rs.user_id = ns.user_id
            WHERE ns.user_id = :user_id
              AND ns.threads_account_id = :account_id
        """), {"user_id": user_id, "account_id": account_id})
    ).first()
    if settings_row is None:
        return 0
    minimum_score, niche, keywords = settings_row
    scored = 0
    usage_context = AIUsageContext(
        user_id=user_id,
        threads_account_id=account_id,
        run_id=f"radar:{uuid.uuid4().hex}:{user_id}:{account_id}",
    )
    compact_context = json.dumps(
        context.compact_dict(), ensure_ascii=False, separators=(",", ":")
    )
    for candidate in claimed:
        candidate_id = int(candidate["id"])
        try:
            await credits.spend_once(
                session, user_id, account_id,
                CREDIT_COSTS["radar_semantic_score"],
                "radar_semantic_score",
                f"radar-semantic:{candidate_id}:v1",
            )
            await session.commit()
            response = await ask_json(
                RADAR_SEMANTIC_SYSTEM,
                json.dumps({
                    "account_context": json.loads(compact_context),
                    "niche": niche,
                    "keywords": list(keywords or []),
                    "post": {
                        "author": candidate.get("author_username"),
                        "text": candidate["post_text"],
                    },
                }, ensure_ascii=False, separators=(",", ":")),
                max_tokens=LLM_MAX_TOKENS["radar_semantic_score"],
                response_model=RadarSemanticScoreResponse,
                feature="radar_semantic_score",
                usage_context=usage_context,
            )
            semantic = round(
                (response.topical_relevance + response.conversation_potential) / 2
            )
            final = round(int(candidate["deterministic_score"]) * 0.7 + semantic * 0.3)
            ready = response.safe and response.relevant and final >= int(minimum_score)
            await session.execute(text("""
                UPDATE radar_candidates
                SET semantic_score = :semantic, final_score = :final,
                    score_reason = :reason,
                    status = :status,
                    filtered_reason = CASE WHEN :ready THEN NULL ELSE 'semantic_or_threshold' END,
                    semantic_scored_at = now(), updated_at = now()
                WHERE id = :candidate_id AND user_id = :user_id
                  AND threads_account_id = :account_id AND status = 'scoring'
            """), {
                "semantic": semantic, "final": final, "reason": response.reason,
                "status": "ready" if ready else "filtered", "ready": ready,
                "candidate_id": candidate_id, "user_id": user_id,
                "account_id": account_id,
            })
            scored += 1
        except credits.NotEnoughCredits:
            await session.execute(text("""
                UPDATE radar_candidates SET status = 'score_blocked',
                  filtered_reason = 'not_enough_credits', updated_at = now()
                WHERE id = :candidate_id AND user_id = :user_id
                  AND threads_account_id = :account_id AND status = 'scoring'
            """), {
                "candidate_id": candidate_id, "user_id": user_id,
                "account_id": account_id,
            })
        except Exception as error:
            log.warning(
                "radar semantic score failed candidate=%s account=%s error_type=%s",
                candidate_id, account_id, type(error).__name__,
            )
            await session.execute(text("""
                UPDATE radar_candidates SET status = 'score_failed',
                  filtered_reason = 'semantic_error', updated_at = now()
                WHERE id = :candidate_id AND user_id = :user_id
                  AND threads_account_id = :account_id AND status = 'scoring'
            """), {
                "candidate_id": candidate_id, "user_id": user_id,
                "account_id": account_id,
            })
    return scored


RAZBOR_SYSTEM = """Ты - аналитик виральности Threads. Тебе дают пост, который залетел.
Разбери механику. Без воды и комплиментов автору.

JSON:
{
 "hook": "какой хук в первой строке и почему цепляет",
 "structure": "как построен пост: ритм, абзацы, длина",
 "trigger": "на какую эмоцию/боль давит",
 "ending": "чем закрывает: CTA, вопрос, панч",
 "how_to_repeat": "механика в 2-3 предложениях: как повторить с ДРУГОЙ темой",
 "hook_type": "один из: pain/number/myth/list/story/ban/compare/question/insight/provocation/unpopular"
}"""


async def razbor(
    session: AsyncSession,
    post_id: str,
    *,
    usage_context: AIUsageContext | None = None,
) -> dict:
    row = (await session.execute(text(
        "SELECT text FROM posts_library WHERE threads_post_id = :pid"
    ), {"pid": post_id})).first()
    if not row:
        raise ValueError("post not in library")
    post_text = (row[0] or "").strip()
    if len(post_text) <= 30:
        raise ValueError("post text is too short")
    response = await ask_json(
        RAZBOR_SYSTEM,
        f"Пост:\n{post_text}",
        max_tokens=LLM_MAX_TOKENS["radar_analysis"],
        response_model=RadarAnalysisResponse,
        feature="radar_analysis",
        usage_context=usage_context,
    )
    result = response.model_dump(mode="json")
    await session.execute(text("""
        UPDATE posts_library SET hook_type = :ht WHERE threads_post_id = :pid
    """), {"ht": result.get("hook_type"), "pid": post_id})
    return result
