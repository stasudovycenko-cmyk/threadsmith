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
