# data/synthetic

This is the tree `docker-compose.yml` mounts to `/app/data` and that
`ingestion-service` reads from (`/app/data/synthetic`).

Regenerate with:

    python scripts/generate_data.py        # writes here, from any working directory

`generate_data.py` resolves its output relative to its own location. It used to
use the relative path `data/synthetic`, so running it from `scripts/` silently
wrote the corpus to `scripts/data/synthetic/` — a tree nothing reads. That is why
the index held a handful of documents instead of 250.

## seed/

Each `<kind>/seed/` folder holds the small hand-written demo documents that were
here before the generated corpus was restored. The parsers glob `*.json`
non-recursively, so files under `seed/` are kept on disk but not ingested. They
share ID prefixes (EMAIL-001, MTG-001, ...) with the generated corpus, so
ingesting both at once would collide in Pinecone and Neo4j.

To use the curated seed instead of the generated corpus, swap the files back.
