import json
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from analystbot.api.http_client import (
    HttpClient,
    RetryPolicy,
)
from analystbot.core.log_config import setup_logger

if TYPE_CHECKING:
    import requests

logger = setup_logger("api.telegram")


@dataclass
class TelegramConfig:
    REQUEST_TIMEOUT: int = 30
    MAX_MESSAGE_LENGTH: int = 4000
    MAX_CAPTION_LENGTH: int = 1024
    DEFAULT_PARSE_MODE: str = "HTML"


class TelegramClient:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        config: TelegramConfig | None = None,
        session: Optional["requests.Session"] = None,
    ):
        if not bot_token:
            raise ValueError("Telegram bot token is required")
        if not chat_id:
            raise ValueError("Telegram chat ID is required")

        self.bot_token = bot_token
        self.chat_id = chat_id
        self.config = config or TelegramConfig()

        policy = RetryPolicy(
            max_retries=3,
            base_delay=1.0,
            max_delay=30.0,
            jitter=0.1,
            timeout=(10, self.config.REQUEST_TIMEOUT),
            rate_limit_delay=0.1,
        )
        self._http = HttpClient(policy=policy, session=session)

        logger.info("Telegram client initialized")

    @property
    def base_url(self) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}"

    @staticmethod
    def _extract_error(response) -> str:
        try:
            data = response.json()
            return str(data.get("description", response.text))
        except Exception:
            return str(response.text)

    def send_message(
        self,
        text: str,
        topic_id: str | None = None,
        parse_mode: str | None = None,
        chat_id: str | None = None,
        *,
        disable_notification: bool = False,
    ) -> bool:
        url = f"{self.base_url}/sendMessage"
        payload = {
            "chat_id": chat_id or self.chat_id,
            "text": text,
            "parse_mode": parse_mode or self.config.DEFAULT_PARSE_MODE,
            "disable_notification": disable_notification,
        }

        if topic_id:
            payload["message_thread_id"] = topic_id

        response = self._http.post(url, json=payload)

        if response is None:
            logger.error("Failed to send message: no response from server")
            return False

        if response.ok:
            try:
                result = response.json()
                if result.get("ok"):
                    return True
                error_desc = result.get("description", "Unknown error")
                logger.error(f"Telegram API error: {error_desc}")
            except Exception:
                logger.error("Telegram API returned non-JSON response")
                return False
        else:
            error_desc = self._extract_error(response)
            logger.error(f"Telegram error ({response.status_code}): {error_desc}")

        if topic_id and response.status_code == 400:
            logger.warning("Retrying without topic_id after Bad Request")
            return self.send_message(
                text=text,
                topic_id=None,
                parse_mode=parse_mode,
                chat_id=chat_id,
                disable_notification=disable_notification,
            )

        return False

    def send_photo(
        self,
        photo_path: Path,
        caption: str = "",
        topic_id: str | None = None,
        chat_id: str | None = None,
        *,
        disable_notification: bool = False,
    ) -> bool:
        if not photo_path.exists():
            logger.error(f"Photo not found: {photo_path}")
            return False

        url = f"{self.base_url}/sendPhoto"

        form_data = {
            "chat_id": chat_id or self.chat_id,
            "caption": caption[: self.config.MAX_CAPTION_LENGTH],
            "disable_notification": "true" if disable_notification else "false",
        }

        if topic_id:
            form_data["message_thread_id"] = topic_id

        try:
            with open(photo_path, "rb") as photo_file:
                files = {"photo": photo_file}
                response = self._http.post(url, data=form_data, files=files)
        except OSError as e:
            logger.error(f"Failed to read photo file {photo_path}: {e}")
            return False

        if response is None:
            logger.error("Failed to send photo: no response from server")
            return False

        if response.ok:
            try:
                result = response.json()
                if result.get("ok"):
                    logger.info(f"Photo sent: {photo_path.name}")
                    return True
                else:
                    error = result.get("description", "Unknown error")
                    logger.error(f"Telegram API error: {error}")
            except Exception:
                logger.error("Telegram API returned non-JSON response")
                return False
        else:
            error = self._extract_error(response)
            logger.error(f"Failed to send photo ({response.status_code}): {error}")

        if topic_id and response.status_code == 400:
            logger.warning("Retrying photo without topic_id")
            return self.send_photo(
                photo_path=photo_path,
                caption=caption,
                topic_id=None,
                chat_id=chat_id,
                disable_notification=disable_notification,
            )

        return False

    def send_media_group(
        self,
        items: list[tuple[Path, str]],
        topic_id: str | None = None,
        chat_id: str | None = None,
        *,
        disable_notification: bool = False,
    ) -> bool:
        if len(items) < 2:
            raise ValueError("send_media_group requires at least 2 photos")
        if len(items) > 10:
            items = items[:10]

        missing = [path for path, _caption in items if not path.exists()]
        if missing:
            logger.error("Album photos not found: %s", ", ".join(map(str, missing)))
            return False

        url = f"{self.base_url}/sendMediaGroup"
        media: list[dict[str, str]] = []
        files: dict[str, object] = {}
        try:
            with ExitStack() as stack:
                for index, (photo_path, caption) in enumerate(items):
                    key = f"photo{index}"
                    files[key] = stack.enter_context(photo_path.open("rb"))
                    media.append(
                        {
                            "type": "photo",
                            "media": f"attach://{key}",
                            "caption": caption[: self.config.MAX_CAPTION_LENGTH],
                            "parse_mode": self.config.DEFAULT_PARSE_MODE,
                        }
                    )

                form_data = {
                    "chat_id": chat_id or self.chat_id,
                    "media": json.dumps(media),
                    "disable_notification": "true" if disable_notification else "false",
                }
                if topic_id:
                    form_data["message_thread_id"] = topic_id

                response = self._http.post(url, data=form_data, files=files)
        except OSError as e:
            logger.error("Failed to read album photos: %s", e)
            return False

        if response is None:
            logger.error("Failed to send album: no response from server")
            return False

        if response.ok:
            try:
                result = response.json()
                if result.get("ok"):
                    names = ", ".join(path.name for path, _caption in items)
                    logger.info("Album sent: %s", names)
                    return True
                error = result.get("description", "Unknown error")
                logger.error("Telegram API error: %s", error)
            except Exception:
                logger.error("Telegram API returned non-JSON response")
                return False
        else:
            error = self._extract_error(response)
            logger.error("Failed to send album (%s): %s", response.status_code, error)

        if topic_id and response.status_code == 400:
            logger.warning("Retrying album without topic_id")
            return self.send_media_group(
                items=items,
                topic_id=None,
                chat_id=chat_id,
                disable_notification=disable_notification,
            )

        return False
