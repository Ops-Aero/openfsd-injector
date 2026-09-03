from __future__ import annotations

import argparse
import asyncio
import logging
import os

from .config import ConfigError, load_config
from .injector import Injector

log = logging.getLogger("openfsd_injector")


def main() -> None:
    parser = argparse.ArgumentParser(prog="openfsd-injector")
    parser.add_argument(
        "-c",
        "--config",
        default=os.environ.get("OPENFSD_CONFIG", "config.yaml"),
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        log.error("%s", exc)
        raise SystemExit(2) from None
    try:
        asyncio.run(Injector(cfg).run())
    except ConfigError as exc:
        log.error("%s", exc)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
