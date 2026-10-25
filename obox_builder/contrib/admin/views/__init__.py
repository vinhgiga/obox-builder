from obox_builder.contrib.admin.views.audit import (
    history_change_set,
    history_change_set_revert,
    history_export,
    history_feed,
)
from obox_builder.contrib.admin.views.auth import (
    login_submit,
    login_view,
    logout_view,
    password_change_submit,
    password_change_view,
)
from obox_builder.contrib.admin.views.dashboard import dashboard
from obox_builder.contrib.admin.views.password_reset import (
    password_reset_confirm_submit,
    password_reset_confirm_view,
    password_reset_request_submit,
    password_reset_request_view,
)
from obox_builder.contrib.admin.views.roles import (
    role_create,
    role_create_submit,
    role_delete,
    role_edit,
    role_edit_submit,
    role_history,
    role_list,
)
from obox_builder.contrib.admin.views.users import (
    user_create,
    user_create_submit,
    user_edit,
    user_edit_submit,
    user_history,
    user_list,
)

__all__ = [
    "dashboard",
    "history_change_set",
    "history_change_set_revert",
    "history_export",
    "history_feed",
    "login_submit",
    "login_view",
    "logout_view",
    "password_change_submit",
    "password_change_view",
    "password_reset_confirm_submit",
    "password_reset_confirm_view",
    "password_reset_request_submit",
    "password_reset_request_view",
    "role_create",
    "role_create_submit",
    "role_delete",
    "role_edit",
    "role_edit_submit",
    "role_history",
    "role_list",
    "user_create",
    "user_create_submit",
    "user_edit",
    "user_edit_submit",
    "user_history",
    "user_list",
]
