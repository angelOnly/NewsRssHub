"""iPhone 主屏幕 Web App 的单设备聚合推送。

本模块只发送“本轮抓取发现 X 条新内容”的首页提醒，不会按资讯逐条推送。
订阅和 VAPID 私钥沿用既有的加密凭据表，避免为单用户场景额外引入表结构迁移。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

from app.config import Settings
from app.storage.repository import Repository, utc_now


logger = logging.getLogger(__name__)

WEB_PUSH_SUBSCRIPTION_CONNECTOR = "web_push_subscription"
WEB_PUSH_VAPID_CONNECTOR = "web_push_vapid"
WEB_PUSH_STATE_SETTING = "web_push_state"

# 等待短暂的收敛窗口，让同一批错峰来源的新增量合成一条提醒。
PUSH_SETTLE_SECONDS = 60
RETRY_DELAYS_SECONDS = (60, 300, 1800)


class WebPushError(RuntimeError):
    """面向页面和日志的安全 Web Push 错误。"""


class WebPushConfigurationError(WebPushError):
    pass


class WebPushSubscriptionError(WebPushError):
    pass


@dataclass(slots=True)
class WebPushStatus:
    state: str
    message: str
    configured: bool
    updated_at: str | None = None
    last_error: str = ""


@dataclass(slots=True)
class PushState:
    pending_count: int = 0
    due_at: str | None = None
    last_sent_at: str | None = None
    attempt_count: int = 0
    last_error: str = ""


@dataclass(slots=True)
class PushDelivery:
    state: str
    pending_count: int = 0
    message: str = ""


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[-10:]


class WebPushService:
    """管理一个主屏幕设备的订阅、聚合状态和 Web Push 投递。"""

    def __init__(
        self,
        repository: Repository,
        settings: Settings,
        *,
        sender: Callable[..., Any] | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self._sender = sender or webpush
        self._now = now_provider or utc_now

    def status(self) -> WebPushStatus:
        if not self.settings.credential_encryption_key:
            return WebPushStatus(
                state="needs_key",
                message="尚未设置凭据加密主密钥，无法安全启用手机通知。",
                configured=False,
            )

        record = self.repository.get_connector_credential(WEB_PUSH_SUBSCRIPTION_CONNECTOR)
        if not record:
            return WebPushStatus(
                state="ready",
                message="尚未绑定手机；请从主屏幕打开后开启通知。",
                configured=False,
            )
        try:
            self._read_subscription_record(record)
        except WebPushError as exc:
            return WebPushStatus(
                state="error",
                message=str(exc),
                configured=False,
                updated_at=record.get("updated_at"),
                last_error=str(record.get("last_error") or ""),
            )

        state = str(record.get("status") or "unknown")
        last_error = str(record.get("last_error") or "")
        if state == "invalid":
            return WebPushStatus(
                state="invalid",
                message="这台手机的通知订阅已失效，请重新开启通知。",
                configured=False,
                updated_at=record.get("updated_at"),
                last_error=last_error,
            )
        if state == "error":
            return WebPushStatus(
                state="error",
                message=last_error or "上次发送通知失败，系统会在下次抓取后自动重试。",
                configured=True,
                updated_at=record.get("updated_at"),
                last_error=last_error,
            )
        return WebPushStatus(
            state="enabled",
            message="手机通知已开启；每个抓取周期最多提醒一次。",
            configured=True,
            updated_at=record.get("updated_at"),
            last_error=last_error,
        )

    def public_config(self) -> dict[str, object]:
        """返回浏览器订阅所需的公开 VAPID 密钥。"""

        # 先校验发件人标识，避免页面显示可开启、订阅保存后才发现无法投递。
        self._vapid_subject()
        vapid = self._get_or_create_vapid()
        public_key = vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        return {
            "available": True,
            "public_key": base64.urlsafe_b64encode(public_key).rstrip(b"=").decode("ascii"),
            "status": asdict(self.status()),
        }

    def save_subscription(self, raw_subscription: Mapping[str, Any]) -> WebPushStatus:
        """验证并加密保存当前主屏幕 Web App 的唯一订阅。"""

        subscription = self._normalize_subscription(raw_subscription)
        previous = self._subscription()
        # 订阅前先确保 VAPID 密钥存在，避免保存了却无法发送的半成品状态。
        self._vapid_subject()
        self._get_or_create_vapid()
        ciphertext = self._encrypt_payload(subscription)
        self.repository.save_connector_credential(
            connector=WEB_PUSH_SUBSCRIPTION_CONNECTOR,
            ciphertext=ciphertext,
            fingerprint=_fingerprint(subscription["endpoint"]),
            status="valid",
        )
        # 仅在新设备或浏览器重建订阅时清除积压；正常页面刷新会同步同一订阅，
        # 不能因此吞掉已经等待发送的抓取提醒。
        if not previous or previous["endpoint"] != subscription["endpoint"]:
            self._save_state(PushState())
        return self.status()

    def unsubscribe(self) -> None:
        self.repository.delete_connector_credential(WEB_PUSH_SUBSCRIPTION_CONNECTOR)
        self._save_state(PushState())

    def record_new_items(self, count: int) -> bool:
        """把本次新增量并入待发提醒；同一周期不会形成多条 Push。"""

        if count <= 0 or not self._subscription():
            return False

        now = self._normalized_now()
        state = self._load_state()
        state.pending_count = min(9999, state.pending_count + int(count))
        if not state.due_at:
            due_at = now + timedelta(seconds=PUSH_SETTLE_SECONDS)
            previous_sent_at = self._parse_time(state.last_sent_at)
            if previous_sent_at:
                # 使用现有全局抓取间隔限频，保证手机不会因来源错峰而连续弹窗。
                cooldown_end = previous_sent_at + timedelta(
                    minutes=self.repository.get_fetch_policy().interval_minutes
                )
                if cooldown_end > due_at:
                    due_at = cooldown_end
            state.due_at = due_at.isoformat()
            state.attempt_count = 0
            state.last_error = ""
        self._save_state(state)
        return True

    def deliver_pending(self) -> PushDelivery:
        """在 processor 中投递已到期的一条聚合提醒，并保留可恢复的失败状态。"""

        state = self._load_state()
        if state.pending_count <= 0:
            return PushDelivery(state="idle")

        now = self._normalized_now()
        due_at = self._parse_time(state.due_at)
        if due_at and due_at > now:
            return PushDelivery(state="waiting", pending_count=state.pending_count)

        subscription = self._subscription()
        if not subscription:
            # 订阅已被用户取消或无法读取时，不能在下次绑定时补发旧数据。
            self._save_state(PushState())
            return PushDelivery(state="disabled", message="未绑定有效手机，已清除待发提醒。")

        try:
            self._send_notification(subscription, state.pending_count)
        except WebPushException as exc:
            status_code = self._response_status(exc)
            if status_code in {404, 410}:
                self.repository.delete_connector_credential(WEB_PUSH_SUBSCRIPTION_CONNECTOR)
                self._save_state(PushState())
                return PushDelivery(state="invalid", message="手机订阅已失效，请重新开启通知。")
            return self._reschedule_after_error(state, status_code=status_code)
        except Exception:
            logger.exception("Web Push 发送失败")
            return self._reschedule_after_error(state)

        self.repository.update_connector_credential_health(
            WEB_PUSH_SUBSCRIPTION_CONNECTOR,
            status="valid",
            last_error="",
            validated=True,
        )
        self._save_state(PushState(last_sent_at=now.isoformat()))
        return PushDelivery(state="sent", pending_count=state.pending_count)

    def send_test(self) -> None:
        subscription = self._subscription()
        if not subscription:
            raise WebPushSubscriptionError("请先开启手机通知。")
        vapid = self._get_or_create_vapid()
        payload = json.dumps(
            {
                "title": "NewsRSSHub",
                "body": "测试通知已发送，点此返回首页",
                "url": "/",
                "tag": "newsrsshub-fetch",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            self._sender(
                subscription_info=subscription,
                data=payload,
                vapid_private_key=vapid,
                vapid_claims={"sub": self._vapid_subject()},
                content_encoding="aes128gcm",
                ttl=3600,
                timeout=self.settings.request_timeout,
            )
        except WebPushException as exc:
            status_code = self._response_status(exc)
            if status_code in {404, 410}:
                self.repository.delete_connector_credential(WEB_PUSH_SUBSCRIPTION_CONNECTOR)
                raise WebPushSubscriptionError("手机订阅已失效，请重新开启通知。") from exc
            raise WebPushError(self._safe_push_error(status_code)) from exc
        except Exception as exc:
            raise WebPushError("暂时无法发送测试通知，请稍后重试。") from exc

    def _send_notification(self, subscription: dict[str, Any], count: int) -> None:
        vapid = self._get_or_create_vapid()
        payload = json.dumps(
            {
                "title": "NewsRSSHub",
                "body": f"本轮抓取发现 {count} 条新内容，点此查看",
                "url": "/",
                "tag": "newsrsshub-fetch",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self._sender(
            subscription_info=subscription,
            data=payload,
            vapid_private_key=vapid,
            vapid_claims={"sub": self._vapid_subject()},
            content_encoding="aes128gcm",
            ttl=3600,
            timeout=self.settings.request_timeout,
        )

    def _reschedule_after_error(
        self, state: PushState, *, status_code: int | None = None
    ) -> PushDelivery:
        state.attempt_count += 1
        delay = RETRY_DELAYS_SECONDS[min(state.attempt_count - 1, len(RETRY_DELAYS_SECONDS) - 1)]
        state.due_at = (self._normalized_now() + timedelta(seconds=delay)).isoformat()
        state.last_error = self._safe_push_error(status_code)
        self._save_state(state)
        self.repository.update_connector_credential_health(
            WEB_PUSH_SUBSCRIPTION_CONNECTOR,
            status="error",
            last_error=state.last_error,
        )
        return PushDelivery(
            state="retry",
            pending_count=state.pending_count,
            message=state.last_error,
        )

    def _subscription(self) -> dict[str, Any] | None:
        record = self.repository.get_connector_credential(WEB_PUSH_SUBSCRIPTION_CONNECTOR)
        if not record:
            return None
        try:
            return self._read_subscription_record(record)
        except WebPushError as exc:
            self.repository.update_connector_credential_health(
                WEB_PUSH_SUBSCRIPTION_CONNECTOR,
                status="error",
                last_error=str(exc),
            )
            return None

    def _read_subscription_record(self, record: Mapping[str, Any]) -> dict[str, Any]:
        payload = self._decrypt_payload(str(record.get("ciphertext") or ""))
        return self._normalize_subscription(payload)

    def _normalize_subscription(self, raw_subscription: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(raw_subscription, Mapping):
            raise WebPushSubscriptionError("手机通知订阅格式无效，请重新开启通知。")
        endpoint = str(raw_subscription.get("endpoint") or "").strip()
        parsed = urlparse(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or len(endpoint) > 4096
            or any(char.isspace() for char in endpoint)
        ):
            raise WebPushSubscriptionError("手机通知订阅地址无效，请重新开启通知。")
        raw_keys = raw_subscription.get("keys")
        if not isinstance(raw_keys, Mapping):
            raise WebPushSubscriptionError("手机通知订阅缺少密钥，请重新开启通知。")
        p256dh = str(raw_keys.get("p256dh") or "").strip()
        auth = str(raw_keys.get("auth") or "").strip()
        if not self._is_base64url(p256dh, minimum=40, maximum=200) or not self._is_base64url(
            auth, minimum=12, maximum=100
        ):
            raise WebPushSubscriptionError("手机通知订阅密钥无效，请重新开启通知。")
        return {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}}

    @staticmethod
    def _is_base64url(value: str, *, minimum: int, maximum: int) -> bool:
        if not minimum <= len(value) <= maximum:
            return False
        return all(character.isalnum() or character in {"-", "_"} for character in value)

    def _get_or_create_vapid(self) -> Vapid:
        record = self.repository.get_connector_credential(WEB_PUSH_VAPID_CONNECTOR)
        if record:
            try:
                payload = self._decrypt_payload(str(record.get("ciphertext") or ""))
                encoded_pem = str(payload.get("private_pem") or "")
                private_pem = base64.b64decode(encoded_pem.encode("ascii"), validate=True)
                return Vapid.from_pem(private_pem)
            except (ValueError, TypeError, WebPushError) as exc:
                raise WebPushConfigurationError("已保存的通知密钥无法读取，请联系管理员重新生成。") from exc

        vapid = Vapid()
        vapid.generate_keys()
        private_pem = vapid.private_pem()
        public_bytes = vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        ciphertext = self._encrypt_payload(
            {"private_pem": base64.b64encode(private_pem).decode("ascii")}
        )
        self.repository.save_connector_credential(
            connector=WEB_PUSH_VAPID_CONNECTOR,
            ciphertext=ciphertext,
            fingerprint=_fingerprint(base64.urlsafe_b64encode(public_bytes).decode("ascii")),
            status="valid",
        )
        return vapid

    def _cipher(self) -> Fernet:
        raw_key = self.settings.credential_encryption_key
        if not raw_key:
            raise WebPushConfigurationError("尚未设置 CREDENTIAL_ENCRYPTION_KEY，无法安全启用手机通知。")
        try:
            return Fernet(raw_key.encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise WebPushConfigurationError("CREDENTIAL_ENCRYPTION_KEY 格式无效。") from exc

    def _encrypt_payload(self, payload: Mapping[str, Any]) -> str:
        return self._cipher().encrypt(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")

    def _decrypt_payload(self, ciphertext: str) -> dict[str, Any]:
        try:
            raw = self._cipher().decrypt(ciphertext.encode("ascii"))
            payload = json.loads(raw.decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, UnicodeEncodeError, json.JSONDecodeError) as exc:
            raise WebPushConfigurationError("已保存的手机通知数据无法读取，请重新开启通知。") from exc
        if not isinstance(payload, dict):
            raise WebPushConfigurationError("已保存的手机通知数据格式无效，请重新开启通知。")
        return payload

    def _load_state(self) -> PushState:
        raw = self.repository.get_app_setting(WEB_PUSH_STATE_SETTING)
        if not raw:
            return PushState()
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("state is not an object")
            return PushState(
                pending_count=max(0, min(int(payload.get("pending_count") or 0), 9999)),
                due_at=self._clean_timestamp(payload.get("due_at")),
                last_sent_at=self._clean_timestamp(payload.get("last_sent_at")),
                attempt_count=max(0, min(int(payload.get("attempt_count") or 0), 100)),
                last_error=str(payload.get("last_error") or "")[:300],
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            return PushState()

    def _save_state(self, state: PushState) -> None:
        self.repository.save_app_setting(
            WEB_PUSH_STATE_SETTING,
            json.dumps(asdict(state), ensure_ascii=False, separators=(",", ":")),
        )

    @staticmethod
    def _clean_timestamp(value: Any) -> str | None:
        candidate = str(value or "").strip()
        return candidate[:64] if candidate else None

    def _normalized_now(self) -> datetime:
        now = self._now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc).replace(microsecond=0)

    @staticmethod
    def _parse_time(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _vapid_subject(self) -> str:
        subject = str(self.settings.web_push_subject or "").strip()
        if subject.startswith("mailto:") and "@" in subject[7:]:
            return subject
        parsed = urlparse(subject)
        if parsed.scheme == "https" and parsed.netloc and not parsed.username and not parsed.password:
            # py-vapid 会把带端口的 HTTPS 联系地址误判为缺少 sub；VAPID 联系标识
            # 不需要和站点实际访问端口一致，因此仅在投递时去掉端口。
            try:
                has_port = parsed.port is not None
            except ValueError as exc:
                raise WebPushConfigurationError(
                    "Web Push 发件人标识无效，请在配置中设置 app.web_push_subject。"
                ) from exc
            if has_port:
                return f"https://{parsed.hostname}"
            return subject
        raise WebPushConfigurationError(
            "Web Push 发件人标识无效，请在配置中设置 app.web_push_subject。"
        )

    @staticmethod
    def _response_status(exc: WebPushException) -> int | None:
        response = getattr(exc, "response", None)
        try:
            return int(response.status_code) if response is not None else None
        except (TypeError, ValueError, AttributeError):
            return None

    @staticmethod
    def _safe_push_error(status_code: int | None = None) -> str:
        if status_code:
            return f"推送服务暂时不可用（HTTP {status_code}），稍后会自动重试。"
        return "推送服务暂时不可用，稍后会自动重试。"
