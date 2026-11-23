import argparse
import asyncio
import getpass
import sys
from uuid import UUID

from pydantic import ValidationError

from app.core.database import engine, session_factory
from app.core.exceptions import EmailAlreadyRegisteredError
from app.modules.tracking.outbox import requeue_failed_event
from app.modules.tracking.security import create_credential, disable_credential
from app.modules.users.models import UserRole
from app.modules.users.schemas import UserCreate
from app.modules.users.service import UserService


async def create_staff(email: str, full_name: str, password: str, role: UserRole) -> None:
    data = UserCreate(email=email, full_name=full_name, password=password)
    async with session_factory() as session:
        await UserService(session).create(data, role)


async def create_admin(email: str, full_name: str, password: str) -> None:
    await create_staff(email, full_name, password, UserRole.ADMIN)


def main() -> int:
    parser = argparse.ArgumentParser(description="Referral Analytics Service administration")
    commands = parser.add_subparsers(dest="command", required=True)
    for role in ("admin", "manager"):
        create = commands.add_parser(
            f"create-{role}", help=f"Create a {role} account interactively"
        )
        create.add_argument("--email", required=True)
        create.add_argument("--full-name", required=True)
    integration = commands.add_parser(
        "create-integration", help="Create service credentials interactively"
    )
    integration.add_argument("--source", required=True)
    disable = commands.add_parser("disable-integration", help="Disable an integration credential")
    disable.add_argument("--id", required=True, type=UUID)
    retry = commands.add_parser("retry-event", help="Requeue a failed event")
    retry.add_argument("--id", required=True, type=UUID)
    args = parser.parse_args()

    if not sys.stdin.isatty():
        print("This command requires an interactive terminal.", file=sys.stderr)
        return 1
    if args.command == "create-integration":

        async def create_service_key() -> str:
            try:
                async with session_factory() as session:
                    _, key = await create_credential(session, args.source)
                    return key
            finally:
                await engine.dispose()

        try:
            key = asyncio.run(create_service_key())
        except ValueError:
            print("Invalid source system.", file=sys.stderr)
            return 1
        print(f"Integration key (shown once): {key}")
        return 0
    if args.command in ("disable-integration", "retry-event"):

        async def execute_admin_operation() -> bool:
            try:
                async with session_factory() as session:
                    if args.command == "disable-integration":
                        return await disable_credential(session, args.id)
                    return await requeue_failed_event(session, args.id)
            finally:
                await engine.dispose()

        if not asyncio.run(execute_admin_operation()):
            print("Credential or failed event not found.", file=sys.stderr)
            return 1
        print("Operation completed.")
        return 0
    try:
        password = getpass.getpass("Password: ")
        confirmation = getpass.getpass("Confirm password: ")
        if password != confirmation:
            print("Passwords do not match.", file=sys.stderr)
            return 1

        async def run() -> None:
            try:
                if args.command == "create-manager":
                    await create_staff(args.email, args.full_name, password, UserRole.MANAGER)
                else:
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
    print("Manager created." if args.command == "create-manager" else "Administrator created.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
