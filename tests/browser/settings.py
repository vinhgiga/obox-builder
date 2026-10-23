from tests.support.comments_settings import Settings as CommentsSettings


class Settings(CommentsSettings):
    INSTALLED_APPS: list[str] = [
        *CommentsSettings.model_fields["INSTALLED_APPS"].default,
        "obox_builder.contrib.jobs",
    ]
