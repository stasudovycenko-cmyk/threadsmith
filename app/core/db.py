"""
БД без ORM-моделей: SQLAlchemy Core + сырые запросы через text().
Обоснование: схема уже описана SQL-миграцией, дублировать её в
декларативных моделях на MVP - лишняя работа и лишний слой расхождений.
Когда (если) проект вырастет - затащим модели постепенно.
"""
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.core.config import settings

engine = create_async_engine(settings.DATABASE_URL, pool_size=10, max_overflow=5)
Session = async_sessionmaker(engine, expire_on_commit=False)
