import argparse

import uvicorn

from app.core.config import get_settings
from app.core.logging import logging_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the Referral Analytics API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        access_log=False,
        log_config=logging_config("api", get_settings().log_level),
    )


if __name__ == "__main__":
    main()
