# Docs health check (GitHub Action)

Scans a documentation site or help center with [infoharness](https://infoharness.com) and fails the job when the grade drops below a threshold.

```yaml
name: Docs health
on:
  schedule: [{cron: "0 6 * * 1"}]   # every Monday
  workflow_dispatch:
jobs:
  docs:
    runs-on: ubuntu-latest
    steps:
      - uses: Ayush85/infoharness/.github/actions/docs-health@main
        with:
          url: https://docs.example.com
          fail-below: B             # fail when the grade is C, D or F
          # api-key: ${{ secrets.INFOHARNESS_API_KEY }}   # optional, free, higher limits
```

Outputs: `grade`, `score`, `issues`, `report-url`. The job summary shows the findings per check.
