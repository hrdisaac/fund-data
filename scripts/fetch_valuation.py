# Daily job for the fund viewer's data relay:
#   1) 蛋卷基金 index valuation table   → data/index_valuation.json
#   2) fund / ETF holdings with weights  → data/holdings.json   (codes listed in funds.txt)
# File location:  .github/workflows/fetch-valuation.yml
name: Fetch index valuation

on:
  schedule:
    # GitHub uses UTC. 14:30 UTC = 22:30 Beijing; 23:30 UTC = 07:30 Beijing (second chance if the first run fails).
    - cron: '30 14 * * *'
    - cron: '30 23 * * *'
  workflow_dispatch:        # adds a "Run workflow" button for manual runs

permissions:
  contents: write           # needed to save the updated data files

concurrency:
  group: fetch-valuation
  cancel-in-progress: false

jobs:
  fetch:
    runs-on: ubuntu-latest
    timeout-minutes: 25
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Fetch and check the valuation data
        run: python scripts/fetch_valuation.py

      - name: Fetch fund holdings (codes from funds.txt)
        if: always()                      # holdings still run if the valuation step failed
        run: python scripts/fetch_holdings.py

      - name: Save the data if it changed
        if: always()
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add data/
          if git diff --cached --quiet; then
            echo "No changes to save."
          else
            git commit -m "Update data $(date -u +%F)"
            git push
          fi
