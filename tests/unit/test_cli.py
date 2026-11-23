from types import SimpleNamespace
from unittest.mock import AsyncMock

from app import cli


def test_admin_cli_requires_interactive_terminal(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli.sys,
        "argv",
        ["app.cli", "create-admin", "--email", "a@example.com", "--full-name", "Admin"],
    )
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    assert cli.main() == 1
    assert "interactive terminal" in capsys.readouterr().err


def test_admin_cli_confirms_password_without_command_line_argument(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli.sys,
        "argv",
        ["app.cli", "create-admin", "--email", "a@example.com", "--full-name", "Admin"],
    )
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    values = iter(["correct-password-123", "correct-password-123"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: next(values))
    create = AsyncMock()
    monkeypatch.setattr(cli, "create_admin", create)
    monkeypatch.setattr(cli, "engine", SimpleNamespace(dispose=AsyncMock()))
    assert cli.main() == 0
    create.assert_awaited_once_with("a@example.com", "Admin", "correct-password-123")
    captured = capsys.readouterr()
    assert "correct-password-123" not in captured.out + captured.err


def test_manager_is_provisioned_by_interactive_cli(monkeypatch, capsys) -> None:
    from app.modules.users.models import UserRole

    monkeypatch.setattr(
        cli.sys,
        "argv",
        ["app.cli", "create-manager", "--email", "manager@example.com", "--full-name", "Manager"],
    )
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: "synthetic-password")
    create = AsyncMock()
    monkeypatch.setattr(cli, "create_staff", create)
    monkeypatch.setattr(cli, "engine", SimpleNamespace(dispose=AsyncMock()))
    assert cli.main() == 0
    create.assert_awaited_once_with(
        "manager@example.com", "Manager", "synthetic-password", UserRole.MANAGER
    )
    assert "Manager created" in capsys.readouterr().out
