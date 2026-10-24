"""Domain errors, mapped to HTTP responses only in `api`."""


class DomainError(Exception):
    """Base class of errors that carry a `snake_case` code, a message and optional details."""

    def __init__(self, code: str, message: str, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class NotFound(DomainError):  # noqa: N818 - names fixed by the spec
    """An entity does not exist (or its identifier is malformed)."""

    def __init__(self, entity: str, id: str) -> None:  # noqa: A002 - mirrors the API field name
        super().__init__("not_found", f"{entity} not found", {"entity": entity, "id": id})
        self.entity = entity
        self.id = id


class Conflict(DomainError):  # noqa: N818 - names fixed by the spec
    """The request conflicts with the current state."""


class ValidationFailed(DomainError):  # noqa: N818 - names fixed by the spec
    """The request is well-formed but its content is not acceptable."""


class ServiceUnavailable(DomainError):  # noqa: N818 - names fixed by the spec
    """The service cannot handle the request yet (for example, hosts were never published)."""
