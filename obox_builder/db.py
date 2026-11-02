"""Database ownership shared by HTTP applications and standalone commands."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from obox_builder.conf import Settings
from obox_builder.errors import ImproperlyConfigured


class Database:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.sessions = settings.session_factory or async_sessionmaker(expire_on_commit=False)
        self.engine: AsyncEngine | None = None
        self._active = False

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        if self._active:
            raise ImproperlyConfigured("Database lifecycle is already active.")
        self._active = True
        try:
            if self.settings.database_url is not None:
                self.engine = create_async_engine(self.settings.database_url)
                self.sessions.configure(bind=self.engine)
            yield
        finally:
            try:
                if self.engine is not None:
                    await self.engine.dispose()
            finally:
                if self.settings.database_url is not None:
                    self.sessions.configure(bind=None)
                self.engine = None
                self._active = False

    def check(self) -> None:
        if self.settings.database_url is not None and not self._active:
            raise ImproperlyConfigured(
                "Start the database lifecycle before opening a unit of work."
            )
