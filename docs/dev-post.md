---
title: My 1B model renamed one file to another file's name — the fix was a provenance guard
tags: hf26challenge, ai, opensource, python
---

My Downloads folder had **599 loose files in it** — 101 PDFs, 79 spreadsheets, 129 images, and a folder full of things called `img.png`, `fp.jpeg` and `1762857107887_floop-plan-4.png`. I had been closing that tab for months.

So for this week's Hacktoberfest challenge ("build something with open-source AI that rewards laziness") I wrote **Tidy Sloth**: a local file tidier that sorts a mess into folders and gives files names a human can actually read. Python standard library, Ollama, no cloud, no API key.

It works. But the interesting part isn't the sorting. It's that a 1-billion-parameter model confidently renamed one file to *a completely different file's name*, and the fix was not a better prompt.

## The sloth is the architecture, not the mascot

Sloths are slow because speed costs energy. A file tidier has the same economics: most files are obvious, and asking a language model about them is wasted work.

So the pipeline spends tokens like they're calories:

```
file ──▶ rules ──▶ confident? ──yes──▶ done, 0 tokens
                       │
                       no
                       ▼
              local model (batched) ──▶ category + name
                       │
                       ▼
             Python validates, then moves
```

1. **Extension rules** — `png → Images`, `xlsx → Data`, `zip → Archives`.
2. **Purpose rules beat extension** — `Invoice-March.pdf` goes to `Invoices`, not `Documents`. `Resume-Sanketh.pdf` goes to `Resumes`.
3. **A meaningful filename is enough** — `carprices.csv` doesn't need a model to tell you it's data.

Only the leftovers go to `llama3.2:1b`: the timestamps, the `img.png`s, the `AC3.json`s.

On my real folder:

| | |
|---|---|
| loose files | 599 |
| resolved by rules | **453 (75.6%)** |
| needed the model | 146 |
| model calls | 13 (batches of 12) |
| wall time | 14.2 min |
| estimated manual sorting | ~80 min |

Three quarters of the folder never touched the model. That's the whole trick.

## Then the model lied

Small models are not editors. I knew that. What I didn't expect was *how* it failed.

I batch a dozen filenames per prompt and ask for a category and a cleaner name for each. The model returned exactly twelve answers, in order, in valid JSON. The count matched. The JSON was clean. And it had silently shifted the associations:

```
1786076876258_SoulTree-Brochure-1 (1).pdf  →  AC2.pdf
1786076876258_SoulTree-Brochure-1.pdf      →  Cost-Breakdown-6a61d9864a29092bf3f7159d-1.pdf
1748950125346_out-0.webp                   →  Aadhar-Card.webp
1764919659917_floop-plan-4.png             →  Copy-of-2024-07-TBC-Banner-Generic-2560x650-1-H1nZTAL.png
```

Every one of those names belongs to a *different file in the same batch*. The model didn't hallucinate a filename; it recycled a real one. Which is worse, because it looks plausible enough to ship.

My first fix was to stop trusting the model's self-reported index and match answers positionally. That didn't help — the count was already right, so there was nothing to catch. I also stripped echoed extensions, which fixed `thing.pdf.pdf` but not this.

## The fix: a rename may only reuse the file's own words

The rule I settled on is boring and it works. **A proposed name is accepted only if it is built from words that were already in the original filename.**

```python
def provenance_ok(proposed, original):
    """A rename may reorganise a file's own words. Never import another's."""
    if _tokens(proposed) & _tokens(original):
        return True
    a, b = _compact(proposed), _compact(original)
    return bool(a and b and (a in b or b in a))
```

`SoulTree-Brochure-1.pdf` → `AC2.pdf` shares zero tokens with the source, so it's rejected and the file keeps its own name. `SoulaceNew2.png` → `soulace-new-2` shares everything, so it's accepted.

The model may **reorganise** a filename. It may never **import** one.

I put that and the other invariants into 15 unit tests, including the exact five bad pairs the model produced:

```
$ python3 -m unittest test_tidysloth
Ran 15 tests in 0.002s
OK
```

After the guard, the same 599-file run produced **zero cross-row names** and zero doubled extensions.

## Two more rules that saved me

**A name with no meaning is never given invented meaning.** Given `01.csv`, the model happily suggested `project-proposal.csv`. That's not tidying, that's fiction — and it's unfalsifiable, so you'd never notice it was wrong. Now `01.csv` stays `01.csv`. Only the category is decided by the model.

**Deterministic noise removal beats model creativity.** `1762857107887_floop-plan-4.png` became `floop-plan-4.png` by stripping a leading 10+ digit message ID in Python — no tokens, no guessing, and it works whether or not Ollama is running. (With one edge case: `4446563839.pdf` is *all* digits, so stripping the "ID" would leave nothing. The strip only applies if a real name survives.)

## Sorting is destructive, so the defaults are cowards

- `plan` changes **nothing**. It writes `plan.json` and an HTML report you read in the browser first.
- `apply` moves files and writes a timestamped manifest.
- `undo` replays that manifest in reverse and removes the empty folders it created.
- **There is no delete code path.** Collisions get renamed, never overwritten.

I tested apply and undo on a 40-file copy: 40 files into 10 folders, then all 40 restored and the folders cleaned up. I have deliberately **not** run `apply` on my real Downloads folder yet — the plan is sitting there for review, which is exactly the workflow I want from a tool that touches my files.

![Tidy Sloth's generated plan report](https://raw.githubusercontent.com/sank8-2/tidy-sloth/master/docs/plan-report.png)

## What I'd tell someone reaching for a small model

- **Match by position, not by index.** A model-supplied `i` field is another generated token, not a fact.
- **A correct count proves nothing.** All twelve rows were there and correctly shaped; the mapping between rows and files was still corrupted.
- **Validate provenance, not just format.** Schema validation would have passed every one of those bad renames.
- **Give the model the smallest job that still helps.** Here, choosing between 15 folders. Names are mostly mechanical.

The 14 minutes is real, and it's slow — that's a 1B model on a laptop, and most of it is the model, not the rules. A bigger model would name better and cost more energy, which rather misses the point of a sloth.

Code: **[github.com/sank8-2/tidy-sloth](https://github.com/sank8-2/tidy-sloth)** — MIT, one file, no dependencies.

```bash
ollama pull llama3.2:1b
python3 tidysloth.py scan  ~/Downloads   # read-only summary
python3 tidysloth.py plan  ~/Downloads   # proposals + report
python3 tidysloth.py apply ~/Downloads/.tidysloth/plan.json
python3 tidysloth.py undo  ~/Downloads/.tidysloth/manifest-*.json
```

Or with no model at all, which still produces a complete and useful plan:

```bash
python3 tidysloth.py plan ~/Downloads --no-model
```
