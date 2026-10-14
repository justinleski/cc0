# ADR-0002: PostgreSQL with pgvector as the Single Store

Status: Accepted

## Context

The product needs relational data (assets, creators, rights evidence, boards),
lexical search, vector similarity search, and durable background job state. Each
of those has a specialist system, but the target is roughly ten users and a
pilot catalog of a few hundred images on one small instance.

## Decision

Use one PostgreSQL database with the `pgvector` extension for all of it. Run
`pgvector/pgvector:0.8.7-pg16` locally. Do not add OpenSearch, Redis, a graph
database, or a separate queue in the MVP; PostgreSQL holds job state, and a
queue, if ever added, is only a wake-up mechanism. Enable `vector` in M0 even
though no embedding column exists yet, so the extension is never the thing that
blocks the first embedding migration.

## Consequences

+ One store to run, back up, and restore, and one connection story for the API
  and the worker.
+ Joins across metadata filters and vector similarity happen in a single query
  rather than across services.
+ No fixed cost for infrastructure the current scale does not need.
- Exact vector search will not scale indefinitely; an approximate index or a
  dedicated vector store becomes necessary at a measured threshold.
- The database is a single point of failure and a single resource pool, so a
  heavy ingestion batch can compete with user queries.
- M2 stores image vectors in `asset_embeddings` and ranks them with an exact
  cosine scan. An approximate index waits until that scan is measured slow.
