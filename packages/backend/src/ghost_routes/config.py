"""Runtime settings from environment variables and the ignored ``.env`` file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _repo_env() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        if (parent / "compose.yaml").exists():
            return parent / ".env"
    return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GHOST_ROUTES_", env_file=_repo_env(), extra="ignore")

    database_url: str = "postgresql+psycopg://ghost_routes:ghost_routes@127.0.0.1:55432/ghost_routes"
    state_dir: Path = Path("~/.local/state/ghost-routes")
    ollama_url: str = "http://127.0.0.1:11434"
    model: str = "qwen2.5vl:7b"
    api_host: str = "127.0.0.1"
    api_port: int = 8077
    web_port: int = 5177
    allowed_origins: list[str] = Field(default_factory=list)

    # Bounds on untrusted documents and inference.
    max_upload_bytes: int = 40 * 1024 * 1024
    max_pages: int = 20
    render_dpi: int = 144
    crop_px: int = 1008  # multiple of the 28 px Qwen2.5-VL patch, so crops are not resized
    crop_overlap_px: int = 128
    inference_timeout_s: float = 600.0
    inference_max_tokens: int = 3072
    inference_retries: int = 2
    job_lease_s: float = 900.0

    @field_validator("state_dir")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return v.expanduser()

    def origins(self) -> list[str]:
        if self.allowed_origins:
            return self.allowed_origins
        return [f"http://127.0.0.1:{self.web_port}", f"http://localhost:{self.web_port}"]

    def path(self, *parts: str) -> Path:
        p = self.state_dir.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p


@lru_cache
def get_settings() -> Settings:
    return Settings()
