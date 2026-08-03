"""
Кредиты. Правила:
- Источник правды - credits_ledger. users.credits_balance - кеш для скорости.
- Списание атомарное: UPDATE ... WHERE balance >= cost. Никаких
  "прочитал -> проверил -> записал" - словим гонку на двух параллельных
  генерациях и уйдём в минус.
- Любое движение пишется в ledger. Разбор полётов с юзером "куда делись
  кредиты" без ledger превращается в ад.
"""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class NotEnoughCredits(Exception):
    pass


async def spend(session: AsyncSession, user_id: int, cost: int, reason: str) -> int:
    """Атомарно списывает cost кредитов. Возвращает новый баланс."""
    row = (await session.execute(text("""
        UPDATE users SET credits_balance = credits_balance - :cost
        WHERE id = :uid AND credits_balance >= :cost
        RETURNING credits_balance
    """), {"cost": cost, "uid": user_id})).first()
    if row is None:
        raise NotEnoughCredits()
    await session.execute(text("""
        INSERT INTO credits_ledger (user_id, delta, reason)
        VALUES (:uid, :delta, :reason)
    """), {"uid": user_id, "delta": -cost, "reason": reason})
    return row[0]


async def spend_once(
    session: AsyncSession,
    user_id: int,
    account_id: int,
    cost: int,
    reason: str,
    operation_key: str,
) -> bool:
    """Charge once per durable operation key in the caller's transaction."""
    event = (
        await session.execute(
            text("""
                INSERT INTO ai_credit_events (
                  operation_key, user_id, threads_account_id,
                  feature, credits
                )
                SELECT :operation_key, account.user_id, account.id,
                       :feature, :credits
                FROM threads_accounts account
                WHERE account.id = :account_id
                  AND account.user_id = :user_id
                ON CONFLICT (operation_key) DO NOTHING
                RETURNING operation_key
            """),
            {
                "operation_key": operation_key,
                "user_id": user_id,
                "account_id": account_id,
                "feature": reason,
                "credits": cost,
            },
        )
    ).first()
    if event is None:
        return False
    try:
        await spend(session, user_id, cost, reason)
    except Exception:
        await session.execute(
            text("""
                DELETE FROM ai_credit_events
                WHERE operation_key = :operation_key
                  AND user_id = :user_id
                  AND threads_account_id = :account_id
            """),
            {
                "operation_key": operation_key,
                "user_id": user_id,
                "account_id": account_id,
            },
        )
        raise
    return True


async def topup(session: AsyncSession, user_id: int, amount: int, reason: str) -> int:
    row = (await session.execute(text("""
        UPDATE users SET credits_balance = credits_balance + :amount
        WHERE id = :uid
        RETURNING credits_balance
    """), {"amount": amount, "uid": user_id})).first()
    await session.execute(text("""
        INSERT INTO credits_ledger (user_id, delta, reason)
        VALUES (:uid, :delta, :reason)
    """), {"uid": user_id, "delta": amount, "reason": reason})
    return row[0]


async def balance(session: AsyncSession, user_id: int) -> int:
    row = (await session.execute(text(
        "SELECT credits_balance FROM users WHERE id = :uid"
    ), {"uid": user_id})).first()
    return row[0] if row else 0
