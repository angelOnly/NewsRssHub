"""Explicit runtime loading for project-owned curation policy."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.config import Settings


class SkillUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SkillStatus:
    available: bool
    path: Path
    message: str


class SkillLoader:
    def __init__(self, settings: Settings) -> None:
        self.path = settings.root_dir / ".agents" / "skills" / "curate-personal-news" / "SKILL.md"
        self._cached_mtime_ns: int | None = None
        self._cached_content: str | None = None

    def status(self) -> SkillStatus:
        if not self.path.exists():
            return SkillStatus(False, self.path, "项目筛选 Skill 缺失，资讯不会进入语义筛选。")
        try:
            self.load()
        except SkillUnavailableError as exc:
            return SkillStatus(False, self.path, str(exc))
        return SkillStatus(True, self.path, "项目筛选 Skill 已加载。")

    def load(self) -> str:
        if not self.path.exists():
            raise SkillUnavailableError("项目筛选 Skill 缺失，无法安全执行资讯筛选。")
        try:
            mtime_ns = self.path.stat().st_mtime_ns
            if self._cached_content is not None and self._cached_mtime_ns == mtime_ns:
                return self._cached_content
            content = self.path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SkillUnavailableError("项目筛选 Skill 无法读取。") from exc
        if not content:
            raise SkillUnavailableError("项目筛选 Skill 为空，无法安全执行资讯筛选。")
        self._cached_mtime_ns = mtime_ns
        self._cached_content = content
        return content
