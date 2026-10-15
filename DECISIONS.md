# Design Decisions
Every schema, query, and architecture decision in plain language: what, why, and the alternative rejected.

- Docker Compose runs the official `falkordb/falkordb` image, publishes the database and Browser ports (6379 and 3000), and mounts a named volume at `/var/lib/falkordb/data` as documented by FalkorDB. This keeps development data persistent; a host bind mount was not chosen because the project needs portable local persistence.