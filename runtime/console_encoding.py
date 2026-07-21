"""Windows-safe console / logging helpers (AgentOps emoji logs, etc.)."""

from __future__ import annotations

import io
import logging
import sys


def _ascii_safe(text: str) -> str:
    return text.encode("ascii", errors="replace").decode("ascii")


def configure_console_encoding() -> None:
    """Prefer UTF-8 stdout/stderr with replacement for unencodable chars."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        buffer = getattr(stream, "buffer", None)
        if buffer is not None:
            try:
                wrapped = io.TextIOWrapper(
                    buffer,
                    encoding="utf-8",
                    errors="replace",
                    line_buffering=True,
                )
                setattr(sys, name, wrapped)
                continue
            except Exception:  # noqa: BLE001
                pass
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass


def patch_logging_for_windows() -> None:
    """Replace StreamHandler.emit so emoji logs never raise on cp1252 consoles."""
    if getattr(logging.StreamHandler.emit, "_vr_company_safe", False):
        return

    def safe_emit(self: logging.StreamHandler, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            stream = self.stream
            try:
                stream.write(msg + self.terminator)
            except UnicodeEncodeError:
                stream.write(_ascii_safe(msg) + self.terminator)
            self.flush()
        except Exception:  # noqa: BLE001
            # Never call handleError with UnicodeEncodeError — it re-prints the same failure.
            try:
                sys.__stderr__.write(_ascii_safe(f"logging emit failed: {record.getMessage()}\n"))
            except Exception:  # noqa: BLE001
                pass

    safe_emit._vr_company_safe = True  # type: ignore[attr-defined]
    logging.StreamHandler.emit = safe_emit  # type: ignore[method-assign]


def harden_console() -> None:
    configure_console_encoding()
    patch_logging_for_windows()
