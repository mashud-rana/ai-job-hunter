"""Run one collection + matching cycle manually.

Usage:
  uv run python -m scripts.run_collect_once                       # all sources (respects per-source intervals)
  uv run python -m scripts.run_collect_once remotive remoteok --force
  uv run python -m scripts.run_collect_once --no-collect --rescore  # re-score existing jobs only
Options:
  --force       ignore per-source minimum intervals
  --no-match    skip matching
  --no-collect  skip collection (match only)
  --rescore     delete existing matches for the current profile version first (scoring rules / AI changed)
  --notify      send Telegram alerts for new high matches
"""

from __future__ import annotations

import argparse
import asyncio

from app.config import get_settings
from app.container import Container
from app.database.repositories import MatchRepository, ProfileRepository
from app.logging import configure_logging


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("sources", nargs="*", help="collector keys (default: all)")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--no-match", action="store_true")
    parser.add_argument("--no-collect", action="store_true")
    parser.add_argument("--rescore", action="store_true")
    parser.add_argument("--notify", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging("WARNING", json_logs=False)
    container = Container.build(settings)
    await container.startup()
    try:
        if args.rescore:
            async with container.db.session() as session:
                ref = await ProfileRepository(session).active_ref()
                if ref:
                    removed = await MatchRepository(session).delete_for_profile(ref)
                    print(f"Deleted {removed} existing matches for re-scoring.")
        if not args.no_collect:
            summary = await container.services.collection.run(only=args.sources or None, force=args.force)
            print("Source results:")
            for r in summary.results:
                print(
                    f"  {r.source:<10} {r.status:<8} fetched={r.fetched:<4} new={r.new:<4} dup={r.duplicates:<4}"
                    f" invalid={r.invalid:<3} irrelevant={r.irrelevant:<4} {r.error or ''}"
                )
        if not args.no_match:
            matching = await container.services.matching.run()
            print("Matching:", matching.as_dict())
        if args.notify:
            print("Alerts sent:", await container.services.notifications.send_high_match_alerts())
    finally:
        await container.aclose()


if __name__ == "__main__":
    asyncio.run(main())
