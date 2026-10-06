"""Process-wide facts set once at start-up. Import-light on purpose."""

#: True in the long-lived MCP server process (set by the CLI before serving),
#: False in one-shot CLI commands and in tests.
in_server_process = False


def mark_server_process() -> None:
    """Record that this interpreter is the long-lived MCP server."""
    global in_server_process
    in_server_process = True
