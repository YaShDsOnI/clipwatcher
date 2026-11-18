"""The `clip-watcher` command (also `python -m clipwatcher`).

    clip-watcher                       the dashboard (same as: clip-watcher dashboard)
    clip-watcher dashboard [--port N] [--no-browser]
    clip-watcher watch <channel> [more...] [options]
    clip-watcher render clips/.meta/<clip>.json [--retitle] [--retranscribe]
    clip-watcher setup [part ...]
"""
from __future__ import annotations

import sys

from . import __version__

COMMANDS = {
    "dashboard": "local web page to manage streamers and watch clips (default)",
    "watch": "record channels and clip chat spikes from the terminal",
    "render": "(re-)render clip jobs",
    "setup": "see which parts are installed and download optional ones (asks first)",
}


def usage() -> str:
    lines = [f"clip-watcher {__version__}: live-stream clipping driven by chat", "",
             "usage: clip-watcher [command] [options]", "", "commands:"]
    lines += [f"  {name:<10} {text}" for name, text in COMMANDS.items()]
    lines += ["", "run  clip-watcher <command> --help  for that command's options"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help", "help"):
        print(usage())
        return
    if argv and argv[0] in ("-V", "--version"):
        print(f"clip-watcher {__version__}")
        return
    command = argv.pop(0) if argv and argv[0] in COMMANDS else "dashboard"
    if argv and command == "dashboard" and not argv[0].startswith("-"):
        sys.exit(f"clip-watcher: unknown command {argv[0]!r}\n\n{usage()}")
    if command == "dashboard":
        from .dashboard import main as run
    elif command == "watch":
        from .watcher import main as run
    elif command == "render":
        from .clipper import main as run
    else:
        from .components import main as run
    run(argv)


if __name__ == "__main__":
    main()
