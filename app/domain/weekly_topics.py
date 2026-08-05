"""本周话题归并的受限模型输出契约。"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator


_TOPIC_REFERENCE = re.compile(r"(?:existing|new):[1-9]\d*")

# 单条内容仍是正常事件；累计两条可见内容后才属于“热点”。
MIN_WEEKLY_TOPIC_CONTENT_COUNT = 2


class WeeklyTopicGroup(BaseModel):
    """一个本周话题及其事件归属。

    ``ref`` 只能引用调用方提供的既有 ID，或使用临时 ``new`` 引用。
    数据库 ID 始终由服务层创建，不能由模型伪造。
    """

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=5, max_length=32)
    display_name: str = Field(min_length=2, max_length=80)
    event_ids: list[int] = Field(min_length=1, max_length=120)

    @field_validator("ref")
    @classmethod
    def valid_reference(cls, value: str) -> str:
        if not _TOPIC_REFERENCE.fullmatch(value):
            raise ValueError("ref 必须是 existing:数字 或 new:数字")
        return value

    @field_validator("display_name")
    @classmethod
    def compact_display_name(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 2:
            raise ValueError("display_name 不能为空")
        return normalized

    @field_validator("event_ids")
    @classmethod
    def no_duplicate_event_ids(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)):
            raise ValueError("event_ids 不能重复")
        return value


class WeeklyTopicOutput(BaseModel):
    """模型必须完整覆盖当前周所有可见候选事件。"""

    model_config = ConfigDict(extra="forbid")

    topics: list[WeeklyTopicGroup] = Field(min_length=1, max_length=120)
