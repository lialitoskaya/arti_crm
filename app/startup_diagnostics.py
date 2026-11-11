"""Private, value-free startup diagnostics and a safe WSGI failure response."""
from __future__ import annotations

import logging
import sys
from typing import Any, Callable, Iterable


_LOGGER = logging.getLogger("arti_crm.startup")
STARTUP_FAILURE_MESSAGE = "CRM could not start. Contact the administrator."


def report_startup_failure(error: Exception) -> None:
    """Log frame locations, never exception arguments, source lines or locals."""
    error_type = type(error)
    lines = [f"CRM startup failed ({error_type.__name__})."]
    if (error_type.__module__, error_type.__name__) == ("app.auth_bootstrap", "AuthBootstrapError"):
        lines.append("Check BOOTSTRAP_ADMIN_USERNAME and BOOTSTRAP_ADMIN_PASSWORD for an empty user database.")
    elif (error_type.__module__, error_type.__name__) == ("app.runtime_config", "RuntimeConfigurationError"):
        lines.append("Check DATABASE_PATH and select the intended existing database; no automatic move is performed.")
    traceback = error.__traceback__
    while traceback is not None:
        code = traceback.tb_frame.f_code
        lines.append(f"  {code.co_filename}:{traceback.tb_lineno} in {code.co_name}")
        traceback = traceback.tb_next
    try:
        _LOGGER.error("\n".join(lines))
    except Exception:
        # Broken logging must not expose an exception or prevent the safe HTTP 500.
        try:
            sys.stderr.write("CRM startup failed; private diagnostics are unavailable.\n")
        except (OSError, ValueError, AttributeError):
            return


def startup_failure_application(
    environ: dict[str, Any], start_response: Callable[..., Any],
) -> Iterable[bytes]:
    body = STARTUP_FAILURE_MESSAGE.encode("utf-8")
    start_response("500 Internal Server Error", [
        ("Content-Type", "text/plain; charset=utf-8"),
        ("Cache-Control", "no-store"),
        ("Content-Length", str(len(body))),
    ])
    return [body]
