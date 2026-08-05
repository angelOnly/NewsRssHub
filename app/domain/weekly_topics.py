"""今日话题增量归并的受限模型输出契约。"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_TOPIC_REFERENCE = re.compile(r"(?:existing|new):[1-9]\d*")

# 单条内容也会先获得当天的话题归属；累计两条可见内容后才在页面展示为热点。
MIN_DAILY_TOPIC_CONTENT_COUNT = 2


class DailyTopicGroup(BaseModel):
    """一组当天新事件的归属。

    ``existing:数字`` 只用于加入当天已有话题，不允许模型改名；
    ``new:数字`` 是一次请求内的临时分组引用，必须附带新话题名称。
    """

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=5, max_length=32)
    display_name: str | None = Field(default=None, min_length=2, max_length=80)
    event_ids: list[int] = Field(min_length=1, max_length=120)

    @field_validator("ref")
    @classmethod
    def valid_reference(cls, value: str) -> str:
        if not _TOPIC_REFERENCE.fullmatch(value):
            raise ValueError("ref 必须是 existing:数字 或 new:数字")
        return value

    @field_validator("display_name")
    @classmethod
    def compact_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if len(normalized) < 2:
            raise ValueError("display_name 不能为空")
        return normalized

    @model_validator(mode="after")
    def validate_display_name_for_reference(self) -> "DailyTopicGroup":
        if self.ref.startswith("new:") and not self.display_name:
            raise ValueError("新建今日话题必须提供 display_name")
        if self.ref.startswith("existing:") and self.display_name is not None:
            raise ValueError("已有今日话题不能在增量归并中改名")
        return self

    @field_validator("event_ids")
    @classmethod
    def no_duplicate_event_ids(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)):
            raise ValueError("event_ids 不能重复")
        return value


class DailyTopicOutput(BaseModel):
    """模型必须完整覆盖本次尚未分配的当天事件。"""

    model_config = ConfigDict(extra="forbid")

    topics: list[DailyTopicGroup] = Field(min_length=1, max_length=120)


# 旧模块名曾随“本周热点”一起发布。保留这几个别名只为让已安装的本地扩展
# 在升级后不立刻导入失败；应用主链路已经统一使用 DailyTopic* 命名。
MIN_WEEKLY_TOPIC_CONTENT_COUNT = MIN_DAILY_TOPIC_CONTENT_COUNT
WeeklyTopicGroup = DailyTopicGroup
WeeklyTopicOutput = DailyTopicOutput
