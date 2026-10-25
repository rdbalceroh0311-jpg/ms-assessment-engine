"""Operator settings: private credentials.env, then AMS_ENV_FILE/.env, then the environment."""

import os
from collections.abc import Iterable
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
ENV_KEYS = [
    "AMS_AI_PROVIDER",
    "AMS_AI_DEBUG_RETENTION_DAYS",
    "AMS_BACKUP_RETENTION_DAYS",
    "ANTHROPIC_API_KEY",
    "PAGESPEED_API_KEY",
    "CRUX_API_KEY",
    "GOOGLE_API_KEY",
    "DATAFORSEO_LOGIN",
    "DATAFORSEO_PASSWORD",
    "DATAFORSEO_LOCATION_CODE",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "AMS_GSC_CLIENT_SECRETS",
    "AMS_GSC_TOKEN_FILE",
    "AMS_GSC_PROPERTY",
    "AMS_DKIM_SELECTORS",
    "SPAMHAUS_DQS_KEY",
    "AMS_ACCESS_EMAIL",
]


def load(keys: Iterable[str] = ENV_KEYS) -> dict[str, str]:
    keys = set(keys)
    loaded = {}
    directory = Path(os.environ.get("AMS_CONFIG_DIR", Path.home() / ".config/ams-assess"))
    for file in [
        directory / "credentials.env",
        Path(os.environ.get("AMS_ENV_FILE", ROOT / ".env")),
    ]:
        if file.is_file():
            loaded.update(
                {
                    k: v
                    for k, v in dotenv_values(file, interpolate=False).items()
                    if k in keys and v is not None
                }
            )
    loaded.update({k: os.environ[k] for k in keys if k in os.environ})
    return loaded
