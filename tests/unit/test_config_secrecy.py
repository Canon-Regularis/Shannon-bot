from __future__ import annotations

import json
import logging

import pytest

from shannon.config import Settings

# Deliberately not shaped like real credentials. A string carrying GitHub's `ghp_` prefix would
# trip push protection and secret scanners, and the masking under test does not care what the
# value looks like.
BOT_TOKEN = "placeholder-discord-bot-token"
API_TOKEN = "placeholder-github-api-token"
WEBHOOK_SECRET = "placeholder-webhook-secret"
DATABASE_URL = "postgresql+asyncpg://shannon:not-a-real-password@localhost:5433/shannon"

LEAKS = (BOT_TOKEN, API_TOKEN, WEBHOOK_SECRET, "not-a-real-password")


@pytest.fixture
def settings() -> Settings:
    return Settings(
        discord_token=BOT_TOKEN,
        github_token=API_TOKEN,
        github_webhook_secret=WEBHOOK_SECRET,
        database_url=DATABASE_URL,
    )


def assert_hides_everything(text: str) -> None:
    for secret in LEAKS:
        assert secret not in text, f"{secret!r} leaked into {text!r}"


def test_repr_hides_credentials(settings: Settings) -> None:
    assert_hides_everything(repr(settings))


def test_str_hides_credentials(settings: Settings) -> None:
    assert_hides_everything(str(settings))


def test_model_dump_hides_credentials(settings: Settings) -> None:
    assert_hides_everything(str(settings.model_dump()))


def test_json_serialisation_hides_credentials(settings: Settings) -> None:
    assert_hides_everything(settings.model_dump_json())


def test_logging_the_settings_object_hides_credentials(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """The failure mode this guards against is one careless logger call."""
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("shannon.test").debug("configuration: %s", settings)

    assert_hides_everything(caplog.text)


def test_an_unhandled_error_carrying_the_settings_hides_credentials(
    settings: Settings,
) -> None:
    error = RuntimeError(f"startup failed with {settings!r}")

    assert_hides_everything(str(error))


def test_the_values_are_still_readable_when_asked_for(settings: Settings) -> None:
    assert settings.discord_token.get_secret_value() == BOT_TOKEN
    assert settings.github_token.get_secret_value() == API_TOKEN
    assert settings.github_webhook_secret.get_secret_value() == WEBHOOK_SECRET
    assert settings.database_url.get_secret_value() == DATABASE_URL


def test_an_unset_secret_reads_as_empty() -> None:
    """Absence has to stay detectable, since an unset token means "run without the bot"."""
    bare = Settings()

    assert bare.discord_token.get_secret_value() == ""
    assert bare.github_token.get_secret_value() == ""
    assert bare.github_webhook_secret.get_secret_value() == ""
    # SecretStr defines __len__, so an empty one is falsy. Worth pinning down, because the
    # startup path and the webhook route both branch on a credential being absent.
    assert not bare.discord_token
    assert Settings(discord_token=BOT_TOKEN).discord_token


def test_credentials_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHANNON_DISCORD_TOKEN", BOT_TOKEN)

    assert Settings().discord_token.get_secret_value() == BOT_TOKEN


def test_role_names_are_not_secret() -> None:
    """Only credentials are masked. Configuration that is safe to print stays printable."""
    text = repr(Settings(role_reviewer="Maintainers"))

    assert "Maintainers" in text


def test_the_example_env_file_holds_no_real_credentials() -> None:
    from pathlib import Path

    example = Path(__file__).parents[2] / ".env.example"
    content = example.read_text(encoding="utf-8")

    for line in content.splitlines():
        if not line.startswith("SHANNON_") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if not any(word in name for word in ("TOKEN", "SECRET")):
            continue
        assert value == "" or value.startswith("your-"), f"{name} looks like a real value"


def test_no_credential_reaches_the_api_response_model() -> None:
    """The settings object lives on app.state, so anything that serialises it must stay safe."""
    payload = json.loads(Settings(discord_token=BOT_TOKEN).model_dump_json())

    assert payload["discord_token"] == "**********"
