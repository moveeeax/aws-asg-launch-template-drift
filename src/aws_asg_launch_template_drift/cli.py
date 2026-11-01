"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .analyze import PayloadError, analyse, exit_code
from .report import render

EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aws-asg-launch-template-drift",
        description=(
            "Report Auto Scaling Groups whose running instances no longer match the "
            "launch template version the group is configured with."
        ),
    )
    parser.add_argument("--region", help="AWS region to query")
    parser.add_argument(
        "--name",
        action="append",
        default=[],
        metavar="ASG",
        help="limit to this Auto Scaling Group (repeatable)",
    )
    parser.add_argument(
        "--tag",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="limit to groups carrying this tag (repeatable)",
    )
    parser.add_argument(
        "--critical-pct",
        type=float,
        default=50.0,
        metavar="PCT",
        help="stale share above which a group is critical (default 50)",
    )
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=30,
        metavar="DAYS",
        help="age of the oldest stale instance that makes a group critical (default 30)",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json", "markdown"),
        default="text",
        help="output format (default text)",
    )
    parser.add_argument(
        "--fail-on",
        choices=("warn", "critical", "none"),
        default="none",
        help="severity that makes the command exit 1 (default none)",
    )
    parser.add_argument(
        "--from-json",
        metavar="PATH",
        help="replay a saved payload instead of calling AWS ('-' reads stdin)",
    )
    parser.add_argument(
        "--dump-payload",
        metavar="PATH",
        help="write the collected payload to PATH so it can be replayed later",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _load_payload(path: str) -> dict:
    try:
        if path == "-":
            return json.load(sys.stdin)
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise PayloadError(f"no such file: {path}") from None
    except json.JSONDecodeError as exc:
        raise PayloadError(f"{path} is not valid JSON: {exc}") from None


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.critical_pct < 0 or args.critical_pct > 100:
        print("--critical-pct must be between 0 and 100", file=sys.stderr)
        return EXIT_USAGE
    if args.max_age_days < 0:
        print("--max-age-days must not be negative", file=sys.stderr)
        return EXIT_USAGE

    try:
        if args.from_json:
            payload = _load_payload(args.from_json)
        else:
            from .collect import collect

            payload = collect(region=args.region, names=args.name, tags=args.tag)

        if args.dump_payload:
            with open(args.dump_payload, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, default=str)

        result = analyse(
            payload,
            critical_pct=args.critical_pct,
            max_age_days=args.max_age_days,
        )
    except PayloadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    print(render(result, args.format))
    return exit_code(result, args.fail_on)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
