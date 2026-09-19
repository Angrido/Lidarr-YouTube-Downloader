"""Tests for the console log formatting."""

import logging

import logutil


def _record(msg, level=logging.INFO, args=()):
    return logging.LogRecord(
        name="t", level=level, pathname=__file__, lineno=1,
        msg=msg, args=args, exc_info=None,
    )


class TestConsoleFormatter:
    def test_info_has_no_icon_but_keeps_the_column(self):
        line = logutil.ConsoleFormatter().format(_record("hello"))
        # "HH:MM:SS" + two spaces + the empty icon column.
        assert line.endswith("hello")
        assert line[8:] == "  " + logutil._NO_ICON + "hello"

    def test_warning_and_error_get_icons(self):
        fmt = logutil.ConsoleFormatter()
        warn = fmt.format(_record("careful", logging.WARNING))
        err = fmt.format(_record("broken", logging.ERROR))
        assert logutil._LEVEL_ICONS[logging.WARNING] in warn
        assert "careful" in warn
        assert logutil._LEVEL_ICONS[logging.ERROR] in err
        assert "broken" in err

    def test_every_icon_is_exactly_two_columns_wide(self):
        # The whole alignment scheme assumes it. U+26A0 ("⚠") is Ambiguous
        # width — terminals disagree — which left a visible gap after the
        # warning icon. Fail here rather than shipping a misaligned log.
        import unicodedata
        icons = list(logutil._LEVEL_ICONS.values()) + [
            logutil.ICON_APP, logutil.ICON_ALBUM,
            logutil.ICON_DONE, logutil.ICON_TRACK,
        ]
        for icon in icons:
            assert len(icon) == 1, f"{icon!r} has a variation selector"
            assert unicodedata.east_asian_width(icon) == "W", (
                f"{icon!r} is not two columns wide"
            )

    def test_icon_is_followed_by_exactly_one_space(self):
        fmt = logutil.ConsoleFormatter()
        # Album sub-steps carry their own indent; an icon line must not
        # inherit it, or the gap after the emoji varies per message.
        line = fmt.format(_record("   indented step", logging.WARNING))
        icon = logutil._LEVEL_ICONS[logging.WARNING]
        assert icon + " indented step" in line

    def test_explicit_icon_beats_the_level_icon(self):
        rec = _record("done", logging.INFO)
        rec.icon = logutil.ICON_DONE
        line = logutil.ConsoleFormatter().format(rec)
        assert logutil.ICON_DONE + " done" in line

    def test_milestone_helper_sets_the_icon(self):
        seen = []

        class _L:
            def info(self, msg, *args, **kwargs):
                seen.append(kwargs)

        logutil.milestone(_L(), "hi", icon=logutil.ICON_APP)
        assert seen[0]["extra"]["icon"] == logutil.ICON_APP
        assert "section" not in seen[0]["extra"]

    def test_icons_align_with_plain_lines(self):
        # Every icon renders two columns, so the message must start at the
        # same offset whether or not there is an icon.
        fmt = logutil.ConsoleFormatter()
        plain = fmt.format(_record("x"))
        err = fmt.format(_record("x", logging.ERROR))
        # One code point wide icon + a space == the 3-space empty column.
        assert plain.index("x") == err.index("x") + 1

    def test_multiline_messages_stay_indented(self):
        line = logutil.ConsoleFormatter().format(_record("a\nb"))
        second = line.split("\n")[1]
        assert second.startswith(" " * 8)
        assert second.strip() == "b"

    def test_formats_args(self):
        line = logutil.ConsoleFormatter().format(_record("n=%d", args=(7,)))
        assert line.endswith("n=7")


class TestDedupeFilter:
    def test_consecutive_duplicates_are_suppressed(self):
        f = logutil.DedupeFilter()
        assert f.filter(_record("same")) is True
        assert f.filter(_record("same")) is False
        assert f.filter(_record("same")) is False

    def test_different_message_passes_again(self):
        f = logutil.DedupeFilter()
        f.filter(_record("a"))
        f.filter(_record("a"))
        assert f.filter(_record("b")) is True
        assert f.filter(_record("a")) is True

    def test_streak_reported_as_its_own_info_line(self):
        emitted = []

        class _H:
            def emit(self, record):
                emitted.append(record)

        f = logutil.DedupeFilter(_H())
        f.filter(_record("same"))
        for _ in range(4):
            f.filter(_record("same"))
        # An ERROR breaks the streak; the note must NOT inherit its level.
        assert f.filter(_record("boom", logging.ERROR)) is True
        assert len(emitted) == 1
        assert emitted[0].levelno == logging.INFO
        assert "repeated 4" in emitted[0].getMessage()

    def test_no_note_without_a_streak(self):
        emitted = []

        class _H:
            def emit(self, record):
                emitted.append(record)

        f = logutil.DedupeFilter(_H())
        f.filter(_record("a"))
        f.filter(_record("b"))
        assert emitted == []


def test_setup_logging_replaces_handlers_and_is_idempotent():
    root = logging.getLogger()
    original = list(root.handlers)
    try:
        logutil.setup_logging()
        first = list(root.handlers)
        logutil.setup_logging()
        assert len(root.handlers) == len(first) == 1
        assert isinstance(
            root.handlers[0].formatter, logutil.ConsoleFormatter
        )
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in original:
            root.addHandler(h)


class TestSectionBreaks:
    def test_section_record_gets_a_blank_line_before_it(self):
        rec = _record("Album X")
        rec.section = True
        line = logutil.ConsoleFormatter().format(rec)
        assert line.startswith("\n")
        # The blank line is a real blank line, not an indented one.
        assert line.split("\n")[0] == ""
        assert "Album X" in line

    def test_plain_records_are_not_spaced(self):
        line = logutil.ConsoleFormatter().format(_record("routine"))
        assert not line.startswith("\n")

    def test_section_helper_marks_the_record(self):
        seen = []

        class _L:
            def info(self, msg, *args, **kwargs):
                seen.append((msg, args, kwargs))

        logutil.section(_L(), "%s hi", "X")
        msg, args, kwargs = seen[0]
        assert msg == "%s hi" and args == ("X",)
        assert kwargs["extra"]["section"] is True

    def test_section_helper_keeps_caller_extra(self):
        seen = []

        class _L:
            def info(self, msg, *args, **kwargs):
                seen.append(kwargs)

        logutil.section(_L(), "x", extra={"other": 1})
        assert seen[0]["extra"] == {"other": 1, "section": True}
