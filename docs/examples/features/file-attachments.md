# Add file attachments

The Files plugin stores upload metadata and associates files with an explicit
model field. Declare the allowed media types and collection size:

```python
import setting

from obox_builder import Obox
from obox_builder.contrib.auth import Auth
from obox_builder.contrib.files import File, Files, IMAGES, attachments
from sqlalchemy.orm import Mapped


class Article(Base):
    images: Mapped[list[File]] = attachments(types=IMAGES, max_count=8)


obox = Obox(
    app,
    settings="setting",
    plugins=(Auth(), Files(root=setting.BASE_DIR / "uploads")),
)
```

Declare Files in the Obox constructor so it is installed before app builders
load. Include the Files model metadata in the host's Alembic migrations, and use
persistent storage when local disk does not survive restarts.

See [Activity and files](../../features/activity-files.md) for authorization,
cleanup, and upload API details.
