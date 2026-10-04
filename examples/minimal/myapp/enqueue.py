"""Enqueue one task each way, to show both propagation styles working."""

from __future__ import annotations

from myapp.tenancy import manager
from myapp.worker import count_patients_explicit, count_patients_implicit


def main() -> None:
    acme = manager.registry.require(1)

    # Explicit: the tenant travels as an argument.
    print(count_patients_explicit.delay(tenant_key=acme.tenant_key).get(timeout=10))

    # Implicit: the tenant travels in the message headers, captured here.
    with manager.tenant_context(acme):
        print(count_patients_implicit.delay().get(timeout=10))


if __name__ == "__main__":
    main()
