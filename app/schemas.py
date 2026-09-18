from __future__ import annotations

from pydantic import BaseModel, Field


class PoolCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    cidr: str


class UnavailableCreate(BaseModel):
    cidr: str


class AllocRequest(BaseModel):
    prefix_len: int
    count: int = Field(default=1, ge=1, le=256)


class AllocateBody(BaseModel):
    """批量申请：同一事务内全部成功，否则整体回滚。"""

    requests: list[AllocRequest] = Field(min_length=1, max_length=256)


class ReleaseBody(BaseModel):
    cidr: str


class ResizeBody(BaseModel):
    """原子调整已分配网段：缩小保留原网络地址，扩展取唯一包含原网段的父网。"""

    cidr: str
    new_prefix_len: int
