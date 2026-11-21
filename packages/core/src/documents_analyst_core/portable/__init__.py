"""The portable zone — the only code in this repo that imports nothing of ours.

That constraint is the whole design. Because these files depend on nothing
internal (no `documents_analyst_core.*`, no SQLAlchemy, no FastAPI), they can be lifted
into another project, or tested in complete isolation, without dragging the
application along.

Rules for anything added to this folder:
  1. no imports from `documents_analyst_core.*`
  2. errors declared in the same file
  3. callbacks, not Protocols we would have to import
  4. config as arguments — never `os.environ`

See docs/REPO_STRUCTURE.md §2.
"""
