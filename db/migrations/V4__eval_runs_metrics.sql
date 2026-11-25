-- PageCite V4: eval_runs gets every metric P5 reports (spec §9 + P5.md), plus what makes a row
-- reproducible (commit, judge model). V1 had only hit@5, MRR and faithfulness.
--   * abstention_accuracy: share of all rows where abstained == not answerable
--   * false_abstain_rate:  share of answerable rows that abstained
--   * avg_tokens / avg_latency_ms: per question, retrieval + answer calls (the judge is excluded)
--   * n_errors: rows whose retrieval, answer or judge call failed (counted as misses, never dropped)
--   * est_cost_usd: OpenAI cost of the run, judge included, from the recorded usage

ALTER TABLE eval_runs
    ADD COLUMN label               VARCHAR(64)   NULL AFTER config,
    ADD COLUMN abstention_accuracy DECIMAL(6, 4) NULL AFTER faithfulness,
    ADD COLUMN false_abstain_rate  DECIMAL(6, 4) NULL AFTER abstention_accuracy,
    ADD COLUMN avg_tokens          DECIMAL(10, 1) NULL AFTER false_abstain_rate,
    ADD COLUMN avg_latency_ms      DECIMAL(10, 1) NULL AFTER avg_tokens,
    ADD COLUMN n_errors            INT           NOT NULL DEFAULT 0 AFTER avg_latency_ms,
    ADD COLUMN est_cost_usd        DECIMAL(10, 4) NULL AFTER n_errors,
    ADD COLUMN git_sha             VARCHAR(40)   NULL AFTER est_cost_usd,
    ADD COLUMN judge_model         VARCHAR(64)   NULL AFTER git_sha;
