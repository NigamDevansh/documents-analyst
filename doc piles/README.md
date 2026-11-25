# Sample piles — drop a folder into the watched folder, get a final document out

Copy any folder here into `watched/` — or into your `WATCH_DIR`, if `.env` points
the system at a folder elsewhere — and it becomes a pile:

```bash
cp -r "doc piles/meridian-health" watched/          # default
cp -r "doc piles/meridian-health" "$WATCH_DIR"/     # if you set WATCH_DIR
```

**Every subdirectory of the watched folder is a pile.** There is no create step,
no config and no API call: the scanner lists the directory, and any folder it
finds becomes a pile on the next pass. Files sitting at the top level are never
ingested — only directories are. Each pile gets its own run, review queue,
history and `final-document.docx`, and piles are never compared with each other.

Copy in both samples and they are processed side by side, one thread each (up to
`pile_concurrency`, default 3); inside each pile the files are still read and
extracted one at a time, in order.

Start the stack with `make up`, then open http://localhost:8000.

## What is here

| Folder | What it demonstrates |
|---|---|
| `meridian-health/` | The full workflow. Two agreements in one pile, a conflict to resolve, and an amendment whose approval **creates** a compliance violation. |
| `acme/` | The same documents `make up` seeds into `watched/acme` from `fixtures/conflict_pile` (only when `WATCH_DIR` is the default `./watched`): a format alias, a corrupt PDF that is quarantined, conflicts and amendments. **Retry** does not re-queue the corrupt PDF — it only re-queues files whose extraction failed (quarantined after 3 attempts), since an unparseable file's bytes have not changed. |

## `meridian-health` — what to look for

Two different party sets, so the pile holds **two agreements**:

- **Meridian ↔ CareBridge Analytics** (`01`–`04`) complies with the rulebook as
  signed. `03_dpa` adds data-protection terms; `04_amendment_no1` raises the
  monthly fee from 40,000 to 55,000 without touching the 480,000 liability cap.
- **Meridian ↔ Northstar Imaging** (`05`–`06`) is the messy one: Net 60 terms,
  no liability cap at all, and a one-sided termination clause.

Expect on a first run:

1. **A conflict.** The CareBridge MSA says Net 30, its SOW says Net 45. Both are
   inside policy, so this surfaces as a question for you, not a finding.
2. **Findings on Northstar.** `R001` (no liability cap stated) and `R002`
   (Net 60 is outside policy) both fire — verified. The judge rules `R010`
   (termination is not mutual) and `R011` (advance notice of subprocessors) are
   model-driven and may or may not fire on any given run.
3. **A violation you create yourself.** Approve the amendment and the audit
   re-runs: the cap of 480,000 no longer covers twelve months at 55,000
   (660,000), so `R003` fails on the second pass having passed on the first.
4. **Open subjects matched by meaning.** The MSA calls it "Data residency
   region", the SOW calls it "Data storage location". Neither is in the
   28-field taxonomy, so they are matched by embedding rather than by key.

Roughly twenty items reach the queue. Most are ordinary multi-document
disagreement — three documents in one agreement carry three different effective
dates — and that is what the queue is for.

## Adding your own pile

```
mkdir watched/my-vendor                 # or "$WATCH_DIR"/my-vendor
cp ~/contracts/*.pdf watched/my-vendor/
```

`.pdf`, `.docx`, `.txt`, `.md`/`.markdown` and `.html`/`.htm` are read. The
next scan picks the folder up — no restart needed.

To add files to a pile that already exists you can also use the dashboard's
upload button (`POST /piles/{pile_id}/documents`). It writes the file into that
pile's folder and wakes the worker; discovery then ingests it like any other
file. Unsupported formats are refused (415), and so is a file named
`final-document.docx` (409).

Add a `rulebook.yaml` to the folder to get compliance findings; without one the
audit stage runs and simply reports nothing. The name must be exactly
`rulebook.yaml` — `rulebook.yml`/`.json`/`.txt` are skipped with a log line, not
applied. Copy one of the rulebooks here as a
starting point. Rules are re-read on the next scan, so editing one takes effect
without a restart.

**Your files are never modified.** The scanner only reads: it hashes each file,
parses it, and records what it found. The one file the system writes is the
generated `final-document.docx` at the root of each pile folder — rewritten after
each review, skipped by the scanner, and never read back in as a source.
