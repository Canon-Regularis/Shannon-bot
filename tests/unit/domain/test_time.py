from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from shannon.discord_bot.formatting import format_comment
from shannon.domain.enums import ObjectType
from shannon.domain.models import Actor, CommentSnapshot, RepositorySnapshot
from shannon.domain.time import as_utc
from shannon.github.mapping import parse_timestamp

NOON_UTC = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def test_an_aware_timestamp_is_left_alone() -> None:
    assert as_utc(NOON_UTC) is NOON_UTC


def test_an_offset_is_preserved_rather_than_rewritten() -> None:
    ahead = datetime(2026, 8, 11, 12, 0, tzinfo=timezone(timedelta(hours=5)))

    assert as_utc(ahead) is ahead
    assert as_utc(ahead).timestamp() == ahead.timestamp()


def test_a_naive_timestamp_is_read_as_utc() -> None:
    naive = datetime(2026, 8, 11, 12, 0)

    assert as_utc(naive).tzinfo is UTC
    assert as_utc(naive).timestamp() == NOON_UTC.timestamp()


class TestParsing:
    def test_githubs_own_format_is_aware(self) -> None:
        parsed = parse_timestamp("2026-08-11T12:00:00Z")

        assert parsed is not None
        assert parsed.tzinfo is not None
        assert parsed.timestamp() == NOON_UTC.timestamp()

    def test_a_payload_without_an_offset_is_still_aware(self) -> None:
        """Left naive, this would later be read as the host's local time."""
        parsed = parse_timestamp("2026-08-11T12:00:00")

        assert parsed is not None
        assert parsed.tzinfo is not None
        assert parsed.timestamp() == NOON_UTC.timestamp()

    def test_an_explicit_offset_survives(self) -> None:
        parsed = parse_timestamp("2026-08-11T12:00:00+05:00")

        assert parsed is not None
        assert parsed.timestamp() == NOON_UTC.timestamp() - 5 * 3600

    def test_nonsense_is_none(self) -> None:
        assert parse_timestamp("whenever") is None
        assert parse_timestamp("") is None
        assert parse_timestamp(None) is None
        assert parse_timestamp(17) is None


def test_a_rendered_timestamp_does_not_depend_on_the_host_timezone() -> None:
    """The epoch seconds Discord is handed have to mean the same thing on every machine."""
    repo = RepositorySnapshot(github_repo_id=1, owner="o", name="n", html_url="u")
    naive = CommentSnapshot(
        repository=repo,
        item_number=1,
        comment_id=1,
        object_type=ObjectType.ISSUE,
        html_url="",
        body="",
        author=Actor("octocat"),
        created_at=datetime(2026, 8, 11, 12, 0),
    )
    aware = CommentSnapshot(
        repository=repo,
        item_number=1,
        comment_id=1,
        object_type=ObjectType.ISSUE,
        html_url="",
        body="",
        author=Actor("octocat"),
        created_at=NOON_UTC,
    )

    assert format_comment(naive) == format_comment(aware)
    assert f"<t:{int(NOON_UTC.timestamp())}:f>" in format_comment(naive)
