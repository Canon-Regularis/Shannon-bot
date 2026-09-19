"""The one place GitHub markup is allowed through, and what it is still not allowed to do.

Issues #125 and #126. Everything in `test_safe_text.py` next door is about killing every marker,
which is right for a title and a comment body. This is the opposite policy for the one block that
earns it, so the tests that matter most are the ones about what it still refuses.

The property at the bottom is the load-bearing one. The module's claim is that every `](` in what
comes out is one it wrote, and that every one it wrote points at a host GitHub serves. That is a
claim about text nobody thought of, so it is asserted over text nobody thought of.
"""

from __future__ import annotations

import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from shannon.discord_bot.rich_text import (
    _LINK,
    ALT_LIMIT,
    IMAGES_SHOWN,
    _host_of,
    _is_github,
    as_rich_text,
)
from shannon.discord_bot.safe_text import DESCRIPTION_PREVIEW_LIMIT, MESSAGE_LIMIT

pytestmark = pytest.mark.unit

ZWSP = "​"
SHOT = "https://user-images.githubusercontent.com/1/a.png"


def said(body: str) -> str:
    return as_rich_text(body).text


class TestWhatIsKeptNow:
    """Issue #125. Every one of these arrived backslashed before."""

    @pytest.mark.parametrize(
        "written",
        ["**bold**", "*italic*", "_under_", "~~struck~~", "||spoiler||", "`code`"],
    )
    def test_inline_markup_survives(self, written: str) -> None:
        assert said(f"a {written} b") == f"a {written} b"

    def test_a_fence_survives_whole(self) -> None:
        assert said("```py\nprint(1)\n```") == "```py\nprint(1)\n```"

    def test_a_list_stays_a_list(self) -> None:
        """Two occurrences, because a rule that forgot MULTILINE still matches the first line."""
        assert said("- one\n- two") == "- one\n- two"

    def test_a_star_list_is_left_alone_too(self) -> None:
        assert said("* one\n* two") == "* one\n* two"

    def test_a_plus_becomes_a_dash(self) -> None:
        """GitHub reads a `+` as a bullet and Discord does not, so a list written with one
        arrived as a paragraph of plus signs."""
        assert said("+ one\n+ two") == "- one\n- two"

    def test_an_indented_list_keeps_its_indent(self) -> None:
        """Not on the first line, which `cut` strips along with the rest of the leading space."""
        assert said("intro\n  - one\n\t- two") == "intro\n  - one\n\t- two"

    def test_a_quote_stays_quoted(self) -> None:
        assert said("> said this\n>> and this") == "> said this\n>> and this"


class TestWhatIsStillRefused:
    """The card has one voice, and a description is somebody else's words inside it."""

    def test_a_heading_loses_its_hashes(self) -> None:
        assert said("## Summary\ntext\n### Detail") == "Summary\ntext\nDetail"

    def test_a_line_leading_issue_reference_keeps_its_hash(self) -> None:
        """The rule needs the space GitHub needs for a heading. Without it this reads `#3` as a
        heading and the reference is gone with no way to tell it was ever there."""
        assert said("#3 is fixed by this\n#4 as well") == "#3 is fixed by this\n#4 as well"

    def test_subtext_is_broken_so_it_cannot_speak_as_the_bot(self) -> None:
        """`-#` renders small and grey, which is the voice every footnote this bot writes uses.
        Dead before issue #125 only by accident, because the escaping backslashed a leading dash.
        """
        assert said("-# posted by the maintainers") == f"-{ZWSP}# posted by the maintainers"

    def test_an_indented_subtext_is_broken_too(self) -> None:
        assert said("intro\n  -# quiet") == f"intro\n  -{ZWSP}# quiet"

    @pytest.mark.parametrize("written", ["<@1234567890>", "<@!1234567890>", "<@&999>", "<#42>"])
    def test_a_mention_cannot_resolve(self, written: str) -> None:
        assert written not in said(f"cc {written} please")

    @pytest.mark.parametrize("written", ["@everyone", "@here"])
    def test_a_mass_mention_cannot_resolve(self, written: str) -> None:
        assert written not in said(f"cc {written} please")

    def test_html_comments_go(self) -> None:
        """A pull request template is mostly these and they are invisible on GitHub."""
        assert said("<!-- tell us why -->real text<!-- and how -->") == "real text"

    def test_a_comment_spanning_lines_goes_too(self) -> None:
        assert said("<!--\nmulti\nline\n-->kept") == "kept"

    def test_an_unterminated_comment_is_left_alone(self) -> None:
        """A greedy match would eat the rest of the description instead."""
        assert said("<!-- never closed\nand the rest") == "<!-- never closed\nand the rest"

    def test_windows_line_endings_are_folded(self) -> None:
        assert said("## One\r\n\r\n\r\n\r\n- two") == "One\n\n- two"

    def test_a_run_of_blank_lines_collapses(self) -> None:
        assert said("one\n\n\n\n\ntwo") == "one\n\ntwo"

    @pytest.mark.parametrize("body", ["", "   ", "\n\n\n", "## ", "<!-- only a comment -->"])
    def test_a_body_that_says_nothing_comes_to_nothing(self, body: str) -> None:
        """What the block reads to decide there is no description to show."""
        assert said(body) == ""


class TestWhereALinkGoes:
    """A masked link is the one piece of markdown where what a reader sees and where they are
    taken are different strings, and anybody who can open an issue writes a description."""

    def test_a_github_link_is_left_as_written(self) -> None:
        assert said("[docs](https://github.com/o/r)") == "[docs](https://github.com/o/r)"

    @pytest.mark.parametrize(
        "url",
        [
            "https://gist.github.com/x",
            "https://raw.githubusercontent.com/o/r/f",
            "https://user-images.githubusercontent.com/1/a.png",
        ],
    )
    def test_the_hosts_github_serves_count_as_github(self, url: str) -> None:
        assert said(f"[a]({url})") == f"[a]({url})"

    def test_anywhere_else_is_named(self) -> None:
        assert said("[click here](https://evil.example/x)") == "click here (evil.example)"

    def test_a_host_that_merely_ends_in_github_is_not_github(self) -> None:
        """`github.com.evil.example` ends with `github.com` under a plain `endswith`."""
        assert said("[a](https://github.com.evil.example/x)") == "a (github.com.evil.example)"

    def test_a_link_with_no_words_is_named_by_its_host(self) -> None:
        """Discord renders a masked link with no words as its own markup."""
        assert said("[](https://evil.example/x)") == "evil.example"

    def test_a_github_link_with_no_words_is_named_too(self) -> None:
        assert said("[](https://github.com/o/r)") == "[github.com](https://github.com/o/r)"

    def test_a_homograph_host_is_shown_as_punycode(self) -> None:
        """A host spelled with a Cyrillic letter renders as `github.com` and is not one.

        Built rather than typed, because the linter refuses an ambiguous character in a
        literal, which is the same objection this test exists about.
        """
        cyrillic_u = "\u0443"

        assert "xn--" in said(f"[a](https://gith{cyrillic_u}b.com/x)")

    def test_a_host_idna_refuses_is_shown_as_written(self) -> None:
        """Being read rather than followed, so showing it wrong is worse than showing it oddly."""
        assert said("[a](https://a_b.example/x)") == "a (a_b.example)"

    @pytest.mark.parametrize(
        "url", ["javascript:alert(1)", "http://plain.example/x", "/relative/path"]
    )
    def test_anything_that_is_not_an_https_url_builds_nothing(self, url: str) -> None:
        rendered = said(f"[t]({url})")

        assert "](" not in rendered
        assert "t" in rendered

    def test_a_url_carrying_a_parenthesis_is_shown_rather_than_linked(self) -> None:
        """It does not match, which is the safe direction: it falls through to the breaker."""
        rendered = said("[a](https://en.wikipedia.org/wiki/Foo_(bar))")

        assert "](" not in rendered

    def test_a_name_in_a_label_cannot_ping(self) -> None:
        assert "<@7>" not in said("[<@7>](https://github.com/o/r)")


class TestThePictures:
    """Issue #126. Discord renders no inline image anywhere, so one has to become a component."""

    def test_an_image_comes_out_of_the_text_and_leaves_its_words(self) -> None:
        described = as_rich_text(f"here is the crash: ![the stack trace]({SHOT}) look")

        assert described.text == "here is the crash: the stack trace look"
        assert described.images == (described.images[0],)
        assert (described.images[0].url, described.images[0].alt) == (SHOT, "the stack trace")

    def test_an_image_with_no_words_leaves_nothing_behind(self) -> None:
        """What was there was a URL, so there is nothing to leave."""
        described = as_rich_text(f"![]({SHOT})")

        assert described.text == ""
        assert described.images[0].alt is None

    def test_a_picture_anywhere_but_github_is_never_fetched(self) -> None:
        """Discord fetches a gallery's media before it will accept the message, so an address in
        an issue body is an address anybody can point this bot at."""
        described = as_rich_text("![shot](https://elsewhere.example/a.png)")

        assert described.images == ()
        assert described.text == "shot"

    def test_a_picture_that_is_not_https_is_never_fetched(self) -> None:
        assert as_rich_text("![shot](http://github.com/a.png)").images == ()

    def test_four_at_most(self) -> None:
        body = "\n".join(f"![{n}](https://github.com/{n}.png)" for n in range(IMAGES_SHOWN + 6))

        assert len(as_rich_text(body).images) == IMAGES_SHOWN

    def test_the_same_picture_twice_spends_one_place(self) -> None:
        """A template repeating one badge cannot spend the gallery on it."""
        described = as_rich_text(f"![a]({SHOT})\n![b]({SHOT})")

        assert len(described.images) == 1
        assert described.images[0].alt == "a", "the first words are the ones written where it was"

    def test_a_name_in_the_words_cannot_ping(self) -> None:
        assert as_rich_text(f"![<@7>]({SHOT})").images[0].alt != "<@7>"

    def test_very_long_words_are_cut_to_what_discord_takes(self) -> None:
        described = as_rich_text(f"![{'n' * (ALT_LIMIT + 50)}]({SHOT})")

        assert described.images[0].alt is not None
        assert len(described.images[0].alt) == ALT_LIMIT

    def test_a_picture_past_the_preview_limit_is_still_shown(self) -> None:
        """The ordering that is the feature. A bug report whose screenshots all sit past character
        seven hundred is an ordinary bug report, and cutting first would show none of them."""
        described = as_rich_text("w" * (DESCRIPTION_PREVIEW_LIMIT + 100) + f"\n![shot]({SHOT})")

        assert described.images[0].url == SHOT


class TestClosingWhatWasLeftOpen:
    """Each text display is drawn on its own, so this should not be necessary. It is two lines and
    the card is built out of matched pairs, which is not a layout to bet on a renderer."""

    @pytest.mark.parametrize("written", ["**", "***unclosed", "a ** b"])
    def test_an_odd_bold_marker_is_closed(self, written: str) -> None:
        assert said(written).count("**") % 2 == 0

    @pytest.mark.parametrize("written", ["```", "```py\ncode", "a ``` b"])
    def test_an_open_fence_is_closed(self, written: str) -> None:
        assert said(written).count("```") % 2 == 0

    def test_a_cut_through_a_marker_is_closed_too(self) -> None:
        """The cut lands wherever it lands, and closing afterwards is what makes that safe."""
        assert said("**" + "w" * DESCRIPTION_PREVIEW_LIMIT).count("**") % 2 == 0

    def test_matched_markers_are_left_alone(self) -> None:
        assert said("**bold**") == "**bold**"


class TestTheHostRule:
    def test_a_non_https_url_has_no_host(self) -> None:
        assert _host_of("http://github.com/x") is None

    def test_a_url_with_no_host_at_all_has_none(self) -> None:
        assert _host_of("https:///just/a/path") is None

    def test_a_host_urlparse_refuses_has_none(self) -> None:
        """An unbalanced bracket reads as a malformed IPv6 host and `urlparse` raises."""
        assert _host_of("https://[bad/x") is None

    @pytest.mark.parametrize("host", ["github.com", "gist.github.com", "githubusercontent.com"])
    def test_what_github_serves(self, host: str) -> None:
        assert _is_github(host)

    @pytest.mark.parametrize(
        "host", ["notgithub.com", "github.com.evil.example", "example.github.io"]
    )
    def test_what_it_does_not(self, host: str) -> None:
        assert not _is_github(host)


@given(st.text(max_size=3000))
@settings(max_examples=300)
def test_nothing_typed_can_reach_out_of_the_card(body: str) -> None:
    """The claim the whole module rests on, asserted over text nobody thought of.

    Every `](` that comes out is one this module wrote, every one of those points at a host GitHub
    serves, no mention survives, and neither marker that can run past its own line is left open.
    """
    described = as_rich_text(body)

    assert described.text.count("](") == len(_LINK.findall(described.text))
    for link in _LINK.finditer(described.text):
        host = _host_of(link.group(2))
        assert host is not None and _is_github(host), link.group(2)

    assert described.text.count("**") % 2 == 0
    assert described.text.count("```") % 2 == 0
    assert not re.search(r"<@[!&]?\d+>|<#\d+>", described.text)
    assert "@everyone" not in described.text
    assert "@here" not in described.text
    assert len(described.text) < MESSAGE_LIMIT

    assert len(described.images) <= IMAGES_SHOWN
    assert len({image.url for image in described.images}) == len(described.images)
    for image in described.images:
        assert image.url.startswith("https://")
        assert _is_github(_host_of(image.url) or "")
