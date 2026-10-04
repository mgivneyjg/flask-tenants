# Minimal flask-tenants example

A complete, runnable multi-tenant Flask app: two tenants, shared reference
data, a tenant-scoped table, and a Celery worker that stays in the right
tenant.

## Run it

```bash
docker run -d --name ft-example-pg \
  -e POSTGRES_USER=app -e POSTGRES_PASSWORD=app -e POSTGRES_DB=app \
  -p 5432:5432 postgres:16-alpine

pip install -e "../..[flask,cli]" "psycopg[binary]"

python -m myapp.bootstrap          # create schemas and two tenants
flask --app myapp.app:create_app run --port 5000
```

```bash
curl -H "Host: acme.example.test"   localhost:5000/patients
curl -H "Host: globex.example.test" localhost:5000/patients
curl localhost:5000/healthz        # no tenant needed
```

Each tenant sees only its own rows, from the same tables, in the same
database, over the same connection pool.

## Worker

```bash
celery -A myapp.worker.celery worker -l info     # needs a running Redis
python -m myapp.enqueue
```

`myapp/worker.py` shows both approaches side by side: an explicit
`tenant_key` argument, and automatic header propagation.
