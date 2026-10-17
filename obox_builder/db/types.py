from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

TZDateTime = sa.DateTime(timezone=True)
JSONB = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

# SQLite only auto-increments INTEGER primary keys, so big integers degrade there.
BigIntIdentity = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

JsonDict = dict[str, Any]
