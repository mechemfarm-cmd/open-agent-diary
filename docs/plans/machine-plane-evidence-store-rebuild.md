# Machine plane — reversible evidence-store rebuild

Status: **proposal, read-only.** Nothing in this document has been executed. No record
has been moved, relabelled or deleted.

## 1. Why this is needed

The evidence store accumulated records across a period when the recorder's segment
allocator could reuse a path. When a path was reused, publication overwrote the
artifact while the store kept every record imported from the larger original. The
allocator is now fixed (`23e2060`), but the damage to the existing corpus is not
repaired by that fix.

The corrected integrity manifest establishes the position:

| Classification | Records |
|---|---:|
| supported by artifact | 2,522 |
| legacy, uniquely keyed | 63,979 |
| artifact mismatch | 118,813 |
| no artifact | 340,369 |
| **legacy / unverifiable** | **459,182** |
| duplicate keys / collisions | 0 |

525,683 records in total. **87% cannot be tied to a surviving artifact.**

The store is not corrupt — there are no duplicate keys, no duplicate event IDs and no
key collisions. It is *unverifiable*: for most records, nothing survives that can
confirm what produced them.

That distinction decides the whole design. A corrupt store would have to be rebuilt
from artifacts, which is impossible for the 340,369 records whose artifacts retention
removed. A merely unverifiable store can be **partitioned**: the records that can be
supported stay as evidence, and the rest are preserved but never presented as
evidence.

## 2. Inputs, and why they are immutable

Two inputs, both hashed before use. The run refuses if either does not match.

| Input | Path | SHA-256 |
|---|---|---|
| Store snapshot | `/var/lib/machine-plane/quarantine/evidence-20261118T202428Z.ndjson` | `2b3375a06b789c85da62fc71c8972fd1be2a182b9e52e900af4431b3bc74c3ab` |
| Corrected manifest | `…/quarantine/manifests/integrity-corrected-20261124T111059Z.json` | `70ebbf8442b178363ff58f77fbf19d68c77b585765e8f4ff3d5000257b75739d` |

The manifest is the sole authority on where each segment's records go. The rebuild does
not re-derive classification, because re-deriving it would make the result depend on
the state of the spool at rebuild time rather than on the reviewed analysis. A segment
whose artifact disappears between manifest and rebuild must cause a refusal, not a
silent reclassification.

## 3. Destinations

Three stores, written separately. None is produced by filtering another; each is
written directly from the source snapshot.

| Store | Contents | Records |
|---|---|---:|
| `store/evidence/events.ndjson` | `supported-by-artifact` only | 2,522 |
| `store/control/events.ndjson` | control-mode records | 0 |
| `store/legacy/events.ndjson` | `legacy-unique-key`, `artifact-mismatch`, `no-artifact` | 523,161 |

**Nothing is promoted.** A record is evidence only if its segment artifact exists, every
one of its byte offsets lands on a line start in that artifact, and the segment records
an explicit capture mode. Looking filtered is not provenance.

The legacy store is not a bin. It is the honest home for records that are real
observations but cannot be attributed. Keeping them preserves the possibility that a
future artifact or a future technique makes them verifiable; presenting them as
evidence would not.

## 4. Procedure

Read-only until step 6.

1. **Verify inputs.** Hash the snapshot and the manifest; refuse on mismatch.
2. **Load the manifest** and build a segment → destination map.
3. **Stream the snapshot once.** For each record, parse its segment from the record
   key and look up the destination. A record whose segment is absent from the manifest
   is a refusal, not a default.
4. **Write three staging files** under a staging directory. Nothing is written into the
   live store path.
5. **Verify** (§5). Any failure discards staging and leaves the live store untouched.
6. **Atomic swap, only after review and explicit operator approval.** Move the current
   store aside under `store/superseded-<timestamp>/`, then move staging into place.
   Both steps are renames within the same filesystem, so the swap is atomic per store
   and reversible by renaming back.

## 5. Verification, before any swap

Each check must pass, and each is reported whether it passes or fails.

- **Counts.** Per-destination counts equal the manifest's, and the three sum to the
  source total: 2,522 + 0 + 523,161 = 525,683.
- **Exactly once.** Every `record_key` in the source appears exactly once across the
  three staging stores, and no `record_key` appears twice within a store. Compared as
  sets, not as counts.
- **No promotion.** No record classified `artifact-mismatch` or `no-artifact` appears in
  the evidence or control store. Checked by joining staged evidence records back to the
  manifest, not by trusting the writer.
- **Provenance re-check.** For every segment contributing to the evidence store, the
  artifact still exists and its hash matches the manifest's.
- **Content preservation.** The concatenation of the three staging stores preserves
  every source record's payload byte-for-byte; only the destination differs.
- **Refusal behaviour.** After the swap, confirm the importer still refuses: a legacy
  segment as evidence; a control segment in the evidence path; a filtered segment in the
  control store; and a mode-less segment anywhere but `--legacy`.

## 6. Rollback

The original store is never modified in place. It is moved aside, not deleted, and the
snapshot remains under `quarantine/`. Rollback is: stop writers, rename
`store/superseded-<timestamp>/` back over the new path. The snapshot and both manifests
remain the fallback if the swap itself is interrupted.

## 7. Explicitly out of scope

- Chain and checkpoints. Alice has frozen this until the rebuild is done.
- Deleting or pruning anything.
- Re-deriving provenance for records whose artifacts are gone. It cannot be done, and a
  guess is worse than a gap.
- Any change to the recorder. The allocator and recovery paths are closed at `23e2060`.
- Importing anything new while the rebuild is staged.

## 8. What this does not fix

The rebuilt evidence store will contain **2,522 records**. That is the honest size of
the verifiable corpus, and it is 0.5% of what the store currently holds. Everything else
moves to legacy, not because it is wrong but because nothing survives to confirm it.

That is a large drop and it should be stated plainly before anyone approves the swap.
The alternative — keeping 525,683 records in a store where 87% cannot be checked — is a
worse position, because a reader cannot tell the two apart.

## 9. Decisions requested

1. Confirm the destination mapping in §3, including that `legacy-unique-key` (63,979
   records with matching artifacts but no recorded mode) belongs in legacy rather than
   evidence.
2. Confirm that the swap in step 6 is a separate, explicitly approved action, and that
   this document authorises nothing on its own.
3. Confirm the verification list in §5 is sufficient, or name what is missing.
