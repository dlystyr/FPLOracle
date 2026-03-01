from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql://fpl:fpl@localhost:5432/fpl"
    redis_url: str = "redis://localhost:6379"
    fpl_base_url: str = "https://fantasy.premierleague.com/api"
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
    sync_interval: int = 21600  # 6 hours

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
