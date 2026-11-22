# Inference Gateway 0.4.6 lab tools

Three tools for the operator to run **on the hub (HUB, 192.0.2.254)**, where the Gateway runs. This laptop cannot reach the lab, so these tools give the only real-hardware evidence for G1, G3, G4 and G6. Each tool is one self-contained Python file (standard library only) with one `.cmd` entry. Each uses the hub's machine-wide `C:\Program Files\Python312\python.exe -I`. The `.cmd` asks Windows for administrator rights: the tools read the Gateway's protected observation folder, and the identity steps write the Gateway ledger through the Gateway's own credentials tool.

All three tools:
- **Refuse to start while the Gateway has work.** They read the Gateway observation file: it must be younger than 60 s, admission must be open, and no request may be running or queued (maintenance probes excepted). If any of that fails, the tool says why, writes a `refused.json` note and stops. G6 and G4 also stop mid-run if other work appears.
- Change no capacity setting (slots, memory), `model_capacity`, `placement_policy` or `failure_policy`, qualification record, installed model, or any other client.
- Write their results to `results\<COMPUTER>-<tool>-<time>\` beside the tool: `report.json` (everything) and `summary.txt` (the table). Bring those folders back. They contain no token.
- Ask for confirmation (`yes`) before sending anything. `--yes` skips the question.

## The Gateway identity (G6 and the launch storm)

By default G6 and the launch storm issue a **temporary Gateway identity of their own**, `lab-capacity-check`, and revoke it when they end. This needs Gateway **0.4.6** on the hub.
- **Issue:** the Gateway's own `credentials reissue` reads a clients-only policy entry: models `qwen3.5:4b` and `qwen3:4b` only, `max_concurrency` 24, queue 24, priority 0 (the lowest), `/api/chat` and `/api/generate`, context 8192. No bootstrap runs.
- **Only temporary identities:** `reissue`, `grant-heads` and `withdraw-heads` refuse, and change nothing:
  - any name that does not start with `lab-`;
  - any application the Gateway did not issue as temporary (for example `lab-traffic`, `capstone`, `capstone-research`);
  - a policy file that is not clients-only with exactly that one application.
- **Never ahead of real work:** the temporary identity's queued requests never age in priority and always come after every real application's waiting requests.
- **Expiry:** the identity expires by itself: G6 after the planned run plus 30 minutes, the launch storm after 40 minutes. After that the Gateway refuses its token, and the Gateway's health cycle revokes it and removes it from the heads, even if the tool was killed.
- **Heads:** `credentials grant-heads --like capstone-research` lets the identity use exactly the heads the research identity may use. Use `--heads-like` to name another template; it is only read.
- **`withdraw-heads`** removes the identity from **every** head. That is right for the temporary identity, which has no heads before the tool grants them.
- **Token folder:** `C:\ProgramData\AgentArenaLab\lab-capacity-check\`. Before anything is written there, the tool checks:
  - the folder's owner and every permission on it and on everything in it: only SYSTEM, Administrators and the operator;
  - the parent folder: nobody else may delete or take it over.

  If anything else is found, the tool refuses and says what it found. Remove that folder as an administrator, then start again. A new folder is created with only those three and checked again. The token is never on the stick, never printed and never in the results.
- **Revoke:** always, in a `finally`: on success, an error, a refusal or Ctrl+C. The steps are `credentials revoke` (disables the identity and cancels its queued requests), then `credentials withdraw-heads`, then the token, client config and policy file are deleted.
- **Interrupted runs:** after issuing, the tool prints what to do if the window is killed and writes the same text to `IF-INTERRUPTED.txt` in the results folder. `<the .cmd> --cleanup` revokes and removes the identity at once. The next start of either tool also does that first.
- **History:** revoking leaves a disabled `lab-capacity-check` row in the ledger. The Gateway never deletes clients.
- **Outside work:** G6 checks the Gateway ledger every 2 s for any running or queued request that is not its own. On one, it stops the level at once and revokes the identity, which cancels its queued requests (running calls end as the Gateway allows). It does not wait for its own backlog. The launch storm checks before and during every round.

`--client-config <path>` uses an existing approved identity instead (for example on a 0.4.5 Gateway, which has no temporary-identity commands). G4 does not use the temporary identity: it is limited to the two qwen models. G4's Gateway step runs only with `--client-config` of an identity permitted to use gemma3:4b. Without one, G4 runs its direct step only and says so.

| Tool | Entry | Time | What it sends |
|---|---|---|---|
| G6 capacity check | `g6-capacity-check\G6-CAPACITY-CHECK.cmd` | about 60–65 min | real inference at 1, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22 and 24 closed-loop callers (`--levels` changes the list), each level 30 s warm-up + 4 min, replaying a fixed sample of 39 completed c922 requests (qwen3.5:4b main roles and the qwen3:4b reviewer, shuffled once with a fixed seed so even 1 caller sends the reviewer's share) |
| G4 Gemma 500 reproduction | `g4-gemma500-repro\G4-GEMMA500-REPRO.cmd` | about 10 min | 17 sequential gemma3:4b chat calls direct to one idle head (default 192.0.2.10): the 4 distinct saved bodies ×3 and 5 variants of the target; plus 2 through the Gateway with `--client-config` |
| Launch storm (G1/G3) | `launch-storm\LAUNCH-STORM.cmd` | a few min | discovery only (models, policy, availability, and on 0.4.6 policy-only and fleet), 16/18/24 simultaneous launches × 3 rounds |

G6 prints its progress as it goes (for example `level 6/13: 10 callers, 2:10 left in this window`). It rewrites `report.json` and `summary.txt` after every level, so a stopped run keeps its finished levels.

Order suggested on the hub:
1. Update the Gateway to 0.4.6 (provisioner "hub > 3 > 2").
2. **Launch storm.**
3. **G4** on an idle head that has gemma3:4b. Its direct step shows the head's own error body.
4. **G6** last, with nothing else running (about an hour).

Notes:
- G4's direct calls go straight to the head's Ollama (`http://192.0.2.N:11434/api/chat`), exactly as the Gateway would send them. Before each call the tool checks that the Gateway has nothing on that head and nothing queued. It compares the head's runtime and model digest with the c922 build (Ollama 0.40.15, gemma3:4b `68ceab7e…`).
- If G4's Gateway step reproduces the 500, gemma3:4b is suspended on that head until its automatic recovery probe passes (about 60 s). That is the Gateway's normal protection, not a policy change.
- During G6 and the launch storm, the Gateway observation (and so the Arena admin console's Gateway view and fleet page) lists the application `lab-capacity-check`. It shows as enabled during the run and disabled afterwards; the row stays.
- Exit codes: 0 done, 1 refused or stopped, 2 bad arguments.

Sources: `lab-tools/src` (assembled by `scripts/build_lab_tools.py`; fixtures extracted from Capstone `outputs/experiment-evaluation-c922/calls.json`). Tests: `tests/test_lab_tools.py`.
