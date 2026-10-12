# ADR-0001: S3-Compatible Object Storage

Status: Accepted

## Context

The catalog will hold original images and thumbnails. Storing those bytes in
PostgreSQL would couple media size to database backups, replication, and query
memory. Local development must not require an AWS account, but production is
intended to run on S3, so the local store needs the same API rather than a
different one.

## Decision

Keep image bytes in an S3-compatible object store and keep only keys and
metadata in PostgreSQL. Run RustFS (`rustfs/rustfs:1.0.1`) locally and AWS S3 in
production. Address requests path-style in both environments, since RustFS does
not serve virtual-host-style bucket names. MinIO is not used; its community
edition was discontinued.

## Consequences

+ The database stays focused on metadata and stays small enough to back up
  cheaply.
+ The same boto3 client code works locally and on S3; only the endpoint,
  credentials, and addressing config change.
+ Readiness can prove the store is reachable with a single `HeadBucket`.
- Object storage and database transactions cannot be committed atomically.
- Ingestion will need to handle orphaned objects and partially committed
  uploads, probably through checksums and a reconciliation pass. Nothing writes
  objects yet, so this is unbuilt as of M0.
- RustFS behavior is not identical to S3. Differences in error codes or
  consistency surface only against real S3.

## Identities and lifecycle

Shared `RUSTFS_ACCESS_KEY` credentials are a local development shortcut and must
not ship to production. Production identities separate API reads, worker
uploads/reads (delete only where required), and bucket creation/configuration by
an init identity. Production identity wiring is not implemented in M0.

Readiness remains `HeadBucket`. M1 fixed the prefix layout as content-addressed
keys, `originals/ab/cd/<sha256>.<ext>` and `thumbnails/ab/cd/<sha256>.webp`;
see [ingestion](../systems/ingestion.md). The M1 worker writes and heads objects
and never deletes one. Versioning stays off unless a recovery need appears.
Takedown deletes the object (and every version if versioning is later enabled)
and retains a negative cache to prevent re-ingestion; takedown is future work.
