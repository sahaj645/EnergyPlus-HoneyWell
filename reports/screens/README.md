# Dashboard screenshots

Per-section PNGs of the live ops dashboard, for the deck. **Regenerated, not committed**
(the images themselves are gitignored - they are large binaries tied to a specific run).

## Regenerate

1. Have a run's data on disk: an A/B export (`reports/results.json` via
   `python -m experiments.ab` then `python -m experiments.report`), a telemetry `*.sqlite`,
   and optionally an endurance checkpoint under `experiments/results/endurance_*/`.
2. Serve the dashboard:

   ```bash
   streamlit run dashboard/app.py
   ```

3. Export one PNG per section (drives headless Edge/Chrome over CDP, waits for each section to
   actually render before capturing):

   ```bash
   python -m dashboard.export_screens
   ```

   Produces `1_headline.png` … `5_llmops.png` here. Add `--prefix demo_` for captures taken
   against synthetic verification data (those stay local, distinct from real-run deck assets).

The dashboard reads everything read-only over WAL SQLite, so it is safe to screenshot *while*
an endurance run is still writing.
