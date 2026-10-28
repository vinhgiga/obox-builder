"""Pydantic schemas for auth/admin forms."""

from __future__ import annotations

from pydantic import BaseModel, Field


class LoginForm(BaseModel):
    username: str
    password: str
    next: str = ""


class PasswordChangeForm(BaseModel):
    current_password: str
    new_password: str
    confirm_password: str

    def passwords_match(self) -> bool:
        return self.new_password == self.confirm_password


class UserForm(BaseModel):
    username: str
    email: str = ""
    full_name: str = ""
    password: str = ""
    is_active: bool = True
    is_superuser: bool = False
    role_ids: list[int] = Field(default_factory=list)


class RoleForm(BaseModel):
    name: str
    permission_ids: list[int] = Field(default_factory=list)
