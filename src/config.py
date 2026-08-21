from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

settings = Settings()