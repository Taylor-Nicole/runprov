# Security policy

## Reporting

Email **taylor.nicole.thompson@gmail.com** with `runprov security` in the subject. That
address is already in the package metadata, so it is not a secret and does not need to be.

Please include the version, the Python version, and the smallest input that reproduces it.
Expect a first reply within **two weeks** — this package is maintained by one person
alongside other work, and a slow honest answer is better than a fast promise. If a report is
valid I will say what the fix is and when; if it is out of scope I will say that instead.

Do not open a public issue for something exploitable until there is a fix, or until we have
agreed there is nothing to fix.

## Supported versions

`0.x`, latest release only. There is no back-porting: this package has no dependencies, so
upgrading is a version-number change and nothing else. When `1.0` exists this section will
say something more useful.

## What the attack surface actually is

Stated plainly, because "it is only provenance" is not an argument:

* **No network I/O.** Nothing in the package opens a socket, and there are no dependencies
  that could.
* **No deserialisation of untrusted data.** Records are read with `json.loads`, never
  `pickle` and never `yaml.load`. The YAML the CLI emits is *generated*; nothing parses YAML.
* **It reads files you point it at, and hashes them.** A registered input is opened and
  streamed. It refuses a FIFO, socket or device rather than blocking on one, and it does not
  follow a directory symlink when walking a tree.
* **It writes files you point it at** — a sidecar, an append-only history, an environment
  snapshot, and optionally a copy of a lock file.
* **It runs `git`**, as a fixed argument vector with a 20-second timeout, in the project
  root. It never passes user text to a shell.
* **It can take over file descriptors 1 and 2** when terminal capture is enabled, and
  restores them. It tees; it never diverts.

## Things that are deliberate, and are not vulnerabilities

* **Records contain absolute paths, hostnames and usernames-by-implication.** A provenance
  record describes a machine. Treat one as you would treat a log file: do not publish it
  without reading it. The environment snapshot deliberately excludes prefix paths, and
  manager detection records that a variable was *set* rather than its value — but `cwd`,
  `script_file` and every registered path are recorded in full, because that is the record's
  job.
* **The embedded pin is not a signature.** It states what a run read and wrote. It proves
  nothing about *who* ran it and is not tamper-evident against someone who can edit the
  artifact. **Every field the pin interpolates is escaped** — the script name, the generation,
  the commit and the input filenames — so that text carrying a newline cannot forge an entry
  in it. That is a correctness property, not an authentication one: it stops a job description
  in `RUNPROV_GENERATION` from producing a pin that lists an input nobody read, which is how
  this actually happens. It does not stop anyone who can edit the artifact.

  Until 2026-08-19 the escaping covered the filenames ALONE, and this sentence said so in a
  way that read as a general claim. It was not one: `RUNPROV_GENERATION` containing a newline
  forged a pin entry, and `runprov verify` then reported `GONE` and exited 1 forever over a
  file that never existed.
* **`content_digest` ignores some differences on purpose** — line endings, archive member
  timestamps, volatile build stamps. `sha256` is recorded beside it and preserves the exact
  bytes. Anything checking file integrity should use `sha256`.

## Out of scope

Provenance records you have chosen to publish; the contents of files you asked it to hash;
and anything requiring an attacker who can already write to your project directory, since at
that point they can edit the scripts.
