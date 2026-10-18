"""Catalog vocabulary shared by migration 0003 and the ingestion worker.

The migration turns each tuple into a CHECK constraint, so a value the worker
writes that is missing here fails in the database rather than silently widening
a column. Ingest writes `STORED`. Embed writes `READY` after the vector commits.
`TABLES` stays the six ingest tables: embeddings are granted separately and are
insert-only.
"""

TABLES = (
    "providers",
    "creators",
    "source_records",
    "rights_evidence",
    "assets",
    "jobs",
)

# Internal discovery classifications. CC0 is the only one this product treats as
# eligible; PUBLIC_DOMAIN_MARK and OTHER_OPEN are deliberately distinct from it.
RIGHTS_CC0 = "CC0"
RIGHTS_PUBLIC_DOMAIN_MARK = "PUBLIC_DOMAIN_MARK"
RIGHTS_OTHER_OPEN = "OTHER_OPEN"
RIGHTS_UNKNOWN = "UNKNOWN"
RIGHTS_REJECTED = "REJECTED"
RIGHTS_CLASSIFICATIONS = (
    RIGHTS_CC0,
    RIGHTS_PUBLIC_DOMAIN_MARK,
    RIGHTS_OTHER_OPEN,
    RIGHTS_UNKNOWN,
    RIGHTS_REJECTED,
)

VERIFICATION_VERIFIED = "VERIFIED"
VERIFICATION_REJECTED = "REJECTED"
VERIFICATION_UNVERIFIED = "UNVERIFIED"
VERIFICATION_STATUSES = (
    VERIFICATION_VERIFIED,
    VERIFICATION_REJECTED,
    VERIFICATION_UNVERIFIED,
)

# How the evidence was established. M1 only records provider_policy: the Met's
# published Open Access policy plus the item flag.
METHOD_PROVIDER_POLICY = "provider_policy"
METHOD_ITEM_STATEMENT = "item_statement"
METHOD_OPERATOR_REVIEW = "operator_review"
VERIFICATION_METHODS = (
    METHOD_PROVIDER_POLICY,
    METHOD_ITEM_STATEMENT,
    METHOD_OPERATOR_REVIEW,
)

ELIGIBILITY_DISCOVERED = "DISCOVERED"
ELIGIBILITY_RIGHTS_CHECK = "RIGHTS_CHECK"
ELIGIBILITY_ELIGIBLE_CC0 = "ELIGIBLE_CC0"
ELIGIBILITY_NON_CC0_CACHED = "NON_CC0_CACHED"
ELIGIBILITY_UNKNOWN_CACHED = "UNKNOWN_CACHED"
ELIGIBILITY_STATUSES = (
    ELIGIBILITY_DISCOVERED,
    ELIGIBILITY_RIGHTS_CHECK,
    ELIGIBILITY_ELIGIBLE_CC0,
    ELIGIBILITY_NON_CC0_CACHED,
    ELIGIBILITY_UNKNOWN_CACHED,
)

# The negative cache. These two statuses are the only ones M1 rechecks.
CACHED_STATUSES = (ELIGIBILITY_NON_CC0_CACHED, ELIGIBILITY_UNKNOWN_CACHED)

ASSET_APPROVED = "APPROVED"
ASSET_PROCESSING = "PROCESSING"
# Bytes are in object storage. READY is the published status. Public search,
# detail, and related reads go through ready_assets, which also requires the
# latest rights_evidence row to be verified CC0. SUPPRESSED is written in the
# same transaction as a newer decision that is not verified CC0.
ASSET_STORED = "STORED"
ASSET_READY = "READY"
ASSET_SUPPRESSED = "SUPPRESSED"
ASSET_STATUSES = (
    ASSET_APPROVED,
    ASSET_PROCESSING,
    ASSET_STORED,
    ASSET_READY,
    ASSET_SUPPRESSED,
)
# An asset row means the download already happened. A later status must not
# send ingest back to the image host.
ASSET_HAS_BYTES = (ASSET_STORED, ASSET_READY, ASSET_SUPPRESSED)

JOB_KIND_INGEST = "ingest"
JOB_KIND_EMBED = "embed"
JOB_KIND_EXPORT = "export"
JOB_KINDS = (JOB_KIND_INGEST, JOB_KIND_EMBED, JOB_KIND_EXPORT)

# Board export lifecycle. The job row owns retries; the export row owns what the
# owner sees. READY means a complete archive is stored and was eligible when it
# was published. EXPIRED means the archive was retired by the retention window.
EXPORT_QUEUED = "QUEUED"
EXPORT_READY = "READY"
EXPORT_FAILED = "FAILED"
EXPORT_EXPIRED = "EXPIRED"
EXPORT_STATUSES = (EXPORT_QUEUED, EXPORT_READY, EXPORT_FAILED, EXPORT_EXPIRED)
EXPORTS_TABLE = "board_exports"
EXPORT_ITEMS_TABLE = "board_export_items"
EXPORT_PREFIX = "exports"

# The public projection. cc0_api selects this and has no privilege on
# source_records or rights_evidence. The view keeps a READY asset whose latest
# evidence row is verified CC0.
READY_ASSETS_VIEW = "ready_assets"
EMBEDDINGS_TABLE = "asset_embeddings"

# ViT-B image/text models in this family emit 512 dimensions. A wider model
# needs a new column; the checkpoint itself stays in configuration.
VECTOR_DIMENSIONS = 512

JOB_PENDING = "pending"
JOB_LEASED = "leased"
JOB_RETRY_WAIT = "retry_wait"
JOB_SUCCEEDED = "succeeded"
# The dead-letter destination. Postgres stays the only job store, so a failed
# job is a terminal state on its own row, inspectable with last_error.
JOB_DEAD = "dead"
JOB_STATES = (
    JOB_PENDING,
    JOB_LEASED,
    JOB_RETRY_WAIT,
    JOB_SUCCEEDED,
    JOB_DEAD,
)
