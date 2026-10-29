# Working in this repo

Several agents fix bugs in parallel, each on its own branch. Nearly all past merge conflicts
came from two branches editing the same lines of the same file. These rules keep the edits apart.

## Tests

- Put the tests for a fix in a new file, `tests/test_<topic>.py`. Name it after the specific
  behaviour (`test_varint_bounds.py`), not the module (`test_parser.py`), so two agents do not
  pick the same name. Run `ls tests` after `git fetch origin` to check.
- Add to an existing test file only when your change alters behaviour that file already tests.
- Keep helpers in the file that uses them. Do not import from another test module.
- Import only what the file uses. Do not reorder or tidy imports in other files.

## Ledgers

Do not edit `BUG_TRACKER.md`, `optimizations.md`, `improvements.md`, or `features.md` in a fix
PR. The coordinator thread updates them on `master` (see "Working agreements" in
`BUG_TRACKER.md`).

## Source files

- Before you start, run `git fetch origin` and `gh pr list`. If an open PR changes the same
  function, tell the user instead of starting.
- Bugs in the same source file go to one agent, or run one after another.
- Fix one bug per PR. Do not reformat, rename, or move code you are not fixing.
- Before you push, merge `origin/master` into your branch and run
  `python3 -m unittest discover -s tests`.
