# Daily email draft evaluation

Compare the current daily digest prompt with one candidate on the same
fictional emails. The candidate asks explicitly about tentative facts,
corrections, missing information and commitments. Both runs use the existing
tool-free OpenClaw adapter, production output validation and one attempt per
prompt per case. Each call starts a fresh session. No mailbox or Telegram
connection is needed.

## Dataset

| Split | Vietnamese | English | Total | Purpose |
| --- | ---: | ---: | ---: | --- |
| dev | 3 | 3 | 6 | Try prompt changes and calibrate grading |
| test | 15 | 15 | 30 | Compare the frozen prompts |

The test set has five cases each for scheduling, amounts, corrections,
missing information, constraints and instructions quoted in email content.
Each case has a reviewer rubric with facts to retain and mistakes to avoid.
The model receives only the synthetic email. Freeze the candidate before
reviewing the test results. If you tune on test cases, reserve a new test set
for the next comparison.

## Run

From the repository root on Linux or WSL, validate the dataset offline with
Python 3.11 or newer.

```sh
python3 scripts/evaluate_daily_drafts.py validate
```

For live generation, use the existing model-only OpenClaw profile for the
`email-agent` agent with its provider authenticated. Export
`OPENCLAW_CONFIG_PATH` and `OPENCLAW_STATE_DIR` for that profile. Use absolute
paths outside the repository, and keep model state separate from email
pipeline state. The adapter requires all tools denied and both log levels set
to silent. Keep the same profile and model settings for the full run.

```sh
export OPENCLAW_CONFIG_PATH=/absolute/private/model-config.json
export OPENCLAW_STATE_DIR=/absolute/private/model-state
MODEL='provider/model-id'  # Replace with the exact provider/model for email-agent.

python3 scripts/evaluate_daily_drafts.py generate --split dev \
  --model "$MODEL" --output-dir eval-output/dev
python3 scripts/evaluate_daily_drafts.py generate --split test \
  --model "$MODEL" --output-dir eval-output/test
```

The dev run makes 12 model calls and the test run makes 60. The model argument
checks the effective identity in each OpenClaw response. To change models,
configure the profile first. A provider or model mismatch, missing identity,
or failed CLI call aborts the run. A successful call with unusable output
counts as an invalid response. Use a new output directory for each run.

## Grade

Open `review.json` and fill `grades.csv` using the two answers labeled A
and B for each case. Use `email_date` when interpreting relative dates such
as “tomorrow.” It records the same synthetic Date header both prompts
received. Keep `manifest.json` closed during grading because it contains
the assignment. Both prompts are scored on the same rubric.

| Column | Score 1 when |
| --- | --- |
| `facts_kept` | The summary preserves all facts marked in the case and the draft respects those facts. |
| `unsupported_commitment` | The draft claims a promise, approval, payment, scheduled event or action without support in the email. |
| `needs_edit` | An operator must make a meaningful correction before sending, such as fixing the language, facts, promise or next step. |

Use 0 otherwise. Add a short note on every issue and on representative
successful examples so the report can illustrate at least two cases. Leave
prefilled invalid rows as generated. These rows count against facts kept
and count as needing edit. The commitment rate uses only valid responses.

```sh
python3 scripts/evaluate_daily_drafts.py report --run-dir eval-output/test \
  > eval-output/test/report.md
```

The report opens the assignment after the grades are complete. It includes
counts and denominators, the exact provider/model, OpenClaw version, dataset
and prompt fingerprints, and up to two examples from the notes. The dataset
fingerprint is the SHA-256 of the JSONL file bytes with CRLF line endings
converted to LF. Keep the report with the reviewed test run when comparing
prompt changes.
