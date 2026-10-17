# Connect core and external employment service

Connect owns profiles, posts, social relationships, messages, stories, meetings,
and authenticated integration APIs. Guild owns employers, quests, applications,
and related access grants. The frontend links to `VITE_GUILD_URL` (default `/guild/`)
for employment journeys. Historical Connect jobs and companies links display the
Guild destination. Search and MCP expose only the remaining core capabilities.

Experience records keep their company display name and accept an optional external
Guild employer UUID. They do not depend on Guild tables or import its models.
Existing linked experiences require an explicit mapping before migration proceeds.

Historical schema migrations remain unchanged for supported upgrades. The two
retirement migrations remove legacy runtime model state while preserving all old
physical tables and values for private archival/rollback. No private records or
fixtures belong in this source repository. Fresh installations traverse the same
migration graph; legacy tables are inert and have no routes or runtime writers.

The cutover first freezes legacy source writes, captures a complete private
snapshot, imports verified catalog records, and privately quarantines unresolved
identity-bearing rows with source IDs, reasons and exact values. Every source row
is accounted in the Guild ledger. Quarantined rows grant no user access. Before
retiring APIs, verify the staged and live importer receipts, existing Guild data,
and private archive checksums. The source rows remain intact for rollback.

Retirement also disables the persisted Celery schedule for the removed legacy
ingestion task. Its full configuration remains in the database and private
cutover archive. Reversing schema state does not automatically re-enable that
writer; restore its archived enabled flag only after reconciling later Guild
writes. All other periodic schedules retain their prior state.
