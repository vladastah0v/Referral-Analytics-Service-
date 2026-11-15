import argparse
import asyncio
import getpass
import sys

from pydantic import ValidationError

from app.core.database import engine, session_factory
from app.core.exceptions import EmailAlreadyRegisteredError
from app.modules.users.models import UserRole
from app.modules.users.schemas import UserCreate
from app.modules.users.service import UserService


async def create_admin(email: str, full_name: str, password: str) -> None:
    data = UserCreate(email=email, full_name=full_name, password=password)
    async with session_factory() as session:
        await UserService(session).create(data, UserRole.ADMIN)


def main() -> int:
    parser = argparse.ArgumentParser(description="Referral Analytics Service administration")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-admin", help="Create an administrator interactively")
    create.add_argument("--email", required=True)
    create.add_argument("--full-name", required=True)
    args = parser.parse_args()

    if not sys.stdin.isatty():
        print("Administrator creation requires an interactive terminal.", file=sys.stderr)
        return 1
    try:
        password = getpass.getpass("Password: ")
        confirmation = getpass.getpass("Confirm password: ")
        if password != confirmation:
            print("Passwords do not match.", file=sys.stderr)
            return 1

        async def run() -> None:
            try:
                await create_admin(args.email, args.full_name, password)
            finally:
                await engine.dispose()

        asyncio.run(run())
    except ValidationError:
        print("Invalid email, name, or password (12–128 characters required).", file=sys.stderr)
        return 1
    except EmailAlreadyRegisteredError:
        print("Email is already registered.", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.", file=sys.stderr)
        return 1
    print("Administrator created.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
