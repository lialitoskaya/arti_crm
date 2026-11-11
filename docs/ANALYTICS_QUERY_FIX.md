# Analytics query correction — 2026-11-05

The preserved emergency change bound the daily-request timezone modifier in the
position of the coarse date lower bound. Correct the parameter order so requests
within the selected local day are counted again.

Response-time calculation previously windowed the entire message history. Select
candidate chat IDs using the existing direction/created_at index, then retain the
complete history of those chats. Do not filter their individual messages by date:
blocks beginning before the period and answers after it must retain their meaning.

Validation: `python -m unittest discover -s tests -p test_analytics_query_bounds.py -v`
passes three tests. The daily-request regression fails against the parent version.
Synthetic SQLite data covers timezone boundaries, daily deduplication, preperiod
block starts, future replies, and equivalence to the unbounded response query.
With 10,000 unrelated historical messages the optimized query uses less than one
fifth of the SQLite progress-handler ticks of the original query. This measures
SQL work on synthetic data, not production latency.

Python syntax and Git whitespace checks pass. The full pytest suite was not run:
pytest and application dependencies are not installed in this local environment.
No production database was opened and no hosting deployment was performed.
Other analytics scans and very large active chats may still be slow; production
latency must be checked after deployment. The separate frontend issue where
filter changes during an in-flight request are dropped remains outside this fix.
