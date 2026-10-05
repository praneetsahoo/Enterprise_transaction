# Test suite

```bash
pytest                                   # everything that needs no database (~2 s)
pytest --cov=app --cov-report=term       # with coverage
DB_HOST=<rds endpoint> pytest            # on EC2: also runs the `live` tests against RDS MySQL 8
pytest -m live                           # only the live tests
```

* **Live tests never change real data**: each runs in a transaction that is rolled back
  (test rows use ids starting `PYT_`).
* **No real AWS calls without a DB**: S3 is faked with `moto`; database calls in orchestration
  tests are mocked.
* **Guard**: the session fails if any test writes into the project's `data/` folders.

## Brief requirement → test

| Requirement (problem statement) | Test(s) |
|---|---|
| Timestamps standardised to UTC | `test_cleaning.py::test_every_timestamp_format_becomes_the_same_utc_moment` (7 formats), `test_ist_midnight_rolls_back_to_previous_utc_day`, `test_bad_timestamps_are_rejected` |
| Currency normalised with a fixed dictionary | `test_currency_aliases` (12 spellings), `test_unknown_currency` |
| Converted to INR | `test_inr_conversion_uses_fixed_rate_and_rounds_half_up_to_paisa`, `test_valid_row_is_cleaned` |
| Flag `amount <= 0` | `test_each_invalid_field_gives_its_reason[AMOUNT_NOT_POSITIVE]` (0 and −1), `test_check_constraints_are_a_last_line_of_defence` (DB) |
| Flag missing `txn_ref_no` | `test_each_invalid_field_gives_its_reason[MISSING_TXN_REF]` (empty and blank) |
| DLQ directory keeps the original record + reason | `test_dlq_file_keeps_the_original_text_and_a_reason`, `test_dlq_keeps_original_record_as_json` (DB) |
| Every row accounted for | `test_pipeline_dry_run_reconciles_with_what_was_planted` (read = valid + rejected + duplicate, per-reason counts match the generator) |
| Batch insert, `txn_ref_no` PRIMARY KEY prevents double counting | `test_primary_key_prevents_double_counting` (DB), `test_first_valid_copy_wins_and_duplicates_are_classified`; live re-run inserted 0 |
| Settlement = amount − amount × commission_pct, JOIN on merchant_id | `test_settlement_formula_success_only_and_missing_rate` (DB) |
| Fraud: 6 failures in 10 min → flagged | `test_six_failures_within_ten_minutes_is_flagged` (DB) |
| Fraud: exactly 5 → not flagged | `test_exactly_five_failures_is_not_flagged` (DB) |
| Fraud: 6 spread beyond 10 min → not flagged | `test_six_failures_spread_beyond_ten_minutes_is_not_flagged` (DB) |
| Genuine sliding window (no daily / fixed buckets) | `test_window_crossing_a_clock_bucket_is_still_flagged` (DB), `test_fraud_query_is_a_time_based_sliding_window_not_buckets`, `test_window_query_matches_selfjoin_and_brute_force_on_random_data` (DB) |
| S3 landing / processed / DLQ copies | `test_pipeline_s3.py::test_pipeline_copies_raw_processed_and_dlq_to_s3`, `test_cli_can_read_its_input_from_s3` |
| Failures are visible (CloudWatch alarm on ERROR) | `test_bad_file_marks_run_failed_and_loads_nothing`, `test_report_runner_fails_loudly_if_the_two_fraud_methods_disagree` |

## Files

| File | Covers |
|---|---|
| `test_foundation.py` | config rules, sample-data generator is deterministic and plants the fraud cases |
| `test_cleaning.py` | every cleaning rule on its own, duplicates, merchant rates, whole-file reconciliation, DLQ |
| `test_pipeline_s3.py` | S3 (moto), pipeline success/failure orchestration, messy files (BOM, column order, header-only, quoted amounts, leading zeros), determinism |
| `test_schema.py` | schema is idempotent and non-destructive, MySQL 8, PK, CHECK constraints, DLQ JSON |
| `test_analytics.py` | settlement, sliding-window fraud (brief cases + bucket trap, boundary, bot burst, mixed statuses), 3-way cross-check, report runner |
