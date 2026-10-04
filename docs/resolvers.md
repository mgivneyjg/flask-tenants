# Resolving the tenant

A **resolver** turns a request into a tenant key, or returns `None` to defer.
Resolvers form an ordered chain; the first non-`None` wins.

```python
from flask_tenants import DomainResolver, SessionResolver, SubdomainResolver

manager = TenantManager(
    ...,
    resolvers=[
        SubdomainResolver("example.com"),   # free, no lookup
        DomainResolver(registry),           # vanity domains
        SessionResolver(),                  # bare-domain fallback
    ],
)
```

The interface is deliberately trivial — a callable taking the request and
returning a key or `None`. If none of the shipped resolvers fit, writing your
own is about ten lines.

## The shipped resolvers

### `SubdomainResolver`

`acme.app.example.com` → `acme`. No database lookup, so it is the cheapest
option, but it cannot serve a tenant on its own domain.

```python
SubdomainResolver("app.example.com", ignore=("www", "api", "admin"))
```

`base_domain` is required so that `app.example.com` itself resolves to no
tenant rather than to `app`.

!!! warning "Set `__tenant_key__ = \"slug\"` on your tenant model"
    This resolver yields the subdomain label — `"acme"` — not your primary
    key. Unless the registry looks tenants up by the same attribute, the very
    first request fails with
    `operator does not exist: integer = character varying`. See
    [Routing key vs. surrogate key](models.md#routing-key-vs-surrogate-key).

### `DomainResolver` — the default

Looks the full hostname up in your domain table. A strict superset of the
subdomain case — a subdomain is just a row — which is why it is the
recommended default despite costing a lookup. Retrofitting custom domains
later would otherwise mean a migration plus a resolver swap in every
deployment.

```python
DomainResolver(registry)
```

### `PathPrefixResolver`

`/t/acme/patients` → `acme`. The only resolver that costs nothing in DNS or
TLS, which makes it a good fit for internal tools and local development.

```python
PathPrefixResolver("t")
```

### `HeaderResolver`

`X-Tenant-ID: acme`. Natural for an API behind a separate frontend, where
subdomains are pure overhead.

```python
HeaderResolver("X-Tenant-ID")
```

A header is caller-supplied, so this grants nothing on its own — see the
authorization section below.

### `SessionResolver`

Reads the tenant the user chose at login.

```python
SessionResolver("tenant_id")
```

The flow it supports: the user logs in, you look up which tenants they belong
to, ask them to pick if there is more than one, store the choice in the
session, and clear it on logout.

```python
@app.post("/login")
def login():
    user = authenticate(request.form["email"], request.form["password"])
    memberships = user.memberships
    if len(memberships) == 1:
        session["tenant_id"] = memberships[0].tenant_id
        return redirect("/")
    return render_template("choose_tenant.html", memberships=memberships)


@app.post("/switch-tenant")
def switch_tenant():
    wanted = request.form["tenant_id"]
    if not user_belongs_to(current_user, wanted):     # (1)!
        abort(403)
    session["tenant_id"] = wanted
    return redirect("/")


@app.post("/logout")
def logout():
    session.clear()
    return redirect("/login")
```

1.  Check at the point of choosing **and** on every request. The session stores
    a preference, not a grant.

## Authorization: the session is a cache, not an authority

The first four resolvers are **stateless** — the tenant is derivable from the
request and the answer does not depend on who is logged in.
`SessionResolver` is not, and that difference carries an obligation.

**Membership must be re-validated on every request.** If the session were
authoritative, someone whose access to Acme is revoked at 10:00 would keep
reading Acme's data until their session expired.

```python
def check_membership(tenant, request) -> bool:
    user_id = session.get("user_id")
    if user_id is None:
        return False
    with Session() as db:
        return db.execute(
            select(Membership.id).where(
                Membership.user_id == user_id,
                Membership.tenant_id == tenant.tenant_key,
                Membership.revoked_at.is_(None),
            )
        ).first() is not None


manager = TenantManager(..., membership_check=check_membership)
```

The cost is one indexed lookup on a small shared table, almost certainly served
from PostgreSQL's buffer cache. If it ever shows up in a profile, memoize it
for a few seconds — but do not let the cache outlive the grant by hours.

The same applies to `HeaderResolver`: a header names a tenant, it does not
authorize one.

## Sessions and horizontal scaling

!!! note "You do not need Redis to run on six nodes"
    Flask's default session is a **signed cookie**, stored in the browser and
    verified with `SECRET_KEY`. Every node can read it, so there are no sticky
    sessions and no shared store. Node affinity is only required for
    server-side sessions with a *local* backend.

    Redis buys server-side revocation, size beyond ~4KB, and keeping session
    contents off the client — all real, none of them load-bearing once
    membership is checked per request. `SessionResolver` reads through Flask's
    session interface either way, so adopting Flask-Session with Redis later
    is an application config change and no library work.

Note also that Flask's cookie is **signed, not encrypted**: the user cannot
tamper with it, but they can decode and read it. A tenant key is low
sensitivity; be careful about what else you put there.

## The two-tab problem

The session is shared across tabs. A user who belongs to Acme and Globex can
open both, switch tenant in one, and have the other operate on the wrong data
while showing the wrong UI.

The stateless resolvers are immune by construction. If it matters, echo the
active tenant into each page and reject mismatches:

```python
@app.before_request
def reject_stale_tab():
    claimed = request.headers.get("X-Active-Tenant")
    if claimed and claimed != str(current_tenant_key()):
        abort(409, "This tab is on a different tenant. Reload.")
```

## Requests that belong to no tenant

A health check, the marketing site, the signup flow and a cross-tenant admin
console all resolve to nothing. By default those run in the **shared schema**,
because they have to work before any tenant exists.

Routes that require a tenant opt in:

```python
from flask_tenants import tenant_required


@app.get("/patients")
@tenant_required
def list_patients():
    ...
```

Flip the default globally if your deployment has no public surface at all:

```python
app.config["TENANTS_STRICT_ROUTES"] = True
```

## Writing your own

```python
from flask_tenants import TenantResolver


class JWTClaimResolver(TenantResolver):
    """Reads the tenant from a verified JWT claim."""

    def resolve(self, request):
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not token:
            return None
        return decode_and_verify(token).get("tenant")
```

A plain function works too — the chain wraps it:

```python
resolvers = [lambda req: req.args.get("tenant")]
```
