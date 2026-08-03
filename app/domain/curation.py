"""Typed contract for the project news-curation Skill.

The model is allowed to decide only grouping, tier, reason and ordering.  The
application owns validation, item persistence and every database mutation.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EditorialTier(StrEnum):
    MUST_READ = "must_read"
    IMPORTANT = "important"
    BRIEF = "brief"
    HIDDEN = "hidden"
    PENDING = "pending"


class SummaryStatus(StrEnum):
    PENDING = "pending"
    COMPLETE = "complete"
    RETRY = "retry"
    FAILED = "failed"


class CurationStatus(StrEnum):
    PENDING = "pending"
    COMPLETE = "complete"
    RETRY = "retry"
    FAILED = "failed"


class CurationGroup(BaseModel):
    """One event group returned by the model for a bounded summary batch."""

    model_config = ConfigDict(extra="forbid")

    item_ids: list[int] = Field(min_length=1, max_length=50)
    primary_item_id: int
    tier: EditorialTier
    reason: str = Field(min_length=1, max_length=240)
    order: int = Field(ge=1, le=10_000)

    @field_validator("item_ids")
    @classmethod
    def no_duplicate_item_ids(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)):
            raise ValueError("item_ids 不能重复")
        return value

    @model_validator(mode="after")
    def primary_must_belong_to_group(self) -> "CurationGroup":
        if self.primary_item_id not in self.item_ids:
            raise ValueError("primary_item_id 必须在 item_ids 中")
        return self


class CurationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    groups: list[CurationGroup] = Field(min_length=1, max_length=50)
