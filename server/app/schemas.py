from pydantic import BaseModel, Field, field_validator
from datetime import datetime


class GuestRegister(BaseModel):
    invite_code: str = Field(min_length=6, max_length=200)
    guest_id: str = Field(min_length=1, max_length=24, pattern=r"^[^\x00-\x1f\x7f]+$")

    @field_validator("guest_id", mode="before")
    @classmethod
    def trim_guest_id(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else value


class GuestLogin(BaseModel):
    invite_code: str = Field(min_length=6, max_length=200)
    guest_id: str = Field(min_length=1, max_length=24, pattern=r"^[^\x00-\x1f\x7f]+$")

    @field_validator("guest_id", mode="before")
    @classmethod
    def trim_guest_id(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else value


class AdminLogin(BaseModel):
    username: str = Field(min_length=1, max_length=48)
    password: str = Field(min_length=1, max_length=200)

    @field_validator("username", mode="before")
    @classmethod
    def trim_username(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else value


class AdminRegister(BaseModel):
    invite_code: str = Field(min_length=6, max_length=200)
    username: str = Field(min_length=1, max_length=48)
    password: str = Field(min_length=4, max_length=200)

    @field_validator("username", mode="before")
    @classmethod
    def trim_username(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else value


class PasswordReset(BaseModel):
    reset_code: str = Field(min_length=12, max_length=200)
    new_password: str = Field(min_length=4, max_length=200)


class ProfileUpdate(BaseModel):
    username: str = Field(min_length=1, max_length=48)
    current_password: str | None = Field(default=None, max_length=200)
    new_password: str | None = Field(default=None, min_length=4, max_length=200)

    @field_validator("username", mode="before")
    @classmethod
    def trim_username(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else value


class AccountUpdate(BaseModel):
    username: str | None = Field(default=None, min_length=1, max_length=48)
    new_password: str | None = Field(default=None, min_length=4, max_length=200)

    @field_validator("username", mode="before")
    @classmethod
    def trim_username(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else value


class InviteCreate(BaseModel):
    max_uses: int = Field(default=1, ge=1, le=100)


class AdminInviteCreate(BaseModel):
    max_uses: int = Field(default=1, ge=1, le=100)
    expires_at: datetime | None = None


class RoomCreate(BaseModel):
    name: str = Field(min_length=1, max_length=48)
    description: str = Field(default="", max_length=200)


class RoomUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=48)
    description: str | None = Field(default=None, max_length=200)
    archived: bool | None = None
