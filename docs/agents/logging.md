# Logging

<!-- Moved verbatim from CLAUDE.md; keep it in sync with the code. -->

### Logging

`logutil.setup_logging()` (called from `app.py`) installs a console formatter
for the `docker compose logs` stream: `HH:MM:SS`, then the message. Warnings
and errors get an icon, which makes their line protrude rather than adding a
column everything else has to pay for. Milestone messages (ready, album start,
album complete) carry an inline icon from `logutil.ICON_*`. A `DedupeFilter`
collapses consecutive identical lines and reports the streak as its own INFO
line — background loops used to repeat the same sentence hundreds of times.
A streak that never ends is still reported every 100 repeats or 60 s.
Prefer fixing repetition at the source (log at DEBUG when nothing changed,
as `lidarr_sync` does) and treat the filter as a safety net.

`logutil.section(logger, ..., icon=...)` opens a visual block: the line is
preceded by a blank line, so startup and each album run read as separate
paragraphs rather than one flat scroll. `logutil.milestone()` adds an icon
without the break. Messages that belong *inside* an album run are prefixed
with three spaces so they sit under its header — keep that convention when
adding album-flow logging; a line carrying an icon has that indent stripped,
so icons always sit one space from their text and protrude from the flow.

Each track of an album run is processed inside `logutil.track_label("02")`
(a `contextvars` label copied onto records by `TrackLabelFilter`), and the
formatter prints it after the indent/icon: `   [02] Search phase: …`. Tracks
download in parallel, so without it their lines interleave anonymously. A
thread started inside a track must run in `contextvars.copy_context()` to
keep the label (`_download_candidate_threaded` does).

**Icons must be East_Asian_Width "W" (exactly two columns).** The alignment
depends on it, and a test enforces it. This is why the warning icon is not
the obvious "⚠" (U+26A0): that codepoint is Ambiguous width, so terminals
disagree and it left a visible gap.
