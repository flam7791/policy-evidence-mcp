# Test fixtures

These files let the test suite run offline and deterministically, with no calls to any API.

- `dataflows.xml` and `structure_msti.xml` follow the SDMX-ML 2.1 structure format returned by
  the OECD API. They are trimmed to a few codes per code list.
- `data_msti.csv` follows the SDMX-CSV "with labels" layout.
- `sample_report.pdf` is a two-page fictional PDF for testing page-level citations.

**All numeric values are synthetic test data, not OECD statistics.** Check the live format with
`python scripts/smoke_live.py`; if the provider changes its layout, refresh these files from a
real response and re-run the tests.
