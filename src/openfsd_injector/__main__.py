from __future__ import annotations

import argparse
import asyncio
import logging

from .config import load_config
from .injector import Injector


def main() -> None:
    parser = argparse.ArgumentParser(prog="openfsd-injector")
    parser.add_argument("-c", "--config", default="config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config(args.config)
    asyncio.run(Injector(cfg).run())


if __name__ == "__main__":
    main()
