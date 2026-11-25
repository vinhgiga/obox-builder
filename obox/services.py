"""Database services participate in the caller's unit of work."""

from sqlalchemy.ext.asyncio import AsyncSession

from obox.core.principal import Identity


class BaseService:
    """Business operations may flush. The unit of work owns the commit."""

    def __init__(self, session: AsyncSession, principal: Identity) -> None:
        self.session = session
        self.principal = principal
