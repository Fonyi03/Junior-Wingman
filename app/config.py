from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # General
    data_dir: Path = Path("/data")
    base_url: str = "http://localhost:8000"
    secret_key: str = "change-me"
    # Optional: set when the app is reachable from the internet (cloud deploy)
    app_password: str = ""

    # LLM: "claude" or "ollama"
    llm_provider: str = "claude"
    anthropic_api_key: str = ""
    claude_model: str = "claude-opus-5-5"
    # Model used for bulk job scoring and email classification
    claude_scoring_model: str = "claude-opus-5-5"
    claude_fallbacks: bool = True
    ollama_url: str = "http://host.docker.internal:11434"
    ollama_model: str = "llama3.1"

    # Job sources
    jsearch_api_key: str = ""  # RapidAPI key, covers LinkedIn/Indeed/Glassdoor listings via Google Jobs
    enable_remotive: bool = True
    enable_arbeitnow: bool = True
    enable_remoteok: bool = True

    # Google OAuth (Gmail)
    google_client_id: str = ""
    google_client_secret: str = ""

    # Scheduling / limits
    search_interval_hours: int = 6
    email_sync_minutes: int = 30
    max_llm_scores_per_run: int = 25
    auto_prepare_drafts: bool = True
    max_drafts_per_run: int = 5
    email_lookback_days: int = 30
    linkedin_sync_days: int = 7
    # Used to display dates; storage is always UTC
    timezone: str = "Europe/Budapest"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.data_dir / 'jobhunter.db'}"

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.upload_dir.mkdir(parents=True, exist_ok=True)
    return s
