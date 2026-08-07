"""Persistence boundary for interface preferences and onboarding progress."""

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.brain_repo import BrainRepo
from app.schemas.ux import (
    AccountUXSettings,
    InterfaceMode,
    OnboardingProgress,
    OnboardingStatus,
    UserUXPreferences,
)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def normalize_text_list(value: Any, *, limit: int = 20) -> list[str]:
    if isinstance(value, str):
        values = value.replace("\n", ",").split(",")
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        values = []
    result = []
    for item in values:
        compact = str(item).strip()
        if compact and compact not in result:
            result.append(compact)
        if len(result) >= limit:
            break
    return result


class UXService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def preferences(self, user_id: int) -> UserUXPreferences:
        row = (
            await self.session.execute(text("""
                INSERT INTO user_preferences (user_id)
                SELECT id FROM users WHERE id = :user_id
                ON CONFLICT (user_id) DO UPDATE SET
                  updated_at = user_preferences.updated_at
                RETURNING user_id, interface_mode
            """), {"user_id": user_id})
        ).mappings().first()
        if row is None:
            raise LookupError("User not found")
        return UserUXPreferences.model_validate(dict(row))

    async def set_mode(
        self,
        user_id: int,
        mode: InterfaceMode,
    ) -> UserUXPreferences:
        row = (
            await self.session.execute(text("""
                UPDATE user_preferences
                SET interface_mode = :mode, updated_at = now()
                WHERE user_id = :user_id
                RETURNING user_id, interface_mode
            """), {"user_id": user_id, "mode": mode})
        ).mappings().first()
        if row is None:
            await self.preferences(user_id)
            return await self.set_mode(user_id, mode)
        return UserUXPreferences.model_validate(dict(row))

    async def account_settings(
        self,
        user_id: int,
        account_id: int,
    ) -> AccountUXSettings | None:
        row = (
            await self.session.execute(text("""
                SELECT account.user_id, account.id AS threads_account_id,
                       coalesce(account.username, account.id::text) AS username,
                       setting.topics, setting.timezone, setting.active,
                       setting.publish_notifications_enabled,
                       coalesce(radar.keywords, '{}'::text[]) AS radar_keywords
                FROM threads_accounts account
                JOIN autocontent_settings setting
                  ON setting.threads_account_id = account.id
                 AND setting.user_id = account.user_id
                LEFT JOIN radar_settings radar
                  ON radar.threads_account_id = account.id
                 AND radar.user_id = account.user_id
                WHERE account.user_id = :user_id
                  AND account.id = :account_id
            """), {"user_id": user_id, "account_id": account_id})
        ).mappings().first()
        if row is None:
            return None
        brain = await BrainRepo(self.session).get_or_create(user_id, account_id)
        dna = dict(brain.dna)
        voice = _json_object(dna.get("voice"))
        examples = normalize_text_list(
            dna.get("recent_examples")
            or dna.get("style_examples")
            or voice.get("sample_phrases"),
            limit=10,
        )
        return AccountUXSettings(
            user_id=user_id,
            threads_account_id=account_id,
            username=str(row["username"]),
            manual_style=(
                str(
                    voice.get("manual_style")
                    or voice.get("tone")
                    or voice.get("summary")
                ).strip()
                if (
                    voice.get("manual_style")
                    or voice.get("tone")
                    or voice.get("summary")
                )
                else None
            ),
            style_examples=examples,
            topics=normalize_text_list(row.get("topics")),
            radar_keywords=normalize_text_list(
                row.get("radar_keywords"), limit=10
            ),
            timezone=str(row.get("timezone") or "Europe/Moscow"),
            autopilot_enabled=bool(row.get("active")),
            publish_notifications_enabled=bool(
                row.get("publish_notifications_enabled", True)
            ),
        )

    async def save_manual_style(
        self,
        user_id: int,
        account_id: int,
        value: str,
    ) -> bool:
        compact = value.strip()
        if not 10 <= len(compact) <= 1500:
            return False
        repository = BrainRepo(self.session)
        brain = await repository.get_or_create(user_id, account_id)
        dna = dict(brain.dna)
        voice = _json_object(dna.get("voice"))
        voice["manual_style"] = compact
        dna["voice"] = voice
        await repository.update_section(
            brain.id,
            "dna",
            dna,
            user_id=user_id,
            account_id=account_id,
        )
        return True

    async def save_style_examples(
        self,
        user_id: int,
        account_id: int,
        examples: list[str],
    ) -> bool:
        compact = [
            item[:1000]
            for item in normalize_text_list(examples, limit=10)
        ]
        repository = BrainRepo(self.session)
        brain = await repository.get_or_create(user_id, account_id)
        dna = dict(brain.dna)
        if compact:
            dna["recent_examples"] = compact
        else:
            dna.pop("recent_examples", None)
            dna.pop("style_examples", None)
        await repository.update_section(
            brain.id,
            "dna",
            dna,
            user_id=user_id,
            account_id=account_id,
        )
        return True

    async def save_topics(
        self,
        user_id: int,
        account_id: int,
        topics: list[str],
    ) -> bool:
        compact = [
            item[:300]
            for item in normalize_text_list(topics, limit=20)
        ]
        row = (
            await self.session.execute(text("""
                UPDATE autocontent_settings setting
                SET topics = :topics, updated_at = now()
                FROM threads_accounts account
                WHERE setting.user_id = :user_id
                  AND setting.threads_account_id = :account_id
                  AND account.id = setting.threads_account_id
                  AND account.user_id = setting.user_id
                RETURNING setting.threads_account_id
            """), {
                "user_id": user_id,
                "account_id": account_id,
                "topics": "\n".join(compact),
            })
        ).first()
        return row is not None

    async def save_radar_keywords(
        self,
        user_id: int,
        account_id: int,
        keywords: list[str],
    ) -> bool:
        compact = [
            item[:100]
            for item in normalize_text_list(keywords, limit=10)
        ]
        row = (
            await self.session.execute(text("""
                UPDATE radar_settings setting
                SET keywords = :keywords, updated_at = now()
                FROM threads_accounts account
                WHERE setting.user_id = :user_id
                  AND setting.threads_account_id = :account_id
                  AND account.id = setting.threads_account_id
                  AND account.user_id = setting.user_id
                RETURNING setting.threads_account_id
            """), {
                "user_id": user_id,
                "account_id": account_id,
                "keywords": compact,
            })
        ).first()
        return row is not None

    async def set_publish_notifications(
        self,
        user_id: int,
        account_id: int,
        enabled: bool,
    ) -> bool:
        row = (
            await self.session.execute(text("""
                UPDATE autocontent_settings setting
                SET publish_notifications_enabled = :enabled,
                    updated_at = now()
                FROM threads_accounts account
                WHERE setting.user_id = :user_id
                  AND setting.threads_account_id = :account_id
                  AND account.id = setting.threads_account_id
                  AND account.user_id = setting.user_id
                RETURNING setting.threads_account_id
            """), {
                "user_id": user_id,
                "account_id": account_id,
                "enabled": enabled,
            })
        ).first()
        return row is not None

    async def onboarding(
        self,
        user_id: int,
        account_id: int,
    ) -> OnboardingProgress | None:
        row = (
            await self.session.execute(text("""
                SELECT user_id, threads_account_id, status,
                       current_step, data, updated_at
                FROM ux_onboarding
                WHERE user_id = :user_id
                  AND threads_account_id = :account_id
            """), {"user_id": user_id, "account_id": account_id})
        ).mappings().first()
        return self._progress(row)

    async def start_onboarding(
        self,
        user_id: int,
        account_id: int,
    ) -> OnboardingProgress | None:
        row = (
            await self.session.execute(text("""
                INSERT INTO ux_onboarding (
                  user_id, threads_account_id, status, current_step
                )
                SELECT account.user_id, account.id, 'in_progress', 1
                FROM threads_accounts account
                WHERE account.user_id = :user_id
                  AND account.id = :account_id
                  AND account.connection_status = 'connected'
                ON CONFLICT (user_id, threads_account_id) DO UPDATE SET
                  status = CASE
                    WHEN ux_onboarding.status = 'completed'
                      THEN ux_onboarding.status
                    ELSE 'in_progress'
                  END,
                  current_step = CASE
                    WHEN ux_onboarding.status = 'completed'
                      THEN ux_onboarding.current_step
                    ELSE greatest(ux_onboarding.current_step, 1)
                  END,
                  updated_at = now()
                RETURNING user_id, threads_account_id, status,
                          current_step, data, updated_at
            """), {"user_id": user_id, "account_id": account_id})
        ).mappings().first()
        return self._progress(row)

    async def update_onboarding(
        self,
        user_id: int,
        account_id: int,
        *,
        step: int,
        values: dict[str, Any] | None = None,
        status: OnboardingStatus = "in_progress",
    ) -> OnboardingProgress | None:
        if not 0 <= step <= 9:
            raise ValueError("Onboarding step must be between 0 and 9")
        row = (
            await self.session.execute(text("""
                UPDATE ux_onboarding progress
                SET status = :status,
                    current_step = :step,
                    data = progress.data || CAST(:values AS jsonb),
                    updated_at = now()
                FROM threads_accounts account
                WHERE progress.user_id = :user_id
                  AND progress.threads_account_id = :account_id
                  AND account.id = progress.threads_account_id
                  AND account.user_id = progress.user_id
                RETURNING progress.user_id,
                          progress.threads_account_id,
                          progress.status,
                          progress.current_step,
                          progress.data,
                          progress.updated_at
            """), {
                "user_id": user_id,
                "account_id": account_id,
                "step": step,
                "status": status,
                "values": json.dumps(
                    values or {}, ensure_ascii=False, separators=(",", ":")
                ),
            })
        ).mappings().first()
        return self._progress(row)

    async def save_topic(
        self,
        user_id: int,
        account_id: int,
        topic: str,
    ) -> bool:
        compact = topic.strip()[:300]
        if not compact:
            return False
        updated = (
            await self.session.execute(text("""
                UPDATE autocontent_settings setting
                SET topics = :topic, updated_at = now()
                FROM threads_accounts account
                WHERE setting.user_id = :user_id
                  AND setting.threads_account_id = :account_id
                  AND account.id = setting.threads_account_id
                  AND account.user_id = setting.user_id
                RETURNING setting.threads_account_id
            """), {
                "user_id": user_id,
                "account_id": account_id,
                "topic": compact,
            })
        ).first()
        if updated is None:
            return False
        keywords = [
            item.strip()[:100]
            for item in compact.split(",")
            if item.strip()
        ][:10]
        await self.session.execute(text("""
            UPDATE radar_settings
            SET niche = :topic, keywords = :keywords, updated_at = now()
            WHERE user_id = :user_id
              AND threads_account_id = :account_id
        """), {
            "user_id": user_id,
            "account_id": account_id,
            "topic": compact[:200],
            "keywords": keywords or [compact[:100]],
        })
        await self.session.execute(text("""
            INSERT INTO user_niches (user_id, niche, keywords)
            VALUES (:user_id, :topic, :keywords)
            ON CONFLICT (user_id) DO UPDATE SET
              niche = excluded.niche,
              keywords = excluded.keywords
        """), {
            "user_id": user_id,
            "topic": compact[:200],
            "keywords": keywords or [compact[:100]],
        })
        return True

    async def save_autopilot_settings(
        self,
        user_id: int,
        account_id: int,
        **values: Any,
    ) -> bool:
        allowed = {
            "goal": "goal",
            "posts_per_day": "posts_per_day",
            "slots": "slots",
            "active": "active",
        }
        fields = [(allowed[key], value) for key, value in values.items() if key in allowed]
        if not fields:
            return False
        assignments = ", ".join(f"{column} = :{column}" for column, _ in fields)
        params = {column: value for column, value in fields}
        params.update({"user_id": user_id, "account_id": account_id})
        row = (
            await self.session.execute(text(f"""
                UPDATE autocontent_settings
                SET {assignments}, updated_at = now()
                WHERE user_id = :user_id
                  AND threads_account_id = :account_id
                RETURNING threads_account_id
            """), params)
        ).first()
        return row is not None

    async def save_neuro_mode(
        self,
        user_id: int,
        account_id: int,
        *,
        active: bool,
        mode: str = "approve",
    ) -> bool:
        if mode not in {"approve", "auto"}:
            raise ValueError("Unsupported Neuro mode")
        row = (
            await self.session.execute(text("""
                UPDATE neuro_settings
                SET active = :active, mode = :mode
                WHERE user_id = :user_id
                  AND threads_account_id = :account_id
                RETURNING threads_account_id
            """), {
                "user_id": user_id,
                "account_id": account_id,
                "active": active,
                "mode": mode,
            })
        ).first()
        return row is not None

    async def save_style(
        self,
        user_id: int,
        account_id: int,
        style: str,
    ) -> bool:
        hints = {
            "expert": "Экспертно и по делу",
            "friendly": "Дружелюбно и просто",
            "own_voice": None,
        }
        if style not in hints:
            raise ValueError("Unsupported writing style")
        repository = BrainRepo(self.session)
        brain = await repository.get_or_create(user_id, account_id)
        dna = dict(brain.dna)
        voice = dict(dna.get("voice") or {})
        hint = hints[style]
        if hint is None:
            voice.pop("manual_style", None)
        else:
            voice["manual_style"] = hint
        if voice:
            dna["voice"] = voice
        else:
            dna.pop("voice", None)
        await repository.update_section(
            brain.id,
            "dna",
            dna,
            user_id=user_id,
            account_id=account_id,
        )
        return True

    @staticmethod
    def _progress(row: Any) -> OnboardingProgress | None:
        if row is None:
            return None
        data = dict(row)
        data["data"] = _json_object(data.get("data"))
        return OnboardingProgress.model_validate(data)
