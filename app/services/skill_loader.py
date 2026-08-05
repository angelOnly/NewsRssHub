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
    def __init__(
        self,
        settings: Settings,
        *,
        skill_name: str = "curate-personal-news",
        display_name: str = "项目筛选",
    ) -> None:
        self.path = settings.root_dir / ".agents" / "skills" / skill_name / "SKILL.md"
        self.display_name = display_name
        self._cached_mtime_ns: int | None = None
        self._cached_content: str | None = None

    def status(self) -> SkillStatus:
        if not self.path.exists():
            return SkillStatus(False, self.path, f"{self.display_name} Skill 缺失，无法安全完成语义判断。")
        try:
            self.load()
        except SkillUnavailableError as exc:
            return SkillStatus(False, self.path, str(exc))
        return SkillStatus(True, self.path, f"{self.display_name} Skill 已加载。")

    def load(self) -> str:
        if not self.path.exists():
            raise SkillUnavailableError(f"{self.display_name} Skill 缺失，无法安全执行语义判断。")
        try:
            mtime_ns = self.path.stat().st_mtime_ns
            if self._cached_content is not None and self._cached_mtime_ns == mtime_ns:
                return self._cached_content
            content = self.path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SkillUnavailableError(f"{self.display_name} Skill 无法读取。") from exc
        if not content:
            raise SkillUnavailableError(f"{self.display_name} Skill 为空，无法安全执行语义判断。")
        self._cached_mtime_ns = mtime_ns
        self._cached_content = content
        return content
