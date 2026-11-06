# Search v2 development notes

## Boundaries

Only `dev` and TEST ASYNC DOMOFOMKA are changed. Production remains on v1.5.6.
The original `codes.db` is a read-only compatibility source. No source rows are removed.
Raw VK histories, corpus labels, GAR files and generated databases stay under ignored `artifacts/`.
No model inference or paid address-cleaning requests are used for batch normalization.

## Data flow

1. `python -m tools.import_gar --regions all --workers 5` streams the already extracted GAR XML.
   Each process owns one region database. Only active/current address objects and houses are kept.
   Administrative parent links reconstruct the address; apartments, rooms, plots and ownership data are omitted.
   Cities of regional significance at level 2 are recognized by their settlement type; levels 5/6 retain nested cities/localities. District and urban-district names are not silently treated as cities.
   A completed region has a schema marker. A failed region can be rebuilt independently.
2. `python -m tools.build_catalog` builds a new catalog beside the current one.
   Buildings retain GAR GUIDs; codes are linked only for a unique matching locality/street/house.
   Source address aliases have a separate full-text lookup; every original source row ID is retained in `provenance`.
   Re-linking rebuilds street and alias indexes, so newly unmatched source identities cannot disappear from search.
   Case, Unicode and spelling-format duplicates can merge; unrelated cities never merge just because street and house match.
   Ambiguous/unmatched source addresses stay as explicitly unverified legacy identities.
3. `python -m tools.compact_catalog` produces the deployable catalog (about 787 MiB).
   It contains all known house keys, code-bearing building identities and source aliases.
   `python -m tools.build_catalog --refresh-keys-only` is an offline maintenance step for normalizer changes; rebuild the compact catalog and search index afterwards.
   The serving model separates street documents from houses and code rows.
   OpenSearch indexes one street/locality document with its exact house keys, rather than repeating full text for every code or house.
   `python -m tools.index_search` builds a staging index, checks the document count, then changes the search alias atomically.
   Prior indexes remain available for rollback; the script never drops an existing live index.

## Request scenarios

- `/codes/*` and `/api/v1/codes/*`: unchanged legacy search semantics.
- `/api/v2/codes/msg`: parse extra entrance/apartment details, city abbreviations, ordinal street names and house components.
  Optional OpenSearch selects street candidates in addition to the catalog index. Exact house/block/structure keys and a second deterministic check validate candidates.
  If OpenSearch is unavailable, indexed catalog search uses its street vocabulary with bounded typo correction.
- Recognized house with codes: send its address and entrance buttons. The code is still shown by editing the same bot message.
- Recognized house with no code rows: show its actual address and explain that the database has no codes for it.
  This does not assert that the physical building has no intercom/code.
- Multiple identities: ask the user to choose an address. The choice is saved with the message and cannot reference an unrelated candidate.
  Selecting one edits the same message to its entrance buttons; the next click edits it to the codes.
  Houses with more than ten entrances keep the additional button groups in separate messages, as in v1.
- No v2 identity or unavailable search service: use v1. A known no-code house and an ambiguous result are not overwritten by a potentially different v1 house.
- Coordinates: DaData still resolves a house; v2 then searches that address. Temporary geocoding failure asks for text and allows later messages to proceed.
- Repeated clicks, send/edit timeout, process restart: PostgreSQL inbox/outbox and stable send IDs preserve work and suppress duplicates.
  Clicks not received from VK are not available in message history and cannot be reconstructed.
- PC/lab unavailable: the German test bot continues using its own compact catalog. No tunnel to the development PC is needed.
  OpenSearch is an optional candidate backend; catalog candidates are always merged before deterministic validation.

## History audit and quality gates

`python -m tools.export_vk --env <private-env> --output artifacts/private/vk-prod`
reads all API-accessible private dialogs in a snapshot list. It does not mark messages read or send anything.
History batches are throttled; rate-limit/server errors back off. Completed dialogs are not fetched again on resume.
The exporter stores a restricted field set, not profile names or contact lists. Public reports contain aggregate counts only.

`python -m tools.audit_history` distinguishes observed empty answers from candidate correction pairs in the same dialog.
Same-house corrections within 30 minutes are hypotheses, not proof of the user's intended address.
Historical bot answers are also weak labels: legacy search can itself have selected a wrong city or interchanged a street number and a house number.
`python -m tools.evaluate_failures` replays every observed empty-answer query, counts code-bearing results/choices, and reports potential recoveries separately from verified corrections.

`python -m tools.evaluate_search --limit 4000` compares an exact legacy matcher snapshot with v2 on a deterministic sample.
Corpus partitions are by dialog, not by random message. Both partitions have now been inspected while fixing regressions, so this is a regression corpus, not an untouched final test set. Automatic matches, useful clarification choices and failures have separate counts.
A better hit rate is insufficient: wrong house/city/ordinal substitutions and loss of known answers must be inspected before release.
Real HTTP/SDK, PostgreSQL transactions and synthetic queue-to-VK checks are reported separately from mocked tests and browser clicks.

## Resource and deployment gates

`compose.search.yml` is an isolated local lab. OpenSearch binds only loopback, has a 3 GiB container cap and 1 GiB heap.
The German bot/API/queue have separate memory, swap, PID and log caps; Redis is stopped and no longer used by the current runtime.
The temporary PC reverse tunnel and relay have been stopped. The German API mounts the compact serving catalog read-only. No VPN or SSH password policy is changed.
A European deployment must fit the measured index and a compact serving catalog, include rollback disk space, and retain memory for PostgreSQL, the bot and VPN.
Do not deploy the complete XML or a 15+ GiB intermediary to the existing small VDS.

Nearest-house suggestions are intentionally gated on trustworthy coordinates. A similar house number or the same street name is not geographic proof.
The data model preserves a canonical building identity so coordinates and new code sources can be added later without changing old code provenance.

## References

- [GAR address registry](https://fias.nalog.ru/)
- [OpenSearch Docker configuration](https://docs.opensearch.org/latest/install-and-configure/install-opensearch/docker/)

## Measured data state, 2026-11-06

The audit covers 2,282 API-visible private dialogs and 198,060 exported messages, including 19,310 paired empty answers.
These are the available exported histories, not an atomic snapshot of future/new messages.

The catalog contains 34,851,597 active searchable GAR buildings. Of 1,008,682 original code rows,
792,465 map uniquely to GAR; 9,100 have ambiguous matches and 207,117 remain unmatched.
Unverified source addresses are retained, not guessed into another city's house.
There are 108,533 code-bearing identities and 1,002,183 distinct code records (6,499 exact duplicates merged).
See [catalog-summary.json](catalog-summary.json) and [history-audit-summary.json](history-audit-summary.json).

The actual legacy search implementation is unchanged. A malformed legacy row whose city column contains a street
is not promoted to an automatic v2 answer; compatibility fallback remains available until that row is reviewed.

## Regression and failure replay

All 46,611 unique corpus items were replayed. Among 43,969 observed-answer items that the current v1 still resolves,
30,262 retain that address automatically (including 1,508 v1 fallbacks), and 13,705 offer it as an explicit choice.
Two items disagree with v1: a city-name substring and a street/house-number reversal. These need intent review;
strict v2 semantics are not proof of what the user meant. There were zero automatically returned different addresses
in this baseline-positive subset. This is address-retention evidence, not a claim of 100% accuracy or equal UX.

On 19,310 historical empty answers, v2 finds code-bearing results for 2,676 requests and code-bearing choices for
1,656 more. Of these 4,332 potential recoveries, 3,587 are not found by the current v1 snapshot.
Another 5,382 requests resolve a known house without codes. Requests are not deduplicated in this replay.
Candidates and co-occurring input features do not establish the user's intended address or the cause of an old failure.

A real OpenSearch comparison used 4,000 corpus items. The union backend retains a reference result/choice for 3,897
items versus 3,895 for the compact catalog alone; it shifts some automatic results into clarification choices.
OpenSearch is ready as an optional backend, but is not installed on the existing 1 GiB German VDS.
Offline PC latency under concurrent batch load: catalog p50 11.814 ms / p95 189.152 ms;
OpenSearch union sample p50 35.580 ms / p95 296.386 ms. These are not VDS or end-to-end VK latency guarantees.

Linux/PostgreSQL integration validation: 137 tests pass; one obsolete legacy event-loop test is intentionally skipped
because the durable queue tests cover its replacement. [search-validation-summary.json](search-validation-summary.json)
contains aggregate results. Raw queries, dialogs and individual review cases remain private.
