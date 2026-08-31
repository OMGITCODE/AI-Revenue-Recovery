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

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    def __getattribute__(self, name: str):
        # Allow standard/internal attributes to bypass override
        if name.startswith("_") or name in ("model_config", "model_fields", "model_computed_fields"):
            return super().__getattribute__(name)
        
        # Check environment first to support dynamic test monkeypatching
        env_key = name.upper()
        if env_key in os.environ:
            val = os.environ[env_key]
            # Perform basic type casting if field is typed in Pydantic schema
            field = self.__class__.model_fields.get(name)
            if field and field.annotation:
                try:
                    if field.annotation == int:
                        return int(val)
                    elif field.annotation == float:
                        return float(val)
                    elif field.annotation == bool:
                        return val.lower() in ("true", "1", "yes")
                except Exception:
                    pass
            return val

        return super().__getattribute__(name)

settings = Settings()