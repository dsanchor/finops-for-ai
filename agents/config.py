import os
from typing import Literal
from urllib.parse import urlsplit

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

AgentKind = Literal["risk", "customer"]


class Settings(BaseModel):
    kind: AgentKind
    openai_base_url: str
    model: str = Field(min_length=1)
    host: str = "0.0.0.0"
    port: int = Field(ge=1, le=65535)
    public_url: str
    auth_mode: Literal["default", "cli", "managed_identity"] = "default"
    managed_identity_client_id: str | None = None
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh"] | None = None
    request_timeout: float = Field(default=180, gt=0)
    max_retries: int = Field(default=2, ge=0)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("openai_base_url")
    @classmethod
    def validate_openai_base_url(cls, value: str) -> str:
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "OpenAI base URL must be an absolute HTTPS URL "
                "without credentials, query or fragment"
            )
        return value.rstrip("/")

    @field_validator("public_url")
    @classmethod
    def validate_public_url(cls, value: str) -> str:
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.netloc or url.query or url.fragment:
            raise ValueError("Public URL must be an absolute HTTP(S) URL without query or fragment")
        return value.rstrip("/")

    @classmethod
    def from_env(cls, kind: AgentKind) -> "Settings":
        load_dotenv()
        prefix = "RISK" if kind == "risk" else "CUSTOMER"
        port = os.getenv(f"{prefix}_PORT", "8001" if kind == "risk" else "8002")
        values: dict[str, object] = {
            "kind": kind,
            "openai_base_url": os.getenv(f"{prefix}_OPENAI_BASE_URL")
            or os.getenv("OPENAI_BASE_URL", ""),
            "model": os.getenv(
                f"{prefix}_MODEL_DEPLOYMENT", "gpt-5.6-sol" if kind == "risk" else "gpt-5.4-mini"
            ),
            "host": os.getenv(f"{prefix}_HOST", "0.0.0.0"),
            "port": port,
            "public_url": os.getenv(f"{prefix}_PUBLIC_URL", f"http://localhost:{port}"),
        }
        for field in (
            "auth_mode",
            "managed_identity_client_id",
            "reasoning_effort",
            "request_timeout",
            "max_retries",
            "log_level",
        ):
            value = os.getenv(f"{prefix}_{field.upper()}", os.getenv(field.upper()))
            if value is not None and value.strip():
                values[field] = value
        return cls.model_validate(values)
