"""Load and validate settings from the environment (.env locally, Secrets in CI)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

_HEX32 = re.compile(r"[0-9a-fA-F]{32}")


class ConfigError(RuntimeError):
    pass


def normalize_id(raw: str) -> str:
    """Accept a bare ID, a dashed UUID, or a full Notion URL; return 32 hex chars."""
    cleaned = raw.strip().split("?")[0].replace("-", "")
    matches = _HEX32.findall(cleaned)
    if not matches:
        raise ConfigError(f"Not a valid Notion ID: {raw!r}")
    return matches[-1].lower()


def normalize_drive_id(raw: str) -> str:
    """Accept a bare folder ID or a pasted Drive URL (with ?query junk)."""
    value = raw.strip().split("?")[0].split("#")[0].rstrip("/")
    return value.rsplit("/", 1)[-1]


def _id_list(value: str | None) -> list[str]:
    return [normalize_id(v) for v in (value or "").split(",") if v.strip()]


@dataclass
class Config:
    notion_api_key: str
    output_dir: Path
    # Optional filters. Empty = back up everything shared with the connection.
    database_ids: list[str] = field(default_factory=list)
    page_ids: list[str] = field(default_factory=list)
    gdrive_folder_id: str = ""
    gdrive_service_account_file: str = ""
    gdrive_service_account_json: str = ""

    def require_drive(self) -> None:
        if not self.gdrive_folder_id:
            raise ConfigError("GDRIVE_FOLDER_ID is not set.")
        if not (self.gdrive_service_account_json or self.gdrive_service_account_file):
            raise ConfigError(
                "Set GDRIVE_SERVICE_ACCOUNT_JSON (CI) or GDRIVE_SERVICE_ACCOUNT_FILE (local)."
            )


def load_config(env_file: str | None = ".env") -> Config:
    if env_file and Path(env_file).exists():
        # Real environment variables (e.g. GitHub Secrets) win over the file.
        load_dotenv(env_file, override=False)

    key = os.getenv("NOTION_API_KEY", "").strip()
    if not key or key == "ntn_replace_me":
        raise ConfigError("NOTION_API_KEY is not set. Add it to .env (see .env.example).")

    return Config(
        notion_api_key=key,
        output_dir=Path(os.getenv("OUTPUT_DIR", "./dist").strip() or "./dist"),
        database_ids=_id_list(os.getenv("NOTION_DATABASE_IDS")),
        page_ids=_id_list(os.getenv("NOTION_PAGE_IDS")),
        gdrive_folder_id=normalize_drive_id(os.getenv("GDRIVE_FOLDER_ID", "")),
        gdrive_service_account_file=os.getenv("GDRIVE_SERVICE_ACCOUNT_FILE", "").strip(),
        gdrive_service_account_json=os.getenv("GDRIVE_SERVICE_ACCOUNT_JSON", "").strip(),
    )
