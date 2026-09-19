"""Console log formatting for the container log.

The log is the main window into what the app is doing and it is read as a
`docker compose logs` stream, so it is formatted for a human rather than
for a parser: a short timestamp, an icon only where something actually
matters, and consecutive duplicate lines collapsed instead of scrolling the
same sentence hundreds of times.

Icons are deliberately sparse — warnings and errors get one automatically,
and a handful of milestone messages (startup, an album starting, an album
finishing) carry one inline. Everything else stays plain so the important
lines stand out.
"""

import logging
import threading
import time

# Milestone icons, used inline by the few messages that deserve one.
ICON_APP = "🚀"
ICON_ALBUM = "🎵"
ICON_DONE = "🎉"
ICON_TRACK = "✅"

_LEVEL_ICONS = {
    logging.WARNING: "❗",
    logging.ERROR: "❌",
    logging.CRITICAL: "🔥",
}
# Terminal alignment rule: every icon used here must be East_Asian_Width
# "W", i.e. exactly two columns in any terminal, so one trailing space
# always lines the text up with the three spaces used when there is no
# icon. This rules out the obvious warning triangle U+26A0 ("⚠"), which is
# Ambiguous width — some terminals draw it in one column and some in two,
# so it cannot be aligned and left a visible gap. A test enforces this.
_NO_ICON = "   "


class DedupeFilter(logging.Filter):
    """Collapse consecutive identical messages.

    Background loops (the Lidarr sync in particular) log the same sentence
    every cycle; hundreds of identical lines bury everything that matters.
    Repeats are swallowed and reported as a single count when a different
    message finally comes through.
    """

    def __init__(self, handler=None):
        super().__init__()
        self._handler = handler
        self._last = None
        self._repeats = 0
        self._lock = threading.Lock()

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:
            return True
        with self._lock:
            if message == self._last:
                self._repeats += 1
                return False
            repeats = self._repeats
            self._last = message
            self._repeats = 0
        if repeats and self._handler is not None:
            # Report the streak as its own INFO line. Folding it into the
            # record that broke the streak would give it that record's
            # level, so a repeated info line could surface wearing an
            # error's icon. Emit straight to the handler: handler.emit()
            # does not re-run filters, so this cannot recurse.
            note = logging.LogRecord(
                name=record.name, level=logging.INFO, pathname=record.pathname,
                lineno=record.lineno,
                msg="(previous line repeated %d×)", args=(repeats,),
                exc_info=None,
            )
            try:
                self._handler.emit(note)
            except Exception:
                pass
        return True


def section(logger, msg, *args, icon=None, **kwargs):
    """Log an INFO line that opens a new block, preceded by a blank line.

    A container log is read as one long scroll; without breathing room the
    start of an album run is indistinguishable from the chatter around it.
    ``icon`` puts a milestone icon in the icon column.
    """
    extra = dict(kwargs.pop("extra", None) or {})
    extra["section"] = True
    if icon:
        extra["icon"] = icon
    logger.info(msg, *args, extra=extra, **kwargs)


def milestone(logger, msg, *args, icon=ICON_TRACK, **kwargs):
    """Log an INFO line carrying a milestone icon, without a block break."""
    extra = dict(kwargs.pop("extra", None) or {})
    extra["icon"] = icon
    logger.info(msg, *args, extra=extra, **kwargs)


class ConsoleFormatter(logging.Formatter):
    """`HH:MM:SS  <icon>  message`, with the icon only when it earns one."""

    def format(self, record):
        stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
        # An explicit per-record icon (a milestone) wins over the level's,
        # and crucially occupies the same column instead of being embedded
        # in the message, where it would sit three columns further right.
        icon = getattr(record, "icon", None) or _LEVEL_ICONS.get(record.levelno)
        message = record.getMessage()
        if icon:
            # Exactly one space between an icon and its text, always. Album
            # sub-steps carry their own leading indent; keeping it here
            # would push the text away from the icon by a varying amount.
            # Dropping it also makes icon lines protrude from the indented
            # flow, which is what you want for a milestone or a problem.
            message = message.lstrip()
            icon = f"{icon} "
        else:
            icon = _NO_ICON
        # Multi-line messages keep their continuation lines aligned under
        # the text column instead of hugging the left edge.
        indent = " " * (len(stamp) + 2 + len(_NO_ICON))
        message = message.replace("\n", "\n" + indent)
        line = f"{stamp}  {icon}{message}"
        if getattr(record, "section", False):
            line = "\n" + line
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        if record.stack_info:
            line += "\n" + self.formatStack(record.stack_info)
        return line


def setup_logging(level=logging.INFO):
    """Install the console formatter and dedupe filter on the root logger.

    Safe to call more than once: the handler is replaced, not stacked.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(ConsoleFormatter())
    handler.addFilter(DedupeFilter(handler))
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    return handler
