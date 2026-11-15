import argparse
import os


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="dechat",
        description="Serverless LAN chat. Opens the chat interface in your browser.",
    )
    parser.add_argument(
        "--terminal", action="store_true",
        help="run the terminal interface instead of the web interface (requires --debug)",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="print discovery and connection debug output in the terminal",
    )
    args = parser.parse_args(argv)
    if args.terminal and not args.debug:
        parser.error("the terminal interface is for debugging, pass both --terminal and --debug")
    return args


def main(argv=None):
    args = parse_args(argv)
    if args.debug:
        # core.config reads this on import, so it must be set before anything else loads
        os.environ["DECHAT_DEBUG"] = "1"

    if args.terminal:
        from dechat.frontends.tui import run_tui
        run_tui()
    else:
        from dechat.frontends.web.app import run_web
        raise SystemExit(run_web(debug=args.debug))
