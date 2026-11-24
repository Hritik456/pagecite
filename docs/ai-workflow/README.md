# AI Workflow: building with Claude Code (spec-driven, token-efficient)

This folder holds everything needed to build the 3 projects with Claude Code. It is also your interview answer to "how do you use AI tools / prompt engineering / token usage?".

**Transparency:** AI use is public. Commits keep the `Co-Authored-By` trailer, and this workflow ships inside each repo at `docs/ai-workflow/`.

**Interview line:** "I work spec-first. I write the design, Claude Code implements one phase at a time against acceptance tests, and I review every diff. I tracked the token cost of each phase."

## 1. Set up a new repo (once per project)

```
mkdir llm-gateway && cd llm-gateway && git init
copy  prompts/llm-gateway/CLAUDE.md   → ./CLAUDE.md
copy  prompts/llm-gateway/specs.md    → ./docs/specs.md
copy  prompts/llm-gateway/phases/*    → ./docs/ai-workflow/phases/
copy  prompts/log-template.md         → ./docs/ai-workflow/log.md
copy  prompts/README.md               → ./docs/ai-workflow/README.md
```

Then create the GitHub repo (`gh repo create <name> --public --source . --push`).

## 2. Running one phase (repeat for every phase)

1. Start a fresh session: run `/clear`, or open a new terminal and run `claude`.
2. Turn on **plan mode** (Shift+Tab) and paste the phase prompt, e.g. the contents of `docs/ai-workflow/phases/P1.md`.
3. Read the plan. Push back on anything you don't understand or that goes beyond the phase.
4. Approve the plan and let Claude implement it.
5. Run the **Done when** checks yourself, not only Claude.
6. Run `/cost` and write the tokens and cost into `docs/ai-workflow/log.md`.
7. Commit with a message like `feat(p1): ...`. Push.
8. **Spend 30 minutes understanding it.** Ask "explain this phase like an interviewer would ask", then answer in your own words. Add any surprises to the log.

## 3. Token-efficiency rules (and why, which is the interview content)

| Rule | Why it saves tokens / improves output |
|---|---|
| Keep CLAUDE.md under 150 lines; put details in `docs/specs.md` | CLAUDE.md is loaded into **every** session and turn. Each extra line costs tokens on every request. The spec is read only when a phase needs it. |
| One phase per session, `/clear` between phases | Old context from the previous phase is noise. It costs tokens and can confuse the model. |
| Point to exact files and sections ("read docs/specs.md §5 LLD") | Stops broad repo scans and whole-file reads. |
| Always include **Done when** (a runnable check) | The model knows when to stop, and you get tests instead of vague "it should work". |
| Always include **Out of scope** | Stops over-building, which is the biggest hidden token cost. |
| Plan mode first, then implement | Fixing a wrong plan costs a few hundred tokens; fixing wrong code costs thousands. |
| Give constraints, not code | You own the design decisions (algorithms, invariants); the model writes the boilerplate. |
| Reuse the prompt template | Consistent structure gives predictable output and makes prompts easy to diff and improve. |
| Use `/compact` if a session gets long | Summarises history instead of carrying the full transcript. |
| Route models by task: Opus to plan and for hard logic, Sonnet to implement, Haiku for chores (see [session-start.md](session-start.md) §D) | Expensive reasoning is spent only where mistakes are costly. |

## 4. Phase prompt template (every file in `phases/` follows this)

```
# P<n>: <title>
Mode: plan first. Implement only after I approve.
## Goal            – one or two sentences, outcome-focused
## Read these files – exact paths/sections only
## Constraints     – stack, invariants, patterns that must be followed
## Deliverables    – files/endpoints/tests to produce
## Done when       – runnable checks (commands, tests, curl, k6)
## Out of scope    – what NOT to build yet
## Finish          – run checks, update README section, append to docs/ai-workflow/log.md
```

## 5. What to say if an interviewer asks

- **"Did you use AI?"** "Yes, Claude Code. The design docs, ADRs and phase prompts are in the repo. I wrote the specs, defined the invariants and acceptance tests, and reviewed every diff."
- **"How did you keep token usage efficient?"** Use section 3, plus real numbers from `log.md`, e.g. "the gateway took N sessions and about X tokens; plan mode cut rework".
- **"Explain this code."** This is why step 8 in section 2 is not optional.
