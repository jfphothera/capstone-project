# Inference Gateway 0.4.2: release notes (2026-11-13)

Same release type as 0.4.1. It updates 0.4.1 in place with `gateway-deploy.py update`, keeps the ledger (same `ledger_id`), and keeps `shared.inference/1` and `shared.inference.observation/1`. No schema change. Everything in [RELEASE-NOTES-0.4.1.md](RELEASE-NOTES-0.4.1.md) still applies.

**Client library: unchanged.** Capstone keeps `client/shared_inference_client.py` **0.4.1** (SHA-256 `e7065e412bdf7e6777714ab51ebafa520c2984e344d964741b9f60f7a4bdce02`, the same file as in 0.4.1).

## The one change: the completion test accepts reasoning models — fixed

**Field, 2026-11-13 (hub on 0.4.1, heads on Ollama 0.40.13):** deepseek-r1:8b and deepseek-r1:14b failed the completion qualification. 0.4.1 required `done_reason` `stop` within `num_predict` 128 and an answer after the reasoning; DeepSeek-R1 is still reasoning at 128 tokens, so it stops on `length`.

**0.4.2 rule** (`capacity_gateway.completion_test`, used by qualification and by the recovery probe): a model passes when it **produced output for the prompt**:
- `done` is true and `eval_count` > 0 (when a runtime reports no `eval_count`: non-empty output text, or a separate `thinking` field);
- whether `done_reason` is `stop` or `length`;
- whether that output is still inside a reasoning block.

Recorded in the evidence's `measurement`, never required: `answered` (text after the reasoning), `reasoning`, `finished_reasoning`, `answer_matches_ready` (case and punctuation ignored), `done_reason`, `eval_count`. A model that produces nothing (`eval_count` 0) or never reports `done` still fails.

**Deadline.** `num_predict` stays 128 and the qualification deadline stays 90 s. Passing no longer needs the model to finish, so 128 tokens are enough for every model; the lab heads already produced 128 tokens of DeepSeek-R1 within the deadline (that is how they reached `length`). Measured here, from a cold load: deepseek-r1:8b 28.6 s, deepseek-r1:14b 61.8 s (22.4 s of it loading the model). A slower head can be given more time per run with `qualify --timeout` (up to 300 s).

## How it was tested

| What | Result |
|---|---|
| **Real deepseek-r1:8b**, local Ollama 0.40.16 on the development laptop (CPU), cold, through `Gateway.qualify` and the real HTTP transport | **Passed** under 0.4.2. `done_reason` length, `eval_count` 128, still inside `<think>` ("First, the user said: …"). The same reply **fails** the 0.4.1 rule |
| **Real deepseek-r1:14b**, same setup | **Passed**; length, 128 tokens, still reasoning ("Okay, so I need to figure out …"). Fails the 0.4.1 rule. 61.8 s of the 90 s deadline |
| Real qwen3:4b, same setup | Passed under both rules (`stop`, 54 tokens, reasoning finished, answer READY) |
| `tests/test_live_ollama.py` (opt-in) with `INFERENCE_GATEWAY_LIVE_MODEL` deepseek-r1:8b and deepseek-r1:14b | 1 passed each |
| `tests/test_release_0_4_1.py`: the rule on the field answers (reasoning at `length`, reasoning in a `thinking` field, `Ready.`, refusals, nothing produced, not done, no `eval_count`) and qualification of every field model | Pass; `endless:1` (never finishes) now passes, `silent:1` (produces nothing) fails |
| Full suite (`verification/test-report.json`) | 163 tests: 155 passed, 3 skipped (the opt-in live tests, run separately: 3 passed on qwen3:4b), 5 failed: the same 4 environmental ACL-script failures as in 0.4.1 (domain groups in local Administrators cannot be expanded on this laptop; the unchanged 0.4.0 code fails them too), and 1 flaky publisher test (it read the observation file during its atomic replace; passed 8 of 8 re-runs; code unchanged since 0.4.0) |

Evidence: `verification/live-ollama-0.4.2-reasoning.json` (measurements and the start and end of each reply; no lab data). The two DeepSeek-R1 models were pulled to the laptop only for this test and removed afterwards. No lab head or the hub was contacted.

**Not tested here:** Ollama 0.40.13 (the heads' version) and the other DeepSeek-R1 sizes on the lab list. The rule depends only on `done` and `eval_count`, which 0.40.13 reports the same way, as the field result shows.

## Upgrade notes (0.4.1 → 0.4.2)

- Run `update` from the 0.4.2 package; expect `ledger.kept: true`, `held_at_upgrade: []`.
- Then re-run the completion qualification of the reasoning models that failed under 0.4.1 (deepseek-r1:8b, deepseek-r1:14b, and any other reasoning model in the list). A model that left a head's list still needs a passing test.
- Rollback to 0.4.1 is supported (no schema change).
