# Held-out email draft evaluation

The candidate preserved all marked facts in 28 of 30 cases, one more than the baseline. Both prompts produced two drafts that the reviewer marked for editing. These results support merging the evaluation and its evidence. They give little basis for changing the production prompt.

| Locked blind grade | Baseline | Candidate |
| --- | ---: | ---: |
| Valid summary and draft | 30/30 | 30/30 |
| Summary kept all marked facts and draft respected them | 27/30 | 28/30 |
| Draft made an unsupported commitment (valid drafts only) | 0/30 | 0/30 |
| Outgoing draft needed a meaningful edit | 2/30 | 2/30 |

The paired results show where the totals differ. On factual fidelity, both passed 26 cases, only the candidate passed two, only the baseline passed one, and both failed one. For edits, 27 pairs needed no edits, one needed edits for both prompts, and one needed edits for each prompt alone.

## What the reviewer saw

- **Interview time zone (test-02).** The sender asked for the recipient’s time zone before scheduling. Both drafts supplied “Asia/Bangkok (UTC+7)” without confirmation from the recipient. Both required an edit even though neither promised to attend.
- **Vietnamese quote (test-07).** The sender needed the recipient’s headcount to prepare a provisional VND 4,800,000 quote excluding VAT. The baseline asked the sender how many people the price covered. The candidate correctly acknowledged that the count was still unknown.
- **Training correction (test-28).** Both drafts acknowledged that training was pending. Only the candidate’s summary also recorded the conflict with the quoted template. The baseline lost the facts point and its outgoing draft still passed.
- **Date visible only to the drafting model (test-04).** The candidate expanded “before noon tomorrow” to January 13. The blinded reviewer marked the added date as unsupported and the draft as needing an edit. The original model input also contained the synthetic header `Mon, 12 Jan 2026 09:00:00 +0000`, which supports January 13. The review packet omitted that header. This is a limitation of that judgment. The table preserves the grades locked before unblinding.

## Run and verification

At commit `a3160ef6d7337d73cb7fbed391ae30c1e26d182f`, both frozen prompts ran on the same 30 fictional held-out emails (15 Vietnamese and 15 English, five per category). There was one fresh session per prompt per case and zero development calls. All 60 returned envelopes identified `openai/gpt-6-astra` under OpenClaw 2026.9.5 (`ec9c1a1`). The dedicated Ubuntu/WSL profile used OpenClaw’s built-in runtime, denied all model tools and had its own state and workspace. No Gmail or Telegram connection was used.

Hydrogen checked the model ID, exit status, single text payload and output contract for all 60 calls. Gemini 4 Argon High graded the anonymized answers against the case rubrics. The reviewer supplied the following SHA-256 of the locked grades before Astra opened the A/B assignments. A single AI reviewer, one generation per prompt per case and 30 synthetic cases limit how broadly the differences can be interpreted.

| Input or evidence | SHA-256 (LF bytes) |
| --- | --- |
| Dataset | `6cee8ad64d7e5f451fdec80dffe750f3f97017652b376e583eddb5beecd2795e` |
| Baseline prompt | `14db6b9bf65430b2d748d8f36a5af2b3425e88ec7fa48493e467f618e142fd94` |
| Candidate prompt | `89fd8584d257377022d8fd531a12e29c0c483c79a5bac7db3eb5a44d70f20947` |
| Blind packet | `9aa0779bd7d22c03a0af10369a0f4018783c682a71f2203a64bc33478e041290` |
| Locked grades | `2367bef2f5df3af7c9839ee87ebfbe3b6c4b5603e7acd43d237d7478a251215f` |

From the repository root, reproduce the counts from the archived evidence without a model call.

```sh
python3 scripts/evaluate_daily_drafts.py report --run-dir evals/results/heldout-a3160ef
```

The packet contains the original cases and A/B answers. The grade sheet contains every judgment and the assignment file makes the totals reproducible. [Blind packet](review.json) · [Locked grades](grades.csv) · [Assignments and run metadata](manifest.json)
