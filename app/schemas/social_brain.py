"""Typed, compact user context for the Social Brain layer."""

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

_TASK_FACT_LIMIT = 8


def _drop_empty(value: Any) -> Any:
    if isinstance(value, dict):
        compact = {
            key: _drop_empty(item)
            for key, item in value.items()
            if item is not None
        }
        return {
            key: item
            for key, item in compact.items()
            if item not in (None, "", [], {})
        }
    if isinstance(value, list):
        return [
            compact
            for item in value
            if (compact := _drop_empty(item)) not in (None, "", [], {})
        ]
    return value


class CompactContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    def compact_dict(self) -> dict[str, Any]:
        return _drop_empty(self.model_dump(mode="json"))

    def compact_json(self) -> str:
        return json.dumps(
            self.compact_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def character_count(self) -> int:
        return len(self.compact_json())


class TaskFact(CompactContext):
    key: str
    value: Any
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class BrainFact(CompactContext):
    fact_type: str
    key: str
    value: Any
    scope: Literal["global", "account"]
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source: str
    updated_at: datetime | None = None

    def for_task(self) -> TaskFact:
        return TaskFact(
            key=self.key,
            value=self.value,
            confidence=(
                self.confidence
                if self.confidence < 1.0
                else None
            ),
        )


def _task_facts(facts: list[BrainFact]) -> list[TaskFact]:
    prioritized = sorted(
        facts,
        key=lambda fact: fact.confidence,
        reverse=True,
    )
    return [
        fact.for_task()
        for fact in prioritized[:_TASK_FACT_LIMIT]
    ]


class BrainUserIdentity(CompactContext):
    user_id: int


class BrainAccountIdentity(CompactContext):
    threads_account_id: int
    threads_username: str | None = None
    uses_user_defaults: bool = False


class BrainVoice(CompactContext):
    available: bool = False
    lexicon: list[str] = Field(default_factory=list)
    sentence_length: str | None = None
    punctuation: str | None = None
    tone: str | None = None
    structure: str | None = None
    taboo: list[str] = Field(default_factory=list)
    sample_phrases: list[str] = Field(default_factory=list)
    facts: list[BrainFact] = Field(default_factory=list)
    updated_at: datetime | None = None


class BrainNiche(CompactContext):
    name: str | None = None
    keywords: list[str] = Field(default_factory=list)
    topic_facts: list[BrainFact] = Field(default_factory=list)


class BrainGoals(CompactContext):
    primary: str | None = None
    secondary: str | None = None


class BrainAudience(CompactContext):
    facts: list[BrainFact] = Field(default_factory=list)


class BrainContentPreferences(CompactContext):
    autocontent_active: bool | None = None
    posts_per_day: int | None = None
    generation_mix_30d: dict[str, int] = Field(default_factory=dict)
    facts: list[BrainFact] = Field(default_factory=list)


class BrainConstraints(CompactContext):
    voice_taboo: list[str] = Field(default_factory=list)
    neuro_active: bool | None = None
    neuro_mode: str | None = None
    neuro_daily_cap: int | None = None
    facts: list[BrainFact] = Field(default_factory=list)


class PerformanceMetrics(CompactContext):
    views: int = 0
    likes: int = 0
    replies: int = 0
    reposts: int = 0
    quotes: int = 0
    shares: int = 0


class BrainPerformance(CompactContext):
    generated_30d: int = 0
    total_posts_30d: int = 0
    published_posts_30d: int = 0
    failed_posts_30d: int = 0
    insight_posts_30d: int = 0
    metrics_30d: PerformanceMetrics | None = None
    neuro_status_30d: dict[str, int] = Field(default_factory=dict)
    facts: list[BrainFact] = Field(default_factory=list)


class BrainStrategy(CompactContext):
    autonomy_level: str | None = None
    values: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime | None = None


class BrainMemory(CompactContext):
    facts: list[BrainFact] = Field(default_factory=list)


class TaskPerformance(CompactContext):
    generated_30d: int | None = None
    published_posts_30d: int | None = None
    insight_posts_30d: int | None = None
    metrics_30d: PerformanceMetrics | None = None
    patterns: list[TaskFact] = Field(default_factory=list)


class TaskVoice(CompactContext):
    lexicon: list[str] = Field(default_factory=list)
    sentence_length: str | None = None
    punctuation: str | None = None
    tone: str | None = None
    structure: str | None = None
    taboo: list[str] = Field(default_factory=list)
    sample_phrases: list[str] = Field(default_factory=list)
    facts: list[TaskFact] = Field(default_factory=list)


class TaskNiche(CompactContext):
    name: str | None = None
    keywords: list[str] = Field(default_factory=list)


class TaskConstraints(CompactContext):
    voice_taboo: list[str] = Field(default_factory=list)
    neuro_active: bool | None = None
    neuro_mode: str | None = None
    neuro_daily_cap: int | None = None
    facts: list[TaskFact] = Field(default_factory=list)


class TaskStrategy(CompactContext):
    autonomy_level: str | None = None
    values: dict[str, Any] = Field(default_factory=dict)


class TaskContentPreferences(CompactContext):
    autocontent_active: bool | None = None
    posts_per_day: int | None = None
    generation_mix_30d: dict[str, int] = Field(default_factory=dict)
    facts: list[TaskFact] = Field(default_factory=list)


class GenerationBrainContext(CompactContext):
    voice: TaskVoice | None = None
    niche: TaskNiche | None = None
    goals: BrainGoals | None = None
    constraints: TaskConstraints | None = None
    content_patterns: list[TaskFact] = Field(default_factory=list)
    performance: TaskPerformance | None = None


class RadarBrainContext(CompactContext):
    niche: TaskNiche | None = None
    goals: BrainGoals | None = None
    audience: list[TaskFact] = Field(default_factory=list)
    topics: list[TaskFact] = Field(default_factory=list)
    performance: TaskPerformance | None = None


class NeuroBrainContext(CompactContext):
    voice: TaskVoice | None = None
    niche: TaskNiche | None = None
    audience: list[TaskFact] = Field(default_factory=list)
    constraints: TaskConstraints | None = None


class AutocontentBrainContext(CompactContext):
    voice: TaskVoice | None = None
    niche: TaskNiche | None = None
    goals: BrainGoals | None = None
    strategy: TaskStrategy | None = None
    content_preferences: TaskContentPreferences | None = None
    performance: TaskPerformance | None = None


class SocialBrainContext(CompactContext):
    user: BrainUserIdentity
    account: BrainAccountIdentity
    voice: BrainVoice = Field(default_factory=BrainVoice)
    niche: BrainNiche = Field(default_factory=BrainNiche)
    goals: BrainGoals = Field(default_factory=BrainGoals)
    audience: BrainAudience = Field(default_factory=BrainAudience)
    content_preferences: BrainContentPreferences = Field(
        default_factory=BrainContentPreferences
    )
    constraints: BrainConstraints = Field(default_factory=BrainConstraints)
    performance: BrainPerformance = Field(default_factory=BrainPerformance)
    strategy: BrainStrategy = Field(default_factory=BrainStrategy)
    memory: BrainMemory = Field(default_factory=BrainMemory)

    def _voice_for_task(self) -> TaskVoice | None:
        if not (self.voice.available or self.voice.facts):
            return None
        return TaskVoice(
            lexicon=self.voice.lexicon,
            sentence_length=self.voice.sentence_length,
            punctuation=self.voice.punctuation,
            tone=self.voice.tone,
            structure=self.voice.structure,
            taboo=self.voice.taboo,
            sample_phrases=self.voice.sample_phrases,
            facts=_task_facts([
                fact
                for fact in self.voice.facts
                if fact.key != "profile"
            ]),
        )

    def _niche_for_task(self) -> TaskNiche | None:
        if not (self.niche.name or self.niche.keywords):
            return None
        return TaskNiche(
            name=self.niche.name,
            keywords=self.niche.keywords,
        )

    def _goals_for_task(self) -> BrainGoals | None:
        if not (self.goals.primary or self.goals.secondary):
            return None
        return self.goals

    def _generation_constraints_for_task(
        self,
    ) -> TaskConstraints | None:
        constraints = TaskConstraints(
            voice_taboo=(
                []
                if self.voice.available
                else self.constraints.voice_taboo
            ),
            facts=_task_facts(self.constraints.facts),
        )
        if not constraints.compact_dict():
            return None
        return constraints

    def _neuro_constraints_for_task(self) -> TaskConstraints | None:
        constraints = TaskConstraints(
            voice_taboo=self.constraints.voice_taboo,
            neuro_active=self.constraints.neuro_active,
            neuro_mode=self.constraints.neuro_mode,
            neuro_daily_cap=self.constraints.neuro_daily_cap,
            facts=_task_facts(self.constraints.facts),
        )
        if not constraints.compact_dict():
            return None
        return constraints

    def _strategy_for_task(self) -> TaskStrategy | None:
        strategy = TaskStrategy(
            autonomy_level=self.strategy.autonomy_level,
            values=self.strategy.values,
        )
        if not strategy.compact_dict():
            return None
        return strategy

    def _content_preferences_for_task(
        self,
    ) -> TaskContentPreferences | None:
        preferences = TaskContentPreferences(
            autocontent_active=(
                self.content_preferences.autocontent_active
            ),
            posts_per_day=self.content_preferences.posts_per_day,
            generation_mix_30d=(
                self.content_preferences.generation_mix_30d
            ),
            facts=_task_facts([
                fact
                for fact in self.content_preferences.facts
                if fact.key != "generation_mix_30d"
            ]),
        )
        if not preferences.compact_dict():
            return None
        return preferences

    def _performance_for_task(self) -> TaskPerformance | None:
        performance = TaskPerformance(
            generated_30d=(
                self.performance.generated_30d or None
            ),
            published_posts_30d=(
                self.performance.published_posts_30d or None
            ),
            insight_posts_30d=(
                self.performance.insight_posts_30d or None
            ),
            metrics_30d=self.performance.metrics_30d,
            patterns=_task_facts([
                fact
                for fact in self.performance.facts
                if fact.key != "rolling_30d"
            ]),
        )
        if not performance.compact_dict():
            return None
        return performance

    def for_generation(self) -> GenerationBrainContext:
        return GenerationBrainContext(
            voice=self._voice_for_task(),
            niche=self._niche_for_task(),
            goals=self._goals_for_task(),
            constraints=self._generation_constraints_for_task(),
            content_patterns=_task_facts(
                self.content_preferences.facts
            ),
            performance=self._performance_for_task(),
        )

    def for_radar(self) -> RadarBrainContext:
        return RadarBrainContext(
            niche=self._niche_for_task(),
            goals=self._goals_for_task(),
            audience=_task_facts(self.audience.facts),
            topics=_task_facts([
                fact
                for fact in self.niche.topic_facts
                if fact.key != "niche"
            ]),
            performance=self._performance_for_task(),
        )

    def for_neuro(self) -> NeuroBrainContext:
        return NeuroBrainContext(
            voice=self._voice_for_task(),
            niche=self._niche_for_task(),
            audience=_task_facts(self.audience.facts),
            constraints=self._neuro_constraints_for_task(),
        )

    def for_autocontent(self) -> AutocontentBrainContext:
        return AutocontentBrainContext(
            voice=self._voice_for_task(),
            niche=self._niche_for_task(),
            goals=self._goals_for_task(),
            strategy=self._strategy_for_task(),
            content_preferences=self._content_preferences_for_task(),
            performance=self._performance_for_task(),
        )
