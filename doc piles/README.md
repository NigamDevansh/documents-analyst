# Sample piles — drop a folder into `watched/`, get a final document out

Copy any folder here into `watched/` and it becomes a pile:

```bash
cp -r "doc piles/meridian-health" watched/
```

**Every subdirectory of `watched/` is a pile.** There is no create step, no config
and no API call: the scanner lists the directory, and any folder it finds becomes
a pile on the next pass. Files sitting at the top level are never ingested — only
directories are.

Start the stack with `make up`, then open http://localhost:8000.

## What is here

| Folder | What it demonstrates |
|---|---|
| `meridian-health/` | The full workflow. Two agreements in one pile, a conflict to resolve, and an amendment whose approval **creates** a compliance violation. |
| `acme/` | The same documents `make up` seeds into `watched/acme` from `fixtures/conflict_pile`: a format alias, a corrupt PDF that is quarantined, conflicts and amendments. |

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
   (Net 60 is outside policy) both fire — verified. The judge rule `R010`
   (termination is not mutual) is model-driven and may or may not fire on any
   given run.
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
mkdir watched/my-vendor
cp ~/contracts/*.pdf watched/my-vendor/
```

`.pdf`, `.docx`, `.txt`, `.md` and `.html` are read. The next scan picks the
folder up — no restart needed.

Add a `rulebook.yaml` to the folder to get compliance findings; without one the
audit stage runs and simply reports nothing. Copy one of the rulebooks here as a
starting point. Rules are re-read on the next scan, so editing one takes effect
without a restart.

**Your files are never modified.** The scanner only reads: it hashes each file,
parses it, and records what it found. The one file the system writes is the
generated `final-document.docx` at the root of each pile folder — rewritten after
each review, skipped by the scanner, and never read back in as a source.
