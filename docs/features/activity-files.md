# Activity and files

Activity and file handling are opt-in plugins. Each uses the current resource
permissions and host identity rather than accepting a caller-supplied owner.

## Activity

Install `Activity()` to record resource revisions and expose activity and
comment routes. The plugin provides a combined timeline, plain-text comments,
cursor pagination, and conditional edits for a comment's author. Parent record
access and the configured comment permissions apply to nested routes.

```python
from obox_builder import Obox
from obox_builder.contrib.activity import Activity
from obox_builder.contrib.auth import Auth

obox = Obox(app, settings="setting", plugins=(Auth(), Activity()))
```

The plugin's tables must be included in the host's database migrations. Models
using SQLAlchemy versioning need the comment version migration before enabling
versioned comment updates. The [Record and Activity API](../record-activity-spec.md)
documents routes and database compatibility requirements.

## Files

Install `Files` with an application-owned storage directory and declare an
attachment collection on a model:

```python
from sqlalchemy.orm import Mapped

from obox_builder.contrib.files import File, IMAGES, attachments
from models import Base


class Article(Base):
    # Other mapped columns omitted.
    images: Mapped[list[File]] = attachments(types=IMAGES, max_count=8)
```

Uploads are resumable, associated with a model field, and authorized on read.
Unclaimed uploads are collected after their configured age. Choose persistent
storage for deployments where local disk is ephemeral, and include the Files
tables in the host's migrations.

```python
import setting
from obox_builder import Obox
from obox_builder.contrib.auth import Auth
from obox_builder.contrib.files import Files

obox = Obox(
    app,
    settings="setting",
    plugins=(Auth(), Files(root=setting.BASE_DIR / "uploads")),
)
```

Install Files during application composition, before resource builders are
loaded. The field accepts an ordered list of ready file IDs on writes. MIME
type, size, count, owner, and field constraints are checked by the plugin. See the [API
contract](../api-spec.md#file-uploads-files-plugin) for upload and metadata
routes.
