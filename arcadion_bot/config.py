from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    discord_token: str
    guild_id: int | None
    database_path: str


def load_settings() -> Settings:
    project_root = Path(__file__).resolve().parent.parent
    env_path = project_root / ".env"
    load_dotenv(dotenv_path=env_path, override=True)

    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token or token == "pega_aqui_tu_token_real":
        raise RuntimeError(
            f"DISCORD_TOKEN is not configured in {env_path}. "
            "Open that file, replace pega_aqui_tu_token_real with the real Bot Token, and save the changes."
        )

    guild_id_raw = os.getenv("GUILD_ID", "").strip()
    return Settings(
        discord_token=token,
        guild_id=int(guild_id_raw) if guild_id_raw else None,
        database_path=os.getenv("DATABASE_PATH", "data/arcadion.sqlite3").strip(),
    )