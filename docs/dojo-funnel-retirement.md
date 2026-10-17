# Legacy Dojo funnel retirement (CON-469)

Dojo owns the active signup and usage funnel. Connect's old `/api/dojo/waitlist/`,
`/api/dojo/events/` and `/api/dojo/summary/` routes are retired. This change removes
its runtime models, serializers and views; migration 0040 removes only Django
model state. The original PostgreSQL tables and all columns remain intact so
rollback does not require reconstructing event data.

Before deploying, take and verify a complete private export of both legacy
Dojo tables in a consistent read-only transaction. Preserve each full row,
including event metadata and timestamps, in a mode-0600 archive outside Git.
Record its checksum, counts, source image and capture time in the private
cutover receipt. Confirm the legacy route has no current producer, and compare
counts again immediately before retirement. Any new rows require a fresh
archive and producer migration; do not silently discard them.

Deploy through Origin CI and verify the three retired routes return 404 on the
live Connect API. Verify the current Dojo signup/usage endpoints through Dojo's
own service and confirm its existing data is unchanged. Check PostgreSQL table
counts still match the private retirement archive. No archived email, visitor
identifier or event payload belongs in a public release receipt.

Rollback uses the previous known-good Connect image through its CI deployment
path. Migration 0040 can be reversed to 0039: it restores model state and makes
no database changes. Preserve the retained tables and private archive until
the separately reviewed migration-baseline/publication cutover is complete.
A public Connect tree must replace the old historical migration baseline;
retaining private migrations containing Dojo schema is not the final OSS split.

Regression tests exercise missing runtime routes and a real migration
forward/reverse cycle that preserves a full legacy event row byte-for-byte at
the ORM values boundary. Run the complete backend CI test recipe before push.
