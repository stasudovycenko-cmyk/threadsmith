"""Russian user-facing copy for machine-readable decisions."""

from app.core.autopilot_intelligence.models import ActionType, DecisionStatus

REASON_MESSAGES = {
    "NO_TOKEN": "Нужно переподключить Threads-аккаунт.",
    "TOKEN_EXPIRED": "Срок подключения Threads истёк.",
    "ACCOUNT_DISCONNECTED": "Threads-аккаунт сейчас не подключён.",
    "NO_CREDITS": "На балансе недостаточно кредитов для новых материалов.",
    "LOW_CREDITS": "Кредиты заканчиваются.",
    "QUEUE_EMPTY": "Очередь публикаций закончилась.",
    "QUEUE_LOW": "В очереди осталось мало публикаций.",
    "QUEUE_FULL": "Очередь заполнена с запасом.",
    "QUEUE_HEALTHY": "Очередь заполнена в соответствии с планом.",
    "AUTOPILOT_DISABLED": "Автопилот выключен для этого аккаунта.",
    "ANALYTICS_UNAVAILABLE": "Пока недостаточно статистики для выводов.",
    "ANALYTICS_DELAYED": "Статистика давно не обновлялась.",
    "ANALYTICS_COLLECTING": "Статистика ещё накапливается.",
    "LOW_ENGAGEMENT": "Последние результаты слабее ожидаемых.",
    "LOW_PERFORMANCE": "Общая оценка последних публикаций снизилась.",
    "GOOD_TOPIC_FOUND": "Найдена тема с устойчиво хорошими результатами.",
    "RADAR_DISABLED": "Поиск новых обсуждений пока не настроен.",
    "RADAR_DELAYED": "Пора обновить поиск новых обсуждений.",
    "RADAR_FAILED": "Последний поиск обсуждений завершился с ошибкой.",
    "HOT_TOPIC_FOUND": "Найдена перспективная тема для реакции.",
    "NEURO_DISABLED": "Подготовка комментариев выключена.",
    "NEURO_QUEUE_READY": "Есть комментарии, ожидающие проверки.",
    "NEURO_LIMIT_REACHED": "Дневной лимит комментариев достигнут.",
    "NEURO_FAILED": "Часть комментариев не удалось подготовить.",
    "PUBLISH_FAILED": "Сегодня была неудачная публикация.",
    "RECOVERY_REQUIRED": "Нужно проверить незавершённую публикацию.",
    "PERMISSION_DENIED": "Threads не разрешил выполнить операцию.",
    "SCHEDULE_NOT_CONFIGURED": "Расписание публикаций не настроено.",
    "TOPICS_NOT_CONFIGURED": "Для Автопилота не выбраны темы.",
    "BRAIN_UNAVAILABLE": "Персональные закономерности ещё не собраны.",
    "BRAIN_COLLECTING": "Автопилот продолжает изучать результаты.",
    "BRAIN_READY": "Персональные закономерности готовы к использованию.",
    "SUBSCRIPTION_INACTIVE": "Подписка неактивна.",
    "SYSTEM_HEALTH_LOW": "Несколько важных частей системы требуют внимания.",
    "SYSTEM_HEALTH_WARNING": "Есть параметры, которые стоит проверить.",
    "SYSTEM_HEALTHY": "Всё работает, срочных действий нет.",
}

ACTION_LABELS = {
    ActionType.NONE: "Ничего менять не нужно",
    ActionType.RECONNECT_ACCOUNT: "Переподключить аккаунт",
    ActionType.OPEN_BALANCE: "Проверить баланс",
    ActionType.OPEN_QUEUE: "Открыть очередь",
    ActionType.OPEN_RECOVERY: "Проверить историю публикаций",
    ActionType.OPEN_RADAR: "Открыть найденные обсуждения",
    ActionType.OPEN_NEURO: "Проверить подготовленные комментарии",
    ActionType.OPEN_ANALYTICS: "Открыть аналитику",
    ActionType.OPEN_SCHEDULE: "Проверить настройки Автопилота",
}

STATUS_LABELS = {
    DecisionStatus.HEALTHY: "Всё работает",
    DecisionStatus.ATTENTION: "Требуется внимание",
    DecisionStatus.BLOCKED: "Работа заблокирована",
    DecisionStatus.WAITING: "Автопилот ожидает",
    DecisionStatus.INSUFFICIENT_DATA: "Недостаточно статистики",
}


def reason_message(code: str) -> str:
    return REASON_MESSAGES.get(code, "Состояние аккаунта изменилось.")
