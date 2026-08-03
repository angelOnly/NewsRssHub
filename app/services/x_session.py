"""Dynamic X session support backed by an encrypted SQLite credential.

RSSHub reads Twitter credentials only when its process starts. This module keeps
the session in NewsRSSHub instead: the web page can replace a Cookie at runtime,
the worker validates it before every X batch, and the source connector uses the
same current X GraphQL operations as RSSHub's web-api route.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable

import requests
from cryptography.fernet import Fernet, InvalidToken

from app.config import Settings
from app.domain.models import FeedItem
from app.plugins.base import SourceFetchResult
from app.storage.repository import Repository


X_CONNECTOR = "x_session"
COOKIE_NAMES = ("auth_token", "ct0")
X_BASE_URL = "https://x.com"
X_BEARER_TOKEN = (
    "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
X_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

USER_FEATURES = {
    "hidden_profile_subscriptions_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "subscriptions_verification_info_is_identity_verified_enabled": True,
    "subscriptions_verification_info_verified_since_enabled": True,
    "highlights_tweets_tab_ui_enabled": True,
    "responsive_web_twitter_article_notes_tab_enabled": True,
    "subscriptions_feature_can_gift_premium": True,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
}

FEED_FEATURES = {
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}


class XSessionError(RuntimeError):
    """A user-safe X session error. It must never contain a Cookie value."""


class XCredentialMissingError(XSessionError):
    pass


class XCredentialExpiredError(XSessionError):
    pass


class XCredentialConfigurationError(XSessionError):
    pass


class XTemporaryError(XSessionError):
    pass


@dataclass(slots=True)
class XCredentialStatus:
    state: str
    message: str
    configured: bool
    fingerprint: str = ""
    updated_at: str | None = None
    last_validated_at: str | None = None
    last_error: str = ""


def parse_x_cookie(value: str) -> dict[str, str]:
    """Accept an auth_token value or a complete browser Cookie header."""

    raw = value.strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    if not raw:
        raise XCredentialMissingError("请粘贴 X 的 auth_token Cookie。")

    if "auth_token=" not in raw:
        token = raw.strip()
        if any(char.isspace() for char in token):
            raise XCredentialMissingError("请输入 auth_token 的值，或完整的 Cookie 字符串。")
        cookies = {"auth_token": token}
    else:
        cookies: dict[str, str] = {}
        for part in raw.split(";"):
            name, separator, cookie_value = part.strip().partition("=")
            if separator and name.strip() in COOKIE_NAMES and cookie_value.strip():
                cookies[name.strip()] = cookie_value.strip()

    if not cookies.get("auth_token"):
        raise XCredentialMissingError("未找到 auth_token；请从 x.com 的 Cookie 中复制该值。")
    return {name: cookies[name] for name in COOKIE_NAMES if cookies.get(name)}


def _fingerprint(cookies: dict[str, str]) -> str:
    return hashlib.sha256(cookies["auth_token"].encode("utf-8")).hexdigest()[-10:]


class XWebClient:
    """Small synchronous X GraphQL client with runtime-resolved operation ids."""

    def __init__(self, cookies: dict[str, str], timeout: int) -> None:
        self.input_cookies = dict(cookies)
        self.timeout = timeout
        self.session = requests.Session()
        self.session.cookies.update(cookies)
        self.query_ids: dict[str, str] = {}

    def validate(self) -> dict[str, str]:
        self._prime()
        payload = self._graphql("Viewer", {}, USER_FEATURES)
        if not ((payload.get("data") or {}).get("viewer")):
            raise XCredentialExpiredError("X 登录 Cookie 已失效，请更新后重试。")
        return self._refreshed_cookies()

    def get_user_id(self, handle: str) -> str:
        payload = self._graphql(
            "UserByScreenName",
            {"screen_name": handle, "withSafetyModeUserFields": True},
            USER_FEATURES,
            {"fieldToggles": {"withAuxiliaryUserLabels": False}},
        )
        result = (((payload.get("data") or {}).get("user") or {}).get("result") or {})
        user_id = str(result.get("rest_id") or "")
        if not user_id:
            raise XTemporaryError("X 未返回该账号资料，请稍后重试。")
        return user_id

    def get_user_tweets(self, user_id: str) -> list[dict[str, Any]]:
        payload = self._graphql(
            "UserTweets",
            {
                "userId": user_id,
                "count": 20,
                "includePromotedContent": True,
                "withQuickPromoteEligibilityTweetFields": True,
                "withVoice": True,
                "withV2Timeline": True,
            },
            FEED_FEATURES,
        )
        tweets: dict[str, dict[str, Any]] = {}

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                legacy = node.get("legacy")
                tweet_id = str(node.get("rest_id") or "")
                if (
                    tweet_id
                    and isinstance(legacy, dict)
                    and legacy.get("full_text")
                    and str(legacy.get("user_id_str") or "") == user_id
                ):
                    tweets[tweet_id] = legacy
                for child in node.values():
                    walk(child)
            elif isinstance(node, list):
                for child in node:
                    walk(child)

        walk(payload)
        return [
            {"id": tweet_id, "legacy": legacy}
            for tweet_id, legacy in list(tweets.items())[:20]
        ]

    def close(self) -> None:
        self.session.close()

    def _prime(self) -> None:
        try:
            homepage = self.session.get(X_BASE_URL + "/", headers={"User-Agent": X_USER_AGENT}, timeout=self.timeout)
            homepage.raise_for_status()
            main_match = re.search(r"/client-web/main\.([a-z0-9]+)\.", homepage.text)
            if not main_match:
                raise XTemporaryError("X 页面协议正在更新，请稍后重试。")
            main_url = f"https://abs.twimg.com/responsive-web/client-web/main.{main_match.group(1)}.js"
            bundle = self.session.get(main_url, headers={"User-Agent": X_USER_AGENT}, timeout=self.timeout)
            bundle.raise_for_status()
        except requests.RequestException as exc:
            raise XTemporaryError("暂时无法连接 X，请稍后重试。") from exc

        self.query_ids = {
            name: query_id
            for query_id, name in re.findall(r'queryId:"([^"]+)".+?operationName:"([^"]+)"', bundle.text)
        }
        missing = {"Viewer", "UserByScreenName", "UserTweets"} - self.query_ids.keys()
        if missing:
            raise XTemporaryError("X 页面协议正在更新，请稍后重试。")

    def _graphql(
        self,
        operation: str,
        variables: dict[str, Any],
        features: dict[str, Any],
        extra_params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        query_id = self.query_ids.get(operation)
        if not query_id:
            raise XTemporaryError("X 页面协议正在更新，请稍后重试。")
        params = {
            "variables": json.dumps(variables, ensure_ascii=False, separators=(",", ":")),
            "features": json.dumps(features, ensure_ascii=False, separators=(",", ":")),
        }
        for key, value in (extra_params or {}).items():
            params[key] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        headers = {
            "User-Agent": X_USER_AGENT,
            "authorization": X_BEARER_TOKEN,
            "x-csrf-token": self.session.cookies.get("ct0", self.input_cookies.get("ct0", "")),
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "en",
            "accept-language": "en-US,en;q=0.9",
            "referer": X_BASE_URL + "/",
        }
        try:
            response = self.session.get(
                f"{X_BASE_URL}/i/api/graphql/{query_id}/{operation}",
                params=params,
                headers=headers,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise XTemporaryError("暂时无法连接 X，请稍后重试。") from exc
        if response.status_code in {401, 403}:
            raise XCredentialExpiredError("X 登录 Cookie 已失效，请更新后重试。")
        if response.status_code == 429:
            raise XTemporaryError("X 暂时限流，稍后会自动重试。")
        if response.status_code >= 400:
            raise XTemporaryError("暂时无法读取 X 内容，请稍后重试。")
        try:
            payload = response.json()
        except ValueError as exc:
            raise XTemporaryError("X 返回了无法识别的数据，请稍后重试。") from exc
        if not isinstance(payload, dict):
            raise XTemporaryError("X 返回了无法识别的数据，请稍后重试。")
        errors = payload.get("errors") or []
        if errors:
            codes = {str(error.get("code")) for error in errors if isinstance(error, dict)}
            if {"32", "89", "135"} & codes:
                raise XCredentialExpiredError("X 登录 Cookie 已失效，请更新后重试。")
            if "88" in codes:
                raise XTemporaryError("X 暂时限流，稍后会自动重试。")
            raise XTemporaryError("暂时无法读取 X 内容，请稍后重试。")
        return payload

    def _refreshed_cookies(self) -> dict[str, str]:
        cookies = {
            name: str(self.session.cookies.get(name) or self.input_cookies.get(name) or "")
            for name in COOKIE_NAMES
        }
        if not cookies.get("auth_token"):
            raise XCredentialExpiredError("X 登录 Cookie 已失效，请更新后重试。")
        return {name: value for name, value in cookies.items() if value}


class XSessionService:
    """Stores and uses an X session without exposing it to templates or logs."""

    def __init__(
        self,
        repository: Repository,
        settings: Settings,
        client_factory: Callable[[dict[str, str]], Any] | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self._client_factory = client_factory

    def _cipher(self) -> Fernet:
        raw_key = self.settings.credential_encryption_key
        if not raw_key:
            raise XCredentialConfigurationError(
                "尚未配置 CREDENTIAL_ENCRYPTION_KEY，无法安全保存 X Cookie。"
            )
        try:
            return Fernet(raw_key.encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise XCredentialConfigurationError(
                "CREDENTIAL_ENCRYPTION_KEY 格式无效，无法安全保存 X Cookie。"
            ) from exc

    def status(self) -> XCredentialStatus:
        try:
            self._cipher()
        except XCredentialConfigurationError as exc:
            return XCredentialStatus("needs_key", str(exc), False)
        record = self.repository.get_connector_credential(X_CONNECTOR)
        if not record:
            return XCredentialStatus("missing", "尚未保存 X 登录 Cookie；X 账号暂不会抓取。", False)
        state = str(record.get("status") or "unknown")
        last_error = str(record.get("last_error") or "")
        messages = {
            "valid": "X 登录 Cookie 可用。",
            "invalid": "X 登录 Cookie 已失效，请在此更新后再抓取。",
            "error": last_error or "暂时无法验证 X 登录状态，请稍后重试。",
        }
        return XCredentialStatus(
            state,
            messages.get(state, "尚未验证 X 登录 Cookie。"),
            True,
            fingerprint=str(record.get("fingerprint") or ""),
            updated_at=record.get("updated_at"),
            last_validated_at=record.get("last_validated_at"),
            last_error=last_error,
        )

    def save_from_web(self, cookie_value: str) -> XCredentialStatus:
        candidate = parse_x_cookie(cookie_value)
        refreshed = self._validate(candidate)
        self._save_refreshed(refreshed)
        return self.status()

    def test_saved(self) -> XCredentialStatus:
        refreshed = self._validate(self._load_cookies())
        self._save_refreshed(refreshed)
        return self.status()

    def fetch_many(self, sources: list[dict[str, Any]]) -> dict[int, SourceFetchResult]:
        client: Any | None = None
        try:
            cookies = self._load_cookies()
            client = self._new_client(cookies)
            refreshed = client.validate()
            self._save_refreshed(refreshed)
            results: dict[int, SourceFetchResult] = {}
            for source in sources:
                source_id = int(source["id"])
                try:
                    results[source_id] = SourceFetchResult(items=self._fetch_source(client, source))
                except Exception as exc:
                    results[source_id] = SourceFetchResult(error=self._safe_error(exc))
            return results
        except Exception as exc:
            safe_error = self._safe_error(exc)
            self._record_failure(safe_error)
            return {int(source["id"]): SourceFetchResult(error=safe_error) for source in sources}
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def _validate(self, cookies: dict[str, str]) -> dict[str, str]:
        client: Any | None = None
        try:
            client = self._new_client(cookies)
            return client.validate()
        except Exception as exc:
            raise self._safe_error(exc) from exc
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def _new_client(self, cookies: dict[str, str]) -> Any:
        if self._client_factory:
            return self._client_factory(cookies)
        return XWebClient(cookies, self.settings.request_timeout)

    def _load_cookies(self) -> dict[str, str]:
        record = self.repository.get_connector_credential(X_CONNECTOR)
        if not record:
            raise XCredentialMissingError("X 登录 Cookie 未配置，请在“X 登录状态”页面保存后重试。")
        try:
            decrypted = self._cipher().decrypt(str(record["ciphertext"]).encode("ascii"))
            payload = json.loads(decrypted.decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError, KeyError) as exc:
            raise XCredentialConfigurationError("已保存的 X Cookie 无法读取，请重新保存一次。") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("auth_token"), str):
            raise XCredentialConfigurationError("已保存的 X Cookie 格式无效，请重新保存一次。")
        return {name: str(payload[name]) for name in COOKIE_NAMES if payload.get(name)}

    def _save_refreshed(self, cookies: dict[str, str]) -> None:
        ciphertext = self._cipher().encrypt(
            json.dumps(cookies, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        self.repository.save_connector_credential(
            connector=X_CONNECTOR,
            ciphertext=ciphertext,
            fingerprint=_fingerprint(cookies),
            status="valid",
        )

    def _record_failure(self, exc: XSessionError) -> None:
        if not self.repository.get_connector_credential(X_CONNECTOR):
            return
        status = "invalid" if isinstance(exc, XCredentialExpiredError) else "error"
        self.repository.update_connector_credential_health(
            X_CONNECTOR,
            status=status,
            last_error=str(exc),
        )

    def _fetch_source(self, client: Any, source: dict[str, Any]) -> list[FeedItem]:
        config = source.get("config") or {}
        user_id = str(config.get("x_user_id") or "")
        if not user_id:
            user_id = str(client.get_user_id(str(source["locator"])))
            self.repository.update_source_config(int(source["id"]), {**config, "x_user_id": user_id})
        raw_tweets = client.get_user_tweets(user_id)
        handle = str(source["locator"])
        items: list[FeedItem] = []
        for raw_tweet in raw_tweets:
            tweet_id = str(raw_tweet.get("id") or "")
            legacy = raw_tweet.get("legacy") or {}
            text = str(legacy.get("full_text") or "").strip()
            if not tweet_id or not text:
                continue
            items.append(
                FeedItem(
                    guid=f"x:{tweet_id}",
                    title=" ".join(text.split())[:500],
                    link=f"https://x.com/{handle}/status/{tweet_id}",
                    content=text[:20000],
                    author=handle,
                    published_at=self._parse_time(legacy.get("created_at")),
                    raw={"tweet_id": tweet_id, "reply_count": legacy.get("reply_count", 0)},
                )
            )
        return items

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = parsedate_to_datetime(str(value))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_error(exc: Exception) -> XSessionError:
        if isinstance(exc, XSessionError):
            return exc
        text = str(exc).lower()
        if any(marker in text for marker in ("401", "403", "unauthorized", "not logged", "auth", "csrf")):
            return XCredentialExpiredError("X 登录 Cookie 已失效，请更新后重试。")
        if "429" in text or "rate limit" in text or "too many" in text:
            return XTemporaryError("X 暂时限流，稍后会自动重试。")
        if any(marker in text for marker in ("suspend", "locked", "challenge")):
            return XCredentialExpiredError("X 登录会话需要在浏览器中重新确认，请更新 Cookie。")
        return XTemporaryError("暂时无法验证或读取 X 内容，请稍后重试。")
