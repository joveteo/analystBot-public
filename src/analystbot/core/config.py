import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv

    DOTENV_AVAILABLE = True
except ImportError:
    DOTENV_AVAILABLE = False


def _looks_like_project_root(path: Path) -> bool:
    if (path / "data" / "live_stocks.db").is_file():
        return True
    pyproject = path / "pyproject.toml"
    if pyproject.is_file():
        try:
            return 'name = "analystbot"' in pyproject.read_text(encoding="utf-8")
        except OSError:
            return False
    return False


def get_project_root() -> Path:
    for env_key in ("ANALYSTBOT_ROOT", "PROJECT_ROOT"):
        raw = os.getenv(env_key)
        if raw:
            candidate = Path(raw).expanduser().resolve()
            if candidate.is_dir():
                return candidate

    cwd = Path.cwd().resolve()
    for path in (cwd, *cwd.parents):
        if _looks_like_project_root(path):
            return path

    here = Path(__file__).resolve()
    for path in here.parents:
        if _looks_like_project_root(path):
            return path

    return here.parent.parent.parent.parent


@dataclass
class Settings:
    tastytrade_client_secret: str | None = field(default=None, repr=False)
    tastytrade_refresh_token: str | None = field(default=None, repr=False)
    telegram_bot_token: str | None = field(default=None, repr=False)
    telegram_chat_id: str | None = None
    telegram_chat_vixfix_id: str | None = None

    tastytrade_client_id: str | None = None
    tastytrade_env: str | None = None
    tastytrade_user_agent: str | None = None
    fred_api_key: str | None = None
    telegram_chat_fred_id: str | None = None
    telegram_chat_chart_id: str | None = None
    rrg_run_timezone: str | None = None

    project_root: Path = field(default_factory=get_project_root)
    db_path: Path = field(init=False)
    data_dir: Path = field(init=False)
    logs_dir: Path = field(init=False)
    env_path: Path = field(init=False)

    _loaded: bool = field(default=False, repr=False)
    _validation_errors: list[str] = field(default_factory=list, repr=False)

    REQUIRED_VARS: list[str] = field(
        default_factory=lambda: [
            "TASTYTRADE_CLIENT_SECRET",
            "TASTYTRADE_REFRESH_TOKEN",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
            "TELEGRAM_CHAT_VIXFIX_ID",
        ],
        repr=False,
    )

    OPTIONAL_VARS: list[str] = field(
        default_factory=lambda: [
            "TASTYTRADE_CLIENT_ID",
            "TASTYTRADE_ENV",
            "TASTYTRADE_USER_AGENT",
            "FRED_API_KEY",
            "TELEGRAM_CHAT_FRED_ID",
            "TELEGRAM_CHAT_CHART_ID",
            "RRG_RUN_TIMEZONE",
        ],
        repr=False,
    )

    def __post_init__(self):
        self.data_dir = self.project_root / "data"
        self.logs_dir = self.project_root / "logs"
        self.db_path = self.data_dir / "live_stocks.db"
        self.env_path = self.project_root / ".env"

        if not self._loaded:
            self.load()

    def load(self, env_path: Path | None = None) -> "Settings":
        if env_path:
            self.env_path = env_path

        if DOTENV_AVAILABLE and self.env_path.exists():
            load_dotenv(self.env_path, override=True)
        elif DOTENV_AVAILABLE:
            load_dotenv(override=True)

        def _env(name: str) -> str | None:
            raw = os.getenv(name)
            if raw is None:
                return None
            stripped = raw.strip()
            return stripped or None

        self.tastytrade_client_secret = _env("TASTYTRADE_CLIENT_SECRET")
        self.tastytrade_refresh_token = _env("TASTYTRADE_REFRESH_TOKEN")
        self.telegram_bot_token = _env("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = _env("TELEGRAM_CHAT_ID")
        self.telegram_chat_vixfix_id = _env("TELEGRAM_CHAT_VIXFIX_ID")

        self.tastytrade_client_id = _env("TASTYTRADE_CLIENT_ID")
        self.tastytrade_env = _env("TASTYTRADE_ENV")
        self.tastytrade_user_agent = _env("TASTYTRADE_USER_AGENT")
        self.fred_api_key = _env("FRED_API_KEY")
        self.telegram_chat_fred_id = _env("TELEGRAM_CHAT_FRED_ID")
        self.telegram_chat_chart_id = _env("TELEGRAM_CHAT_CHART_ID")
        self.rrg_run_timezone = _env("RRG_RUN_TIMEZONE")

        self._loaded = True
        return self

    def validate(self, stage: str | None = None) -> tuple[bool, list[str]]:
        missing = []

        if stage is None or stage == "market_data":
            if not self.tastytrade_client_secret:
                missing.append("TASTYTRADE_CLIENT_SECRET")
            if not self.tastytrade_refresh_token:
                missing.append("TASTYTRADE_REFRESH_TOKEN")

        if stage is None or stage == "telegram":
            if not self.telegram_bot_token:
                missing.append("TELEGRAM_BOT_TOKEN")
            if not self.telegram_chat_id:
                missing.append("TELEGRAM_CHAT_ID")
            if not self.telegram_chat_vixfix_id:
                missing.append("TELEGRAM_CHAT_VIXFIX_ID")

        if stage == "fred" and not self.fred_api_key:
            missing.append("FRED_API_KEY")

        if stage == "fred_telegram":
            if not self.telegram_bot_token:
                missing.append("TELEGRAM_BOT_TOKEN")
            if not self.telegram_chat_id:
                missing.append("TELEGRAM_CHAT_ID")
            if not self.telegram_chat_fred_id:
                missing.append("TELEGRAM_CHAT_FRED_ID")

        if stage == "chart_telegram":
            if not self.telegram_bot_token:
                missing.append("TELEGRAM_BOT_TOKEN")
            if not self.telegram_chat_id:
                missing.append("TELEGRAM_CHAT_ID")
            if not self.telegram_chat_chart_id:
                missing.append("TELEGRAM_CHAT_CHART_ID")

        self._validation_errors = missing
        return len(missing) == 0, missing

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
