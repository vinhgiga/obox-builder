# File attachments

```python
from sqlalchemy.orm import Mapped
from obox.contrib.files import File, IMAGES, attachments
from models import Base


class Article(Base):
    # Supply a table name, primary key and other mapped columns.
    __abstract__ = True
    images: Mapped[list[File]] = attachments(types=IMAGES, max_count=8)
```

Enable `obox.contrib.resources`, `obox.contrib.auth` and `obox.contrib.files`,
then set MEDIA_ROOT/FILES in settings. Add attachment fields to the contrib
Resource declaration; its editor handles upload and claiming. Files tables must
be included in Alembic migrations. Use an application-owned persistent directory.
See [activity and files](../../features/activity-files.md).
