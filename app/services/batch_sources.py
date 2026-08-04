"""批量来源文件导入，不把连接器细节暴露给 Web 页面。"""

from __future__ import annotations

from dataclasses import dataclass, field
from textwrap import dedent
from typing import Any
from urllib.parse import urlparse

import yaml

from app.domain.models import SourceDraft, SourceKind
from app.services.sources import SourceService


@dataclass(frozen=True, slots=True)
class BatchSourceRow:
    line_number: int
    name: str
    locator: str
    message: str = ""


@dataclass(frozen=True, slots=True)
class BatchSourceInput:
    """一条已通过 YAML 结构校验、尚未写入数据库的来源。"""

    line_number: int
    draft: SourceDraft


@dataclass(slots=True)
class BatchImportResult:
    added: list[BatchSourceRow] = field(default_factory=list)
    duplicates: list[BatchSourceRow] = field(default_factory=list)
    errors: list[BatchSourceRow] = field(default_factory=list)

    @property
    def received_count(self) -> int:
        return len(self.added) + len(self.duplicates) + len(self.errors)


class BatchSourceImportService:
    """校验批量来源并只写入规范化后的来源记录。

    导入从不主动测试或抓取内容：X 账号可先导入，再由用户配置 Cookie 后
    手动测试；其他平台也保持相同的可控流程。
    """

    # 导出的全部来源也要能一次回传；1 MB 文件上限仍限制了异常大请求。
    MAX_ROWS = 1000
    MAX_UPLOAD_BYTES = 1_000_000
    DEFAULT_INTERVALS = {
        SourceKind.X_RSSHUB: 60,
        SourceKind.REDDIT: 120,
        SourceKind.YOUTUBE: 360,
        SourceKind.RSS: 120,
    }

    def __init__(self, sources: SourceService) -> None:
        self.sources = sources

    @staticmethod
    def yaml_template() -> str:
        """返回与导入器同源维护的可下载 YAML 示例。"""

        return dedent(
            """\
            # NewsRSSHub 批量来源导入文件
            # 一个文件可混合 X、Reddit、YouTube 和 RSS；只保留你要导入的条目。
            # kind 只能是：x_rsshub、reddit、youtube、rss。
            # X 账号可以先导入，测试前再到“设置与连接”验证 Cookie。

            defaults:
              official: true
              enabled: true

            sources:
              - name: "替换成 X 账号显示名称"
                kind: x_rsshub
                locator: "@替换成 X 账号"
                poll_interval_minutes: 60
                # archived: false  # 导出的备份会携带该状态；归档来源导入后仍保持归档

              # 需要添加 YouTube、Reddit 或 RSS 时，复制下面对应区块并取消注释。
              # - name: "替换成 YouTube 频道名称"
              #   kind: youtube
              #   locator: "@频道名、频道主页或 UC 开头的频道 ID"
              #   poll_interval_minutes: 360

              # - name: "替换成 Reddit 社区名称"
              #   kind: reddit
              #   locator: "r/社区名"
              #   poll_interval_minutes: 120

              # - name: "替换成 RSS 来源名称"
              #   kind: rss
              #   locator: "https://example.com/feed.xml"
              #   poll_interval_minutes: 120
            """
        )

    def import_text(
        self,
        *,
        kind: SourceKind | str,
        entries: str,
        is_official: bool = False,
        poll_interval_minutes: int = 60,
        enabled: bool = True,
    ) -> BatchImportResult:
        """保留给已有调用方的文本导入入口，Web 页面改用 YAML 上传。"""

        source_kind = SourceKind(kind)
        interval = max(5, min(int(poll_interval_minutes), 1440))
        rows = [
            BatchSourceInput(
                line_number=line_number,
                draft=SourceDraft(
                    name=name,
                    kind=source_kind,
                    locator=locator,
                    is_official=is_official,
                    poll_interval_minutes=interval,
                    enabled=enabled,
                ),
            )
            for line_number, name, locator in self._parse_rows(source_kind, entries)
        ]
        return self._import_drafts(rows)

    def import_yaml(self, content: str) -> BatchImportResult:
        """导入一个可混合平台的 YAML 来源文件。"""

        if not content.strip():
            raise ValueError("上传的 YAML 文件为空。")
        try:
            document = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            raise ValueError(f"YAML 格式错误：{exc}") from exc
        if not isinstance(document, dict):
            raise ValueError("YAML 根节点必须是包含 sources 的对象。")

        defaults = document.get("defaults") or {}
        if not isinstance(defaults, dict):
            raise ValueError("defaults 必须是对象；可删除这一段并在每条来源中单独填写。")
        raw_sources = document.get("sources")
        if not isinstance(raw_sources, list) or not raw_sources:
            raise ValueError("YAML 中必须提供至少一条 sources。")
        if len(raw_sources) > self.MAX_ROWS:
            raise ValueError(f"一次最多添加 {self.MAX_ROWS} 条来源，请拆分 YAML 文件。")

        rows: list[BatchSourceInput] = []
        parse_errors: list[BatchSourceRow] = []
        for line_number, raw_source in enumerate(raw_sources, start=1):
            fallback_name, fallback_locator = self._yaml_row_identity(raw_source)
            try:
                rows.append(self._parse_yaml_source(line_number, raw_source, defaults))
            except ValueError as exc:
                parse_errors.append(
                    BatchSourceRow(
                        line_number=line_number,
                        name=fallback_name,
                        locator=fallback_locator,
                        message=str(exc),
                    )
                )
        return self._import_drafts(rows, initial_errors=parse_errors)

    def _import_drafts(
        self,
        rows: list[BatchSourceInput],
        *,
        initial_errors: list[BatchSourceRow] | None = None,
    ) -> BatchImportResult:
        if len(rows) + len(initial_errors or []) > self.MAX_ROWS:
            raise ValueError(f"一次最多添加 {self.MAX_ROWS} 条来源，请拆分文件。")

        result = BatchImportResult(errors=list(initial_errors or []))
        seen: set[tuple[str, str]] = set()
        for item in rows:
            fallback = BatchSourceRow(
                line_number=item.line_number,
                name=item.draft.name,
                locator=item.draft.locator,
            )
            try:
                normalized, feed_url = self.sources.prepare_draft(item.draft)
                key = (normalized.kind.value, normalized.locator)
                row = BatchSourceRow(
                    line_number=item.line_number,
                    name=normalized.name,
                    locator=normalized.locator,
                )
                if key in seen:
                    result.duplicates.append(
                        BatchSourceRow(
                            line_number=row.line_number,
                            name=row.name,
                            locator=row.locator,
                            message="与本次文件中的另一条来源重复",
                        )
                    )
                    continue
                seen.add(key)
                if self.sources.repository.find_source(*key):
                    result.duplicates.append(
                        BatchSourceRow(
                            line_number=row.line_number,
                            name=row.name,
                            locator=row.locator,
                            message="已经存在于来源库",
                        )
                    )
                    continue
                self.sources.repository.create_source(normalized, feed_url)
                result.added.append(row)
            except Exception as exc:
                result.errors.append(
                    BatchSourceRow(
                        line_number=fallback.line_number,
                        name=fallback.name,
                        locator=fallback.locator,
                        message=str(exc),
                    )
                )
        return result

    def _parse_yaml_source(
        self,
        line_number: int,
        raw_source: Any,
        defaults: dict[str, Any],
    ) -> BatchSourceInput:
        if not isinstance(raw_source, dict):
            raise ValueError("每条 sources 必须是包含 name、kind、locator 的对象。")

        raw_kind = raw_source.get("kind")
        try:
            kind = SourceKind(str(raw_kind).strip())
        except ValueError as exc:
            raise ValueError("kind 只能是 x_rsshub、reddit、youtube 或 rss。") from exc

        raw_locator = raw_source.get("locator", raw_source.get("url", ""))
        if not isinstance(raw_locator, str) or not raw_locator.strip():
            raise ValueError("locator 必须是非空字符串。")
        locator = raw_locator.strip()

        raw_name = raw_source.get("name")
        if raw_name is not None and not isinstance(raw_name, str):
            raise ValueError("name 必须是字符串。")
        name = raw_name.strip() if raw_name else self._default_name(kind, locator)
        if not name:
            raise ValueError("name 不能为空。")

        official_value = raw_source.get(
            "official",
            raw_source.get("is_official", defaults.get("official", defaults.get("is_official", False))),
        )
        enabled_value = raw_source.get("enabled", defaults.get("enabled", True))
        archived_value = raw_source.get("archived", defaults.get("archived", False))
        interval_value = raw_source.get(
            "poll_interval_minutes",
            defaults.get("poll_interval_minutes", self.DEFAULT_INTERVALS[kind]),
        )
        return BatchSourceInput(
            line_number=line_number,
            draft=SourceDraft(
                name=name[:120],
                kind=kind,
                locator=locator,
                is_official=self._parse_bool(official_value, "official"),
                poll_interval_minutes=self._parse_interval(interval_value),
                enabled=self._parse_bool(enabled_value, "enabled"),
                archived=self._parse_bool(archived_value, "archived"),
            ),
        )

    @staticmethod
    def _yaml_row_identity(value: Any) -> tuple[str, str]:
        if not isinstance(value, dict):
            return "未命名来源", ""
        name = value.get("name")
        locator = value.get("locator", value.get("url", ""))
        return str(name or "未命名来源")[:120], str(locator or "")[:500]

    @staticmethod
    def _parse_bool(value: Any, field: str) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().casefold()
            if normalized in {"true", "yes", "1"}:
                return True
            if normalized in {"false", "no", "0"}:
                return False
        raise ValueError(f"{field} 必须是 true 或 false。")

    @staticmethod
    def _parse_interval(value: Any) -> int:
        if isinstance(value, bool):
            raise ValueError("poll_interval_minutes 必须是 5 到 1440 的整数。")
        try:
            interval = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("poll_interval_minutes 必须是 5 到 1440 的整数。") from exc
        return max(5, min(interval, 1440))

    def _parse_rows(self, kind: SourceKind, entries: str) -> list[tuple[int, str, str]]:
        rows: list[tuple[int, str, str]] = []
        for line_number, raw in enumerate(entries.splitlines(), start=1):
            value = raw.strip()
            if not value or value.startswith("#"):
                continue
            if value.startswith("- "):
                value = value[2:].strip()

            if "|" in value:
                name, locator = (part.strip() for part in value.split("|", 1))
            elif "\t" in value:
                name, locator = (part.strip() for part in value.split("\t", 1))
            else:
                locator = value
                name = self._default_name(kind, locator)

            if not locator:
                rows.append((line_number, name or "未命名来源", ""))
                continue
            rows.append((line_number, (name or self._default_name(kind, locator))[:120], locator))
        if not rows:
            raise ValueError("请至少提供一条来源。")
        return rows

    @staticmethod
    def _default_name(kind: SourceKind, locator: str) -> str:
        value = locator.strip().rstrip("/")
        if kind == SourceKind.X_RSSHUB:
            return f"X · {value.lstrip('@')}"
        if kind == SourceKind.REDDIT:
            return f"Reddit · {value.lstrip('/')}"
        if kind == SourceKind.YOUTUBE:
            return f"YouTube · {value.rsplit('/', 1)[-1].lstrip('@')}"
        parsed = urlparse(value)
        return f"RSS · {parsed.netloc or value}"[:120]
