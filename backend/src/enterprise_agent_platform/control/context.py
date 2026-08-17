from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RequestContext:
    tenant_id: str
    actor_id: str
    scopes: tuple[str, ...]
    request_id: str
    trace_id: str | None = None

    def __post_init__(self) -> None:
        if not self.tenant_id or not self.actor_id or not self.request_id:
            raise ValueError("tenant_id, actor_id and request_id are required")
        object.__setattr__(self, "scopes", tuple(self.scopes))
