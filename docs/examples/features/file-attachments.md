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

Enable `obox.contrib.auth` and `obox.contrib.files`, then configure `FILES` with
an application-owned persistent directory. Include the file tables in Alembic
migrations and add the attachment field to the resource's explicit `fields`.
Grant `files.access_files` along with the applicable model permissions. The
generated form and API use the same attachment ownership and persistence rules.
See [activity and files](../../features/activity-files.md).
