"""Copy the local JSON data (storage/data/*.json) to your GitHub Gist, e.g. before deploying to Render.

Usage (GIST_ID and GITHUB_TOKEN in .env or the environment):
  uv run python -m scripts.push_to_gist            # upload local files to the gist
  uv run python -m scripts.push_to_gist --pull     # download the gist into storage/data (backup/restore)

Uploading replaces files with the same name in the gist; other gist files are left alone.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.config import get_settings
from app.database.backends import GistBackend, LocalBackend


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pull", action="store_true", help="download gist -> local instead")
    args = parser.parse_args()

    settings = get_settings()
    if not (settings.gist_id and settings.github_token):
        print("Set GIST_ID and GITHUB_TOKEN first (in .env).")
        return 1
    local = LocalBackend(settings.data_dir)
    gist = GistBackend(settings.gist_id, settings.github_token)
    try:
        if args.pull:
            files = await gist.read_all()
            if not files:
                print("The gist has no .json files yet.")
                return 1
            await local.write(files)
            print(f"Downloaded {len(files)} files to {settings.data_dir}")
        else:
            files = await local.read_all()
            if not files:
                print(f"No JSON files in {settings.data_dir}. Run the app once first.")
                return 1
            await gist.write(files)
            size = sum(len(t.encode()) for t in files.values()) / 1024
            print(f"Uploaded {len(files)} files ({size:.0f} KB) to gist {settings.gist_id}: {', '.join(sorted(files))}")
    finally:
        await gist.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
