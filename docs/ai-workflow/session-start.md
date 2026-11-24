# Session-start prompts (copy, fill the brackets, paste)

Each project gets its own Claude Code chat, opened inside that project's repo folder. `CLAUDE.md` loads automatically, so these prompts stay short.

---

## A. First chat for a project (repo bootstrap)

Run this once, with the model on **Sonnet**.

```
We are starting the project <PageCite | FlashSale | LLM Gateway>.
Source folder: "C:\Users\hriti\OneDrive - IIT Kanpur\Desktop\Resume projects\prompts\<docmind|flashsale|llm-gateway>"

Set up this repo:
1. Copy CLAUDE.md → ./CLAUDE.md, specs.md → ./docs/specs.md, phases/* → ./docs/ai-workflow/phases/,
   ../README.md → ./docs/ai-workflow/README.md, ../log-template.md → ./docs/ai-workflow/log.md
2. Create the folder layout from CLAUDE.md (empty dirs with .gitkeep), .gitignore, .env.example (empty values).
3. git init, first commit "chore: project scaffold + design docs".
Do not write application code yet. Stop after the commit and show me the tree.
```

## B. Start a phase (every phase, new session or `/clear` first)

Start in **plan mode** (Shift+Tab). Use the model from section D.

```
Project: <name>. Completed phases: <P1, P2 | none>. Now: P<n>.
Read docs/ai-workflow/phases/P<n>.md. It is the task (Goal, Constraints, Done when, Out of scope).
Use CLAUDE.md for stack and invariants, and only the docs/specs.md sections that the phase file lists.
Give me a plan first: files to create/change, the order, and how each "Done when" check will be run.
Keep to the phase's scope. If something in the spec looks wrong or ambiguous, ask instead of guessing.
```

After you approve the plan, Claude implements it. Then:

```
Run every "Done when" check and show me the output. Then update the README section named in "Finish",
and add a P<n> row to docs/ai-workflow/log.md (I'll fill tokens from /cost).
```

## C. Continue a half-done phase (new session)

```
Project: <name>, phase P<n> is partly done. Read docs/ai-workflow/phases/P<n>.md and `git log --oneline -10`.
Tell me which deliverables and "Done when" checks are still missing (check the code, don't assume), then plan only those.
```

---

## D. Model routing: use the right model for the task

This is the core of the "I use tokens efficiently" story.

| Task type | Model | Why |
|---|---|---|
| Planning a phase, concurrency/locking logic, Lua scripts, race tests, debugging a flaky test | **Opus** (`/model opus`) | Mistakes here are expensive. One good plan saves several rounds of fixes. |
| Implementing an approved plan: routes, models, adapters, UI pages, normal tests | **Sonnet** (`/model sonnet`) | Good enough for well-specified code, at much lower cost per token. |
| Mechanical chores: README/ADR edits, renames, seed data, formatting, reading logs, simple searches | **Haiku** (`/model haiku`) | Cheapest and fastest; there is no reasoning to get wrong. |

- Shortcut: `/model opusplan` (if your version has it) uses Opus in plan mode and Sonnet when executing.
- Log which model you used per phase in `log.md`. Then you can say, for example: "planning on Opus took [X]% of tokens but cut rework; implementation ran on Sonnet."

## E. What makes you better than "someone who just pastes prompts"

You can show these in the interview:

1. **You wrote the spec and the invariants.** For example, "a seat is BOOKED by at most one booking" and "fall back only before the first byte". The AI implements them; you decide them.
2. **Every phase has runnable acceptance tests.** That is how you know AI output is correct, rather than hoping it is.
3. **You review plans and diffs.** Note in `log.md` the plan changes you made and the bugs you caught. These are your concrete stories.
4. **You manage cost:** model routing (section D), `/clear` between phases, a small CLAUDE.md, and scoped file reads. You have real token numbers from `log.md`.
5. **You can explain any line.** That is the 30-minute step after every phase. Someone who only pastes prompts can't do this.
