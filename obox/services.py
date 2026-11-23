"""Application services share the caller's session and transaction."""

from sqlalchemy.ext.asyncio import AsyncSession

from obox.core.principal import Identity


class BaseService:
    def __init__(self, session: AsyncSession, principal: Identity) -> None:
        self.session, self.principal = session, principal
