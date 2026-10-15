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
    ALT_LIMIT,
    COMMIT_LINK_LIMIT,
    IMAGES_SHOWN,
    Images,
    _host_of,
    _is_github,
    as_note_text,
    as_rich_text,
)
from shannon.discord_bot.safe_text import (
    COMMENT_PREVIEW_LIMIT,
    DESCRIPTION_PREVIEW_LIMIT,
    MESSAGE_LIMIT,
)
from shannon.domain.models import RepositorySnapshot
from shannon.domain.text import LINK

pytestmark = pytest.mark.unit

ZWSP = "​"
SHOT = "https://user-images.githubusercontent.com/1/a.png"

REPO = RepositorySnapshot(github_repo_id=1, owner="o", name="n", html_url="https://github.com/o/n")

# The longest names GitHub allows, which is where a commit link costs the most.
LONGEST = RepositorySnapshot(
    github_repo_id=2,
    owner="o" * 39,
    name="n" * 100,
    html_url=f"https://github.com/{'o' * 39}/{'n' * 100}",
)

# A name GitHub would never issue, so no address can be built into it.
BROKEN = RepositorySnapshot(
    github_repo_id=3, owner="o", name="..", html_url="https://github.com/o/.."
)

FULL = "a1b2c3d4" * 5


def said(body: str) -> str:
    return as_rich_text(body).text


def linked(body: str, *, repository: RepositorySnapshot = REPO) -> str:
    return as_rich_text(body, repository=repository).text


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

    def test_a_host_that_merely_looks_wrong_is_encoded_to_itself(self) -> None:
        """An underscore is not something `idna` objects to, which is easy to assume it is.

        The codec waves through any ASCII label that fits in sixty-three characters without
        applying the rules that would refuse this one, so a host nobody could register comes
        back exactly as it went in, down the ordinary path rather than the one below.
        """
        assert said("[a](https://a_b.example/x)") == "a (a_b.example)"

    def test_a_host_idna_refuses_is_shown_as_written(self) -> None:
        """Being read rather than followed, so showing it wrong is worse than showing it oddly.

        A label over sixty-three characters is what the codec refuses outright, and it is built
        rather than typed for the reason the homograph test above gives.
        """
        too_long = "n" * 70

        assert said(f"[a](https://{too_long}.example/x)") == f"a ({too_long}.example)"

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


class TestWhereThePicturesAreNotLifted:
    """Issue #166. The same conversion for a body that gets no gallery under it.

    A note and a commit message take this policy. A gallery under every screenshot-heavy comment,
    or under every commit of a push, is noise.
    """

    def links(self, body: str) -> str:
        return as_rich_text(body, images=Images.AS_LINKS).text

    def test_an_image_keeps_its_address_as_a_link(self) -> None:
        """The regression this policy exists to avoid. `_shown` leaves the alt text where the
        markup was, which deletes the address - right when the picture is shown underneath, and a
        bare word when it is not."""
        said_it = self.links(f"look: ![the stack trace]({SHOT})")

        assert f"[the stack trace]({SHOT})" in said_it
        assert said_it.count("](") == 1, "the bang was left in front of the link"

    def test_nothing_is_lifted(self) -> None:
        assert as_rich_text(f"![shot]({SHOT})", images=Images.AS_LINKS).images == ()

    def test_an_image_pointing_off_github_is_named_rather_than_followed(self) -> None:
        """The same host rule every other link gets, because dropping the bang is all this does -
        what is left is a link, and `_linked` judges it like any other."""
        said_it = self.links("![shot](https://elsewhere.example/a.png)")

        assert "](" not in said_it
        assert "elsewhere.example" in said_it

    def test_the_block_still_lifts_by_default(self) -> None:
        """The parameterisation moved nothing: the default is what the description always did."""
        assert as_rich_text(f"![shot]({SHOT})").images != ()


class TestANoteBody:
    """`as_note_text`, which the renderer and the service are both handed. Issue #166."""

    def test_markup_survives(self) -> None:
        said_it = as_note_text("## Repro\n- run `make test`", repository=REPO)

        assert "- run `make test`" in said_it
        assert "\\" not in said_it, "the markup this keeps was escaped"

    def test_it_is_cut_to_the_comment_limit(self) -> None:
        """Not the description's, though the two share a value today. A note that borrowed the
        wrong name would follow it the day they stop agreeing."""
        assert as_note_text("y" * (COMMENT_PREVIEW_LIMIT + 400), repository=REPO).count("y") == (
            COMMENT_PREVIEW_LIMIT
        )

    def test_a_plus_list_becomes_a_list(self) -> None:
        """GitHub reads `+` as a bullet and Discord does not. A comment used to arrive as a
        paragraph of plus signs - neither escaped nor converted, which was a defect rather than
        formatting lost on purpose."""
        assert as_note_text("+ one\n+ two", repository=REPO).startswith("- one")

    def test_bot_subtext_cannot_be_forged(self) -> None:
        """`-#` is Discord's subtext and every footnote this bot writes uses it, so a body able to
        produce one could put words in the bot's mouth. It was dead by accident before - the `-`
        happened to be escaped - and is broken deliberately now."""
        assert not as_note_text("-# posted by the maintainers", repository=REPO).startswith("-#")

    def test_an_image_keeps_its_address(self) -> None:
        assert SHOT in as_note_text(f"![shot]({SHOT})", repository=REPO)

    def test_a_hash_in_a_note_links_to_its_commit(self) -> None:
        """Issue #186: the repository every note is handed is the one its hashes link into."""
        assert as_note_text("Fixed in abc1234.", repository=REPO) == (
            "Fixed in [abc1234](https://github.com/o/n/commit/abc1234)."
        )


class TestTheCommitsABodyNames:
    """Issue #186. A hash GitHub would link is linked here too, into the body's own repository."""

    def test_a_hash_links_to_its_commit(self) -> None:
        assert linked("Fixed in abc1234.") == (
            "Fixed in [abc1234](https://github.com/o/n/commit/abc1234)."
        )

    def test_a_whole_hash_is_labelled_short_and_addressed_whole(self) -> None:
        """As GitHub shows one, and the address is every character that was written."""
        assert linked(f"This reverts commit {FULL}.") == (
            f"This reverts commit [a1b2c3d](https://github.com/o/n/commit/{FULL})."
        )

    def test_another_repositorys_commit_links_there_and_says_so(self) -> None:
        assert linked("see other/repo@abc1234") == (
            "see [other/repo@abc1234](https://github.com/other/repo/commit/abc1234)"
        )

    def test_another_repositorys_whole_hash_is_labelled_short_too(self) -> None:
        """The cherry-pick a commit message names, where a whole hash is what `git` writes."""
        assert linked(f"cherry picked from octo/lib@{FULL}") == (
            f"cherry picked from [octo/lib@a1b2c3d](https://github.com/octo/lib/commit/{FULL})"
        )

    def test_without_a_repository_nothing_is_linked(self) -> None:
        """A draft card's body: it belongs to no repository for a bare hash to belong to."""
        assert said("Fixed in abc1234.") == "Fixed in abc1234."

    def test_one_with_no_address_is_left_as_written_and_the_next_still_linked(self) -> None:
        """Into a repository GitHub could not have issued, so the bare hash goes nowhere - and
        the one naming its own repository after it is still read."""
        assert linked("abc1234 and o/r@def5678", repository=BROKEN) == (
            "abc1234 and [o/r@def5678](https://github.com/o/r/commit/def5678)"
        )

    def test_five_at_most(self) -> None:
        hashes = [f"{n}abcdef" for n in range(1, COMMIT_LINK_LIMIT + 2)]

        out = linked(" ".join(hashes))

        assert out.count("](") == COMMIT_LINK_LIMIT
        assert out.endswith(f" {hashes[-1]}"), "the one past the limit was not left as written"

    def test_five_is_the_number_the_ceiling_was_worked_out_for(self) -> None:
        """Each link is up to 178 characters the cut no longer counts, and the ceiling in
        `as_rich_text`'s docstring is five of those. A larger number would still pass every test
        above, which read the constant, and quietly break the arithmetic."""
        assert COMMIT_LINK_LIMIT == 5

    def test_a_hash_written_twice_counts_twice(self) -> None:
        """Each link is characters the cut has stopped counting, whichever commit it names."""
        assert linked("abc1234 " * (COMMIT_LINK_LIMIT + 1)).count("](") == COMMIT_LINK_LIMIT

    def test_a_link_costs_the_preview_none_of_its_words(self) -> None:
        """Linked after the cut, so the limit goes on counting what a reader sees."""
        body = "abc1234 " + "w" * (DESCRIPTION_PREVIEW_LIMIT - 8)

        assert linked(body).count("w") == DESCRIPTION_PREVIEW_LIMIT - 8

    def test_a_hash_the_cut_ran_into_is_left_as_written(self) -> None:
        """What is left is a prefix the cut chose rather than the writer."""
        body = "w" * (DESCRIPTION_PREVIEW_LIMIT - 10) + " abc1234def5678"

        out = linked(body)

        assert "](" not in out
        assert out.endswith("abc1234de…")

    def test_a_fence_the_cut_left_open_links_nothing_inside_it(self) -> None:
        out = linked("```\n" + "abc1234 " * 100)

        assert "](" not in out
        assert out.count("```") == 2, "the fence was not closed"

    def test_a_hash_in_a_link_to_elsewhere_stays_inside_it(self) -> None:
        """Linked before the author's own links are judged, so this one is still whole: run after,
        it would be a GitHub link standing beside a host it does not go to."""
        assert linked("[fixed in abc1234](https://evil.example/x)") == (
            "fixed in abc1234 (evil.example)"
        )

    def test_a_hash_written_as_a_links_address_is_not_followed(self) -> None:
        out = linked("[x](abc1234)")

        assert "](" not in out
        assert "github.com" not in out

    def test_a_hash_in_code_is_left_as_code(self) -> None:
        assert linked("run `git show abc1234`") == "run `git show abc1234`"

    def test_bold_closes_around_a_link(self) -> None:
        assert linked("**abc1234") == "**[abc1234](https://github.com/o/n/commit/abc1234)**"

    def test_subtext_stays_broken_in_front_of_one(self) -> None:
        assert linked("-# abc1234") == (
            f"-{ZWSP}# [abc1234](https://github.com/o/n/commit/abc1234)"
        )


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

    assert described.text.count("](") == len(LINK.findall(described.text))
    for link in LINK.finditer(described.text):
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


# Hashes spliced into everything that could go wrong around one: code, links, addresses, names,
# escapes, the cut mark. No GitHub host, so the only GitHub links that can come out are the ones
# the commit step wrote - and none of the label-less GitHub link that grows on its own, a gap that
# predates issue #186 and is not what this asserts about.
_AROUND_A_HASH = [
    "abc1234",
    "0123abc",
    FULL,
    "o/r@def5678",
    "other/repo@abc1234",
    " ",
    "\n",
    "x",
    "1",
    "`",
    "``",
    "```",
    "[",
    "]",
    "(",
    ")",
    "](",
    "https://",
    "http://e.x/",
    "evil.example",
    "@",
    "#",
    ".",
    "..",
    ":",
    "-",
    "_",
    "*",
    "**",
    "~~",
    "||",
    "!",
    "\\",
    "<",
    ">",
    "<@123>",
    "…",
]


@given(
    st.lists(st.sampled_from(_AROUND_A_HASH), max_size=300).map("".join),
    st.sampled_from([REPO, LONGEST, BROKEN]),
)
@settings(max_examples=300)
def test_nothing_typed_can_reach_out_of_the_card_naming_commits(
    body: str, repository: RepositorySnapshot
) -> None:
    """Issue #186. The same claims as above with commits linked, and at most five links more.

    Short draws, which is where hashes meet the hazards beside them; `BROKEN` is a repository no
    address can be built into. The ceiling is the property after this one, which is built to reach
    it.
    """
    shown = as_rich_text(body, repository=repository).text
    plain = as_rich_text(body).text

    assert shown.count("](") == len(LINK.findall(shown))
    for link in LINK.finditer(shown):
        host = _host_of(link.group(2))
        assert host is not None and _is_github(host), link.group(2)

    assert shown.count("**") % 2 == 0
    assert shown.count("```") % 2 == 0
    assert not re.search(r"<@[!&]?\d+>|<#\d+>", shown)
    assert len(shown) < MESSAGE_LIMIT
    assert 0 <= len(LINK.findall(shown)) - len(LINK.findall(plain)) <= COMMIT_LINK_LIMIT


# Every link it may write, in front where the cut cannot reach them, and enough of the soup behind
# that every draw is cut. Measured: 200/200 draws are cut and carry five links, and the longest
# came out at 1612 characters.
_FIVE_IN_FRONT = " ".join(["abc1234"] * COMMIT_LINK_LIMIT) + " "


@given(st.lists(st.sampled_from(_AROUND_A_HASH), min_size=250, max_size=400).map("".join))
@settings(max_examples=200)
def test_five_links_into_the_longest_names_still_fit_a_message(soup: str) -> None:
    """Issue #186. The ceiling in `as_rich_text`'s docstring, where it is tightest: a body cut at
    the preview limit and carrying every link it may, into the repository whose names make each
    link cost the most."""
    shown = as_rich_text(_FIVE_IN_FRONT + soup, repository=LONGEST).text

    assert "…" in shown, "the draw was never cut, so this measured nothing"
    assert len(LINK.findall(shown)) == COMMIT_LINK_LIMIT
    assert len(shown) < MESSAGE_LIMIT
