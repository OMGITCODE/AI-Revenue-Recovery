import os
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # Logging Configuration
    log_level: str = "INFO"
    log_format: str = "console"

    # API Security & Authentication
    cors_origins: str = "*"
    recoveriq_api_key: str = ""

    # Twilio Integration
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_from: str = "whatsapp:+14155238886"
    twilio_sms_from: str = ""
    demo_recipient_whatsapp: str = ""
    demo_recipient_sms: str = ""

    # Razorpay Integration
    razorpay_webhook_secret: str = ""

    # Setu Account Aggregator Integration
    setu_client_id: str = ""
    setu_client_secret: str = ""

    # OpenAI & Gemini LLM Integration (Fail-safe Inbound Intent Classification)
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.6-flash"
    llm_provider: str = "auto"  # auto | gemini | openai
    llm_rate_limit_per_minute: int = 30
    llm_aggregate_rate_limit_per_minute: int = 120
    llm_global_daily_cap: int = 500

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    def reload(self) -> "Settings":
        """
        Reloads configuration from ambient environment variables and .env file.
        Provides a clean, explicit way to refresh settings in test fixtures
        or when runtime environment variables change.
        """
        fresh = Settings()
        for field in self.__class__.model_fields:
            setattr(self, field, getattr(fresh, field))
        return self


settings = Settings()