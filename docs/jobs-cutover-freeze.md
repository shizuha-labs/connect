# Jobs cutover write freeze

Deploy this code to every Connect API/worker replica first. Record the source
image and current Guild private backup. Freeze just the six jobs/company tables:

```sh
python manage.py freeze_jobs_for_cutover freeze
python manage.py freeze_jobs_for_cutover status
python manage.py export_jobs_cutover_snapshot --output /private/connect-final.json.gz
```

Freeze atomically waits for in-flight writes and installs PostgreSQL triggers.
A failed lock or transaction leaves no partial freeze. The triggers reject API,
worker, admin and bulk INSERT/UPDATE/DELETE writes; Connect messaging and other
core tables remain writable. API mutations return 503 with Retry-After; public
job detail reads continue without incrementing counters. Wait for all replicas
to run the deployed source before freezing so older job-detail requests do not
attempt counter updates. Status must list all six frozen tables.

The exporter refuses to run without all six triggers. It captures all physical
columns in a repeatable-read, read-only transaction, including retained legacy
Dojo tables and profile experiences. It creates a new mode-0600 gzip file and
prints only checksum/count metadata. Copy that archive to private durable
storage, verify decompression and both digests, and update the private identity
map against this final capture. Do not import a historical unfrozen snapshot.

Keep the freeze in place through the Guild dry-run, import, exact count and
source-row verification, idempotent replay and privacy checks. Keep the source
archive and old tables. Only then retire Connect jobs routes and the harvesting
schedule. A PostgreSQL write rejection from an old background job is expected
during this controlled interval; it must not be treated as permission to thaw.

Rollback before migration needs only the explicit command below. After migration,
reconcile any Guild writes before restoring Connect writers; do not blindly
allow both systems to diverge.

```sh
python manage.py freeze_jobs_for_cutover thaw
python manage.py freeze_jobs_for_cutover status
```

Both freeze and thaw are idempotent. Thaw removes only the named cutover triggers
and guard function; it does not delete or rewrite any source row.
