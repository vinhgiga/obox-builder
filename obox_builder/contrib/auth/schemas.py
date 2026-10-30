"""Typed inputs for account administration through any adapter."""

from pydantic import BaseModel, ConfigDict, Field


class UserInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    username: str = Field(min_length=1, max_length=4096)
    password: str = Field(min_length=1, max_length=4096)
    email: str = Field(default="", max_length=254)
    full_name: str = Field(default="", max_length=255)
    is_active: bool = True
    is_superuser: bool = False
    role_ids: list[str] = Field(default_factory=list, max_length=200)


class UserPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    password: str = Field(default="", min_length=1, max_length=4096)
    email: str = Field(default="", max_length=254)
    full_name: str = Field(default="", max_length=255)
    is_active: bool = True
    is_superuser: bool = False
    role_ids: list[str] = Field(default_factory=list, max_length=200)


class RoleInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(default="", min_length=1, max_length=150)
    permission_ids: list[str] = Field(default_factory=list, max_length=1000)


class RoleCreate(RoleInput):
    name: str = Field(min_length=1, max_length=150)
