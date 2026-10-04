# CLI

```bash
pip install "flask-tenants[cli]"
export FLASK_TENANTS_MANAGER=myapp.tenancy:manager
```

`--manager` takes an import path as `module:attribute`, resolving to a
`TenantManager` or a zero-argument factory returning one. The environment
variable saves repeating it.

## `status`

Every tenant's revision against head. **Exits non-zero if any tenant is
behind**, so it works as a deployment gate.

```bash
$ flask-tenants status
head: 4f2c9a1b8e03

  ok  acme      tenant_1    4f2c9a1b8e03
  ok  globex    tenant_2    4f2c9a1b8e03
BEHIND  initech   tenant_3    9c1d4e7a2b55
 NONE  umbrella  tenant_4    -

4 tenants, 2 not at head
```

## `list`

```bash
flask-tenants list
flask-tenants list --state failed      # tenants needing a human
```

Tab-separated: key, schema, state.

## `upgrade`

```bash
flask-tenants upgrade --shared                   # the public schema
flask-tenants upgrade --all                      # every tenant
flask-tenants upgrade --tenant acme              # one tenant
flask-tenants upgrade --all --continue-on-error  # keep going past a failure
flask-tenants upgrade --all --revision 4f2c9a1b  # a specific revision
```

Shared first, then tenants — tenant tables may hold foreign keys into shared
ones.

One transaction per tenant, stopping at the first failure. Exits non-zero if
anything failed or was skipped.

## `provision`

```bash
flask-tenants provision acme
```

Creates the schema and replays migrations to head. Idempotent — safe to re-run
after a failure.

## `deactivate`

```bash
flask-tenants deactivate acme
```

Soft delete. The resolver stops serving the tenant; the schema and all its
data are retained.

!!! note "There is no `purge`"
    And no `--force` that would add one. This package never drops a schema.
    Archival and purge are a human operation with a runbook — see
    [Limits](limits.md#2-schemas-accumulate-forever).

## `run`

```bash
flask-tenants run myapp.maintenance:backfill_colour
flask-tenants run myapp.maintenance:reindex --workers 4
flask-tenants run myapp.maintenance:risky --stop-on-error
```

Runs `module:function` once per **active** tenant, with that tenant bound.
Exits non-zero if anything failed.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Everything succeeded |
| `1` | A tenant failed, was skipped, or is behind head |
