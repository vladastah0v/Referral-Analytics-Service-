class AuthenticationError(Exception):
    """Authentication failed without disclosing account or session details."""


class PermissionDeniedError(Exception):
    """The authenticated principal lacks the required role."""


class EmailAlreadyRegisteredError(Exception):
    """The normalized email address is already registered."""
