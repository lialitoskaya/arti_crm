from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Marketplace = Literal["ozon", "yandex", "wildberries", "mock"]
ChatStatus = str
TaskStatus = Literal["open", "in_progress", "done", "cancelled", "archived"]
MessageDirection = Literal["inbound", "outbound", "internal"]


class ChatCreate(BaseModel):
    marketplace: Marketplace
    external_chat_id: str
    customer_name: str | None = None
    customer_public_id: str | None = None
    order_id: str | None = None
    status: str = "new"
    assigned_to: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChatUpdate(BaseModel):
    status: str | None = Field(default=None, max_length=80)
    assigned_to: str | None = None
    assigned_user_id: int | None = None
    customer_name: str | None = None




class ChatFunnelCreate(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    sort_order: int = 0


class ChatFunnelUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    sort_order: int | None = None
    is_default: bool | None = None


class ChatStatusCreate(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    key: str | None = Field(default=None, max_length=80)
    funnel_id: int | None = None
    color: str | None = Field(default=None, max_length=40)
    sort_order: int = 0


class ChatStatusUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    funnel_id: int | None = None
    color: str | None = Field(default=None, max_length=40)
    sort_order: int | None = None
    is_active: bool | None = None


class MessageCreate(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    author: str | None = "manager"


class AiReplyCreate(BaseModel):
    message_id: int
    extra_instruction: str | None = Field(default=None, max_length=1500)


class InternalNoteCreate(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    author: str | None = "manager"


class TaskCreate(BaseModel):
    chat_id: int
    title: str = Field(min_length=1, max_length=250)
    description: str | None = None
    assignee: str | None = None
    assigned_user_id: int | None = None
    due_at: str | None = None


class TaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    status: TaskStatus | None = None
    assignee: str | None = None
    assigned_user_id: int | None = None
    due_at: str | None = None
    comment: str | None = Field(default=None, max_length=2000)
    comment_author: str | None = Field(default="manager", max_length=120)


class ReviewReplyCreate(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    mark_processed: bool = True


class QuestionAnswerCreate(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    mark_processed: bool = True


class ReviewStatusUpdate(BaseModel):
    status: str = Field(min_length=1, max_length=80)


class LoginCreate(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=500)


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=120)
    password: str = Field(min_length=6, max_length=500)
    display_name: str | None = Field(default=None, max_length=160)
    role: Literal["admin", "manager", "viewer"] = "manager"


class UserPasswordUpdate(BaseModel):
    password: str = Field(min_length=6, max_length=500)


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=160)
    role: Literal["admin", "manager", "viewer"] | None = None
    is_active: bool | None = None


class ProfileUpdate(BaseModel):
    username: str | None = Field(default=None, min_length=2, max_length=120)
    display_name: str | None = Field(default=None, max_length=160)
    current_password: str | None = Field(default=None, max_length=500)
    new_password: str | None = Field(default=None, min_length=6, max_length=500)


class KnowledgeCategoryCreate(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=1000)
    sort_order: int = 0


class KnowledgeCategoryUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=1000)
    sort_order: int | None = None


class KnowledgeArticleCreate(BaseModel):
    category_id: int | None = None
    title: str = Field(min_length=1, max_length=220)
    content: str = Field(default='', max_length=50000)
    tags: str | None = Field(default=None, max_length=1000)
    image_url: str | None = Field(default=None, max_length=2000)
    is_published: bool = True


class KnowledgeArticleUpdate(BaseModel):
    category_id: int | None = None
    title: str | None = Field(default=None, min_length=1, max_length=220)
    content: str | None = Field(default=None, max_length=50000)
    tags: str | None = Field(default=None, max_length=1000)
    image_url: str | None = Field(default=None, max_length=2000)
    clear_image: bool | None = None
    is_published: bool | None = None
