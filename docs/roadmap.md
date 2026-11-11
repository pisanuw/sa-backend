# Roadmap (backend)

The plan for both repositories is in the frontend repository, `docs/roadmap.md`. It covers braille
parity for deafblind and blind students, math, more STEM image types, a campus workflow (courses,
collections, a request queue, review state), self-hosting, and evaluation with students. This file
lists only what falls to the backend.

## In review

- The worker imports the image pipeline only for flowchart uploads, so chart and summary jobs and the
  tests run without torch, YOLO and Google Vision.
- CI runs the unit tests on every push and pull request.

## Next

- **Per-account upload quota** (README, known gaps), before any wider rollout.
- **Contracted braille**: `POST /braille` that returns contracted (grade 2) UEB as a .brf, using
  liblouis (`apt install liblouis-bin python3-louis` in the Dockerfile). The frontend already makes
  uncontracted braille in the browser; this adds the grade most adult readers use. Liblouis is
  LGPL-2.1, used as a separate library.
- **Campus workflow** tables: `organizations`, `memberships`, `courses`, `collections`,
  `collection_items`, `requests`. Each needs RLS enabled in its migration, like every backend table.
  Needs decisions first (see the frontend roadmap, "Decisions needed").
- **Self-hosting**: a `docker compose` file with Postgres and the backend in no-AI mode, so another
  campus can run the whole stack without paid services.
- **OpenAPI** description of the routes.
- Rename `/flowcharts` and `/research` routes (upstream issue stem-accessibility/backend#1) and
  review unused study code (stem-accessibility/backend#2).
