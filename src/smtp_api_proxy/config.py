from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProxySettings(BaseSettings):
    # --- SMTP Server Network Interface Configuration ---
    SMTP_HOST: str = Field(default="0.0.0.0", description="The IP interface to bind the SMTP proxy to.")
    SMTP_PORT: int = Field(default=1025, description="The non-privileged port to listen for incoming SMTP mail.")

    # --- Vendor Routing Toggle ---
    # Must explicitly match either "sendgrid" or "mailgun"
    EMAIL_PROVIDER: Literal["sendgrid", "mailgun"]

    # --- Twilio SendGrid Configuration Layout ---
    SENDGRID_API_KEY: str | None = Field(default=None, description="The secret authorization API key for SendGrid v3 calls.")

    # --- Mailgun Configuration Layout ---
    MAILGUN_API_KEY: str | None = Field(default=None, description="The private HTTP API authorization key for Mailgun.")
    MAILGUN_DOMAIN: str | None = Field(default=None, description="The verified sending domain namespace configured inside Mailgun.")
    MAILGUN_REGION: Literal["us", "eu"] = Field(default="us", description="The targeted Mailgun account infrastructure datacenter.")

    # --- Outbound Network Proxy Interface ---
    HTTP_PROXY: str | None = Field(default=None, description="The absolute corporate or system web proxy URL to tunnel requests through.")

    # --- Pydantic Engine Settings Config ---
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"  # Gracefully ignores unmapped environment keys
    )

    # --- Conditional Architecture Validation Rules ---
    @model_validator(mode="after")
    def validate_active_provider_requirements(self) -> ProxySettings:
        """Ensures that the required credential payloads exist for the active provider."""
        if self.EMAIL_PROVIDER == "sendgrid" and not self.SENDGRID_API_KEY:
            raise ValueError("A valid SENDGRID_API_KEY must be provided when EMAIL_PROVIDER is set to 'sendgrid'.")
        
        if self.EMAIL_PROVIDER == "mailgun":
            missing_fields = []
            if not self.MAILGUN_API_KEY:
                missing_fields.append("MAILGUN_API_KEY")
            if not self.MAILGUN_DOMAIN:
                missing_fields.append("MAILGUN_DOMAIN")
            
            if missing_fields:
                raise ValueError(f"Missing mandatory Mailgun parameter(s): {', '.join(missing_fields)}.")
                
        return self

# Singleton instantiation object used across the application package space
settings = ProxySettings()