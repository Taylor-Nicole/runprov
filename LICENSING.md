# Licence and copyright holder — the decision, and what it rests on

Recorded rather than settled in conversation, because it is the one blocker to publication
and because "we discussed it and I think we said MIT" is not a record.

**This is not legal advice.** The French statutory position below is well established and
easy to verify, but the one question that actually matters — *what is your employment
status* — can only be answered by your institution.

## DECIDED, 2026-08-07

**CeCILL-B**, copyright **Hôpital Henri-Mondor and Taylor Thompson**. `LICENSE` holds the text
verbatim (21,393 bytes, sha256 `ae13622d13fd432d…`); every module carries a three-line
header. Taylor chose the French licence over MIT; the analysis below is kept because it is
the record of why, not because the question is still open.

CeCILL-B is the right member of the family for this: **permissive, BSD-equivalent**, so it
does not propagate obligations into pipelines that import it — which matters for a module
every step depends on. Its one real obligation is **Article 5.3.4 CREDITS**: anyone
distributing a modified version must state that it is based on this software and reproduce
the intellectual-property notice. For an academic author that is a feature, not a cost.

It is also written under French law, which removes the standing question of whether an
Anglo-American warranty disclaimer survives a French court — a sensible fit for a public CHU.
The price is recognition: outside France, `CECILL-B` is a less familiar line in a dependency
audit than `MIT`. PyPI carries the trove classifier, so it is at least machine-readable.

**Both holders are named, 2026-08-10, on Taylor's instruction.** That is the right shape
for the situation CPI art. L113-9 creates: the institution holds the economic rights in
software written by an employee in the exercise of their duties, and Taylor holds the
inalienable *droit moral* as sole author. Naming only one of them would misstate the
position in one direction or the other.

**Still worth confirming with the DRCI: the legal entity name.** `Hôpital Henri-Mondor` is a
site — part of **AP-HP** (Assistance Publique – Hôpitaux de Paris), in Créteil. AP-HP is very
likely the entity that actually holds rights; the hospital name identifies where the work was
done rather than who owns it. Compare a paper's affiliation line, which names the site, with
a copyright line, which names the holder. If the DRCI says AP-HP, the line becomes
`Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris and Taylor Thompson`. One commit
today; impossible after the first release.

### Superseded recommendation, kept for the record

**MIT, with the CHU as copyright holder.** Add a DCO from day one, and revisit Apache-2.0
only if a company asks for it.

## Why the holder is probably not you

French law treats software as a deliberate exception to the rule that authors keep their
rights. **Code de la propriété intellectuelle, art. L113-9**: software created by an
employee in the exercise of their duties, or on the employer's instructions, has its
*economic rights* (droits patrimoniaux) vested in the employer **automatically** — no
assignment, no signature, no formality. For public-sector agents the equivalent effect
follows from the provisions on works created by agents publics in the course of their
duties.

So the practical question is not "did I write it" — you did — but **"was I an employee or a
public agent when I wrote it?"** That depends on your doctoral arrangement:

| your situation | who most likely holds the economic rights |
|---|---|
| contrat doctoral, or any employment/agent contract with the CHU | **the CHU** |
| CIFRE | the company, per the CIFRE agreement — read it |
| no employment contract (self-funded, foreign scholarship) | **you**, most likely — L113-9 needs an employment relationship |

You keep the *droit moral* — attribution — in every case. That is not waivable in French
law, which is why an AUTHORS file is worth keeping regardless of who the holder is.

Given you describe a French public CHU and an LBM/bioinfo team, the CHU is the likely
answer. Do not assume it: **ask the DRCI** (Direction de la Recherche Clinique et de
l'Innovation) — that is the office at a CHU that handles this, and it is a routine question
for them, not an imposition.

### The exact questions to ask

1. Under my current contract, who holds the economic rights to software I write in the
   course of my doctoral work?
2. Does the institution have a policy or preferred licence for releasing research software
   open source? (Many French public bodies name **CeCILL** — see below.)
3. Is there an internal declaration step — a software declaration, an **APP** deposit
   (Agence pour la Protection des Programmes), or a valorisation/SATT review — before a
   public release?
4. What is the exact legal entity name and year to put in the copyright line?

Question 3 is the one that catches people. A public release that skipped an internal
declaration is awkward to unwind, and unwinding is impossible once others depend on it.

## Why MIT, given what you actually want

Your stated goal is that **internal colleagues can use it**, in a team that operates like
industry.

Note first that if the CHU holds the rights, **internal use is not a licensing question at
all.** Colleagues inside the same legal entity using software that entity owns need no
licence from anyone. The licence governs *external* distribution. So your goal is met
either way, and nothing stops the team using it today from the repository.

What publication adds is different and still worth having: `pip install runprov` instead of
copying files, a version number colleagues can pin, and — for the thesis — a reviewer who
can install it and check your claims instead of taking a Methods paragraph on trust.

For that, MIT is the right default:

* **Shortest and best understood.** Another lab's or a partner company's legal review is a
  formality rather than a meeting.
* **Compatible in every direction that matters.** MIT code can be used inside Apache-2.0,
  GPL, and proprietary projects. Nothing downstream is foreclosed.
* **No obligations your colleagues can accidentally breach.** Apache-2.0 imposes NOTICE and
  change-notice requirements on redistribution. Harmless in principle; one more thing an
  industry-facing team has to remember in practice.

**When Apache-2.0 would be the better call:** if a company plans to embed it in a product
and their counsel asks for an express patent grant. That is Apache-2.0's real advantage
over MIT, and for a ~700-line dependency-free provenance utility with nothing patentable in
it, the grant is close to symbolic. Wait until someone asks.

**CeCILL-B deserves a mention** because you are a French public institution. CeCILL was
written by CEA, CNRS and Inria precisely because Anglo-American licences sit awkwardly under
French law — warranty disclaimers and choice-of-law clauses that may not hold up in a French
court. **CeCILL-B is the BSD-equivalent** of the family (CeCILL-C is LGPL-like, CeCILL is
GPL-like). If your DRCI or SATT has a policy, it is likely to name one of these. It is a
perfectly good answer; it is simply less recognised outside France, which costs you adoption.
If they ask for it, take it — do not argue.

## Can you move from MIT to Apache-2.0 later?

**Yes for future versions, and no for past ones.** Both halves matter.

* **Future versions: unrestricted, while the rights holder is a single party.** The holder
  can release 0.2.0 under any licence they choose. MIT does not bind the author.
* **Released versions stay MIT permanently.** Anyone who obtained 0.1.0 under MIT keeps
  those rights forever. There is no recall. So a relicence is a fork in the road going
  forward, never a retraction.
* **The window closes when external contributors arrive.** Once someone else's patch is
  merged, they hold copyright in their contribution and you cannot relicence without their
  agreement. Projects that skipped this have had to hunt down contributors years later, or
  rewrite the code they could not reach.

**Done, 2026-08-07, before the first outside pull request.** [CONTRIBUTING.md](CONTRIBUTING.md)
requires a `Signed-off-by:` line, [DCO.txt](DCO.txt) is the certificate verbatim, and the
generated `.github/workflows/dco.yml` **refuses a pull request containing an unsigned
commit** — merge commits excepted, since GitHub generates those. Asking for a sign-off in a
contributing guide is documentation; the CI job is the rule, and this option dies quietly
without one. A full CLA is heavier and unnecessary at this scale.

Adding the patent grant later is therefore cheap. Starting restrictive and loosening is the
direction that is *not* cheap, which is another argument for MIT first.

## What this repository is missing anyway

There is no LICENSE, no CITATION.cff, and no author affiliation recorded anywhere — a gap
the earlier review already flagged as a reviewer's first question: *"How do I cite this
software, and who are the authors with their ORCIDs and affiliations?"* The same DRCI
conversation answers all of it at once. Worth doing in one pass.

## Decision

| | |
|---|---|
| Licence | **CeCILL-B** — chosen 2026-08-07, verbatim in `LICENSE` |
| Holder | **Hôpital Henri-Mondor and Taylor Thompson** — institution per L113-9, author per droit moral. Confirm with the DRCI whether the entity is AP-HP rather than the site |
| Contributions | **DCO** (`Signed-off-by`) from the first commit |
| Apache-2.0 | not now; revisit only if a company's counsel asks for the patent grant |
| Status | **Publishable.** The extraction script's LICENSE blocker is cleared. |
