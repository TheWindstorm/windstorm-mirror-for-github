# DNA Strand Master Plan: Windstorm Mirror for GitHub

Plan version 0.0 (2026-11-22). Builds nothing yet: the plan is written, and reviewed, before any code.

This is the plan the skill will be built from. Each gene is one thing the skill does. Each codon is one claim small
enough to test, with its proof next to it. A proof is a test in `tests/test_mirror.py`, named after its codon.
A codon with no test says so, under "Not verified".

The genes and codons arrive in the next change. Until then this file holds only the rules the tests enforce.

## Promises

`PROMISES.json` holds what real machines, and the people using them, showed must change: what was seen, the
promise, its codon, and the version it is due in. Five tests keep it honest. A version cannot be released while a
promise due in it has no test. The version after that cannot be released until the row says on which real machine
the promise was seen to work. A date moves only with a written reason, and a promise that came from what the
person saw on their own machine moves only by their word. Rows are never deleted. A lesson that was heard and not
taken is written under `declined` with the reason.

The link between this plan and the tests is itself tested: every proof named here is a real test, and every codon
test has a codon here.
