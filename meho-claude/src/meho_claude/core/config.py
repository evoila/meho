"""Pydantic Settings configuration loaded from TOML with env var overrides."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)


def _default_state_dir() -> Path:
    return Path.home() / ".meho"


class MehoSettings(BaseSettings):
    """Global configuration loaded from ~/.meho/config.toml + env vars."""

    model_config = SettingsConfigDict(
        env_prefix="MEHO_",
        env_nested_delimiter="__",
        toml_file=str(Path.home() / ".meho" / "config.toml"),
    )

    # State directory
    state_dir: Path = Field(default_factory=_default_state_dir)

    # CLI defaults
    default_timeout: int = Field(default=30, description="Default command timeout in seconds")

    # Logging
    log_level: str = Field(default="WARNING")
    log_file: str = Field(default="meho.log")

    # Debug
    debug: bool = Field(default=False)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        toml_path = Path(str(cls.model_config.get("toml_file", "")))
        sources = [init_settings, env_settings]
        if toml_path.exists():
            sources.append(TomlConfigSettingsSource(settings_cls))
        return tuple(sources)
