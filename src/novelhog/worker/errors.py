"""Error summaries shared by the worker stages."""

_SUMMARY_LIMIT = 300


def unreachable_message(error: Exception) -> str:
    """Return `Site unreachable: <message>` on one line, cut to the same length as `summarize`."""
    return "Site unreachable: " + " ".join(str(error).split())[:_SUMMARY_LIMIT]


def summarize(error: Exception) -> str:
    """Return `<ExceptionType>: <message>` on one line, cut to a short length."""
    text = " ".join(str(error).split())[:_SUMMARY_LIMIT]
    return f"{type(error).__name__}: {text}"
