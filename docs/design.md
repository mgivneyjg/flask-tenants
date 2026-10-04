# Why it works this way

The short version of eighteen decisions. Each one notes what was rejected,
because that is usually the more useful half.

## Isolation

**`schema_translate_map` is the source of truth; `SET LOCAL search_path` is a
backstop.** They fail in opposite directions. `search_path` alone leaks across
pooled connections if a reset is ever missed — the canonical, silent
multi-tenancy catastrophe. The translate map alone is structurally leak-proof
but invisible to `text()` SQL and any third-party library issuing its own
queries. For a library, where you do not control what callers do, covering
both is worth it.

*Rejected:* `search_path` alone, which is what `django-tenants` does — but
`django-tenants` is an application framework with much tighter control over
its own query layer than a library has over its callers.

**Both are set at `Session.after_begin`, from one function.** `SET LOCAL` is
transaction-scoped: PostgreSQL reverts it at commit *or* rollback, including
the rollback from an unhandled exception. That removes the pool-leak failure
mode structurally rather than procedurally — there is no reset to forget — and
makes the design correct under PgBouncer transaction pooling, where the
setting's lifetime and the pooled unit finally agree.

**One shared connection pool.** Per-tenant pools multiply past
`max_connections` and sit idle; you would get database-per-tenant's resource
profile with none of its isolation benefit.

## Context

**An extension-owned `ContextVar`, not `flask.g`.** Flask's `g` is itself
`ContextVar`-backed, so the two are identical inside a request and diverge
entirely outside one. A Celery worker, a CLI command or a test fixture would
otherwise need every caller to push an application context.

**No tenant plus a tenant-scoped query is an error.** A fallback to `public`
would make "forgot to set the tenant" produce wrong data instead of a stack
trace. `with public_schema():` is the explicit way to mean the shared schema.

## Models

**One `registry`, two `MetaData`.** Alembic autogenerates against a
`MetaData`, so the two table sets must be separate. But two independent
`declarative_base()` calls also create two *registries*, and string-based
`relationship()` cannot resolve across registries — a tenant model pointing at
a shared one fails with `InvalidRequestError`. Decoupling registry from
metadata gives both.

*Rejected:* `django-tenants`' `SHARED_APPS`/`TENANT_APPS` module lists, a
workaround for Django's app registry that SQLAlchemy does not need. Which
schema a table lives in belongs next to the table.

**The application owns the tenant table.** Tenants always grow
application-specific columns; a library-owned table makes that somebody else's
migration problem. The library types against a `Protocol` and ships a mixin.

**Schema names derive from an immutable surrogate key.** Slugs change, and
renaming a schema under live connections is the one migration nobody wants.
Names are also SQL identifiers and cannot be parameterised, so every one is
validated against an allowlist and quoted.

## Routing

**An ordered resolver chain, defaulting to the domain table.** The domain
table is a strict superset of the subdomain case — a subdomain is just a row —
and retrofitting custom domains later would mean a migration plus a resolver
swap in every deployment.

**The session resolver caches a choice; it does not grant access.** Membership
is re-validated against the database on every request. The alternative leaves
an eight-hour window between revoking someone's access and that taking effect.

**Signed-cookie sessions, not Redis.** Flask's default session is client-side
and verified by `SECRET_KEY`, so six nodes need no sticky sessions and no
shared store. Redis buys revocation, size and confidentiality — none
load-bearing once membership is checked per request — and the resolver reads
through the session interface either way, so adopting it later is a config
change.

## Lifecycle

**Two Alembic environments, `alembic_version` per tenant schema.** Tenants
legitimately sit at different revisions, so a global version table would
assert something false. Per-schema tables also make an interrupted run
resumable.

*Rejected:* Alembic branch labels — technically the right model for two
histories in one graph, and the part of Alembic people get wrong most often.

**One transaction per tenant, stopping at the first failure.** #212 failing
usually means a class of problem that will also hit #213.
`--continue-on-error` exists for 3am.

**A `status` command.** Drift is possible by design, so it must be
observable. Twenty lines, and the most-missed piece in homegrown versions.

**Provision by replaying migrations.** Replay cannot drift, because a new
tenant takes the path every existing tenant took.

*Rejected:* `create_all()` + `stamp head`. Fast, constant-time, and silently
omits everything a migration did outside SQLAlchemy metadata. Tenants
provisioned either side of such a migration end up different, and nothing
surfaces it for months — trading a bounded, visible cost for an unbounded,
invisible one.

**Soft-delete only.** The library never drops a schema. A healthcare contract
ending usually means a retention obligation, and keeping the most destructive
statement this code could emit out of it entirely was the conservative call.

**`for_each_tenant` is maintenance, not analytics.**

*Rejected:* generated `UNION ALL` views — on correctness, not performance.
Different-revision tenants make every union view invalid the moment a
migration stops partway, which would couple reporting correctness to atomic
migration runs.

## Proof

**The leak test ships with the library.** The design declined database-level
enforcement, so nothing turns a wrong-tenant bug into an error. The test is
the only thing that demonstrates the mechanism works, which makes it
load-bearing rather than a nicety.

**Application-level enforcement only.** Per-tenant roles with `SET LOCAL ROLE`
would have converted wrong-tenant reads into `permission denied`. The simpler
deployment won; the consequence is documented in [Limits](limits.md) rather
than left implicit.

*Rejected:* RLS, which is the right tool for the shared-table-with-`tenant_id`
model, not for physical separation.

## The full record

The complete interview — every option considered, not just the one taken — is
in the design record published alongside this project.
