"""Command-line entry points for scheduled jobs (cron / Windows Task Scheduler).

    python -m apps.marketpilot.cli run-cycle --deployment 1 [--as-of ISO] [--no-ingest]
    python -m apps.marketpilot.cli run-due [--no-ingest]
    python -m apps.marketpilot.cli alerts

`run-due` runs one cycle for every deployment that is not stopped, plus stopped
ones (which only process pending fills, e.g. liquidation orders). Prints one
JSON line per cycle. Exit code: 0 if every cycle succeeded or was partial,
1 if any failed or could not start.

`alerts` prints the current alerts as JSON. Exit code 2 if any alert is
critical, 0 otherwise, so a scheduled check (e.g. cron with MAILTO) can notify.
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime

from apps.marketpilot.config import get_settings
from apps.marketpilot.market_data import build_market_data_provider
from apps.marketpilot.services.alerts import AlertService
from apps.marketpilot.services.forward_trading import ForwardTradingService, RunCycleRequest
from packages.database.engine import create_engine, create_session_factory
from packages.market_data.errors import ProviderConfigurationError
from packages.shared.errors import AppError
from packages.shared.logging_setup import configure_logging
from packages.shared.timeutils import ensure_utc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="marketpilot", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("run-cycle", help="run one daily cycle for a deployment")
    one.add_argument("--deployment", type=int, required=True)
    one.add_argument("--as-of", type=lambda v: ensure_utc(datetime.fromisoformat(v)))
    one.add_argument("--no-ingest", action="store_true", help="use stored bars only")
    due = sub.add_parser("run-due", help="run one cycle for every deployment")
    due.add_argument("--no-ingest", action="store_true", help="use stored bars only")
    sub.add_parser("alerts", help="print current alerts; exit 2 if any is critical")
    return parser


async def _main(args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging(
        level=settings.log_level, fmt=settings.log_format, secrets=settings.secret_values()
    )
    if settings.database_url is None:
        print(json.dumps({"error": "DATABASE_URL is not set"}))
        return 1
    try:
        provider = build_market_data_provider(settings)
    except ProviderConfigurationError:
        provider = None  # cycles will record that ingestion was unavailable
    engine = create_engine(settings.database_url.get_secret_value())
    service = ForwardTradingService(
        create_session_factory(engine),
        default_feed=settings.alpaca_data_feed,
        provider=provider,
    )
    exit_code = 0
    try:
        if args.command == "alerts":
            report = await AlertService(
                create_session_factory(engine),
                default_feed=settings.alpaca_data_feed,
                provider_configured=provider is not None,
            ).evaluate()
            print(report.model_dump_json(indent=2))
            return 2 if report.counts.get("critical") else 0
        request = RunCycleRequest(as_of=getattr(args, "as_of", None), ingest=not args.no_ingest)
        if args.command == "run-cycle":
            ids = [args.deployment]
        else:
            ids = [d.id for d in await service.list_deployments()]
        for deployment_id in ids:
            try:
                cycle = await service.run_cycle(deployment_id, request)
                print(cycle.model_dump_json())
                if cycle.status == "failed":
                    exit_code = 1
            except AppError as exc:
                print(json.dumps({"deployment_id": deployment_id, "error": exc.message}))
                exit_code = 1
    finally:
        if provider is not None:
            await provider.aclose()
        await engine.dispose()
    return exit_code


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_main(_parser().parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
