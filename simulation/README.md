# simulation/

EnergyPlus assets and run scripts. Large binary-ish inputs are **not** committed; drop them
here before the first run.

## Files you must supply

| File | Where to get it |
|---|---|
| `baseline.idf` | An EnergyPlus prototype model, e.g. the DOE Medium Office reference building shipped in `$ENERGYPLUS_DIR/ExampleFiles/`. |
| `weather.epw` | An Indian TMY file from [climate.onebuilding.org](https://climate.onebuilding.org) — e.g. Chennai, Ahmedabad or Hyderabad for a cooling-dominated case. |

## Versioning convention

- `baseline.idf` — the control arm. **Never edited.**
- `v1_<slug>.idf`, `v2_<slug>.idf`, … — written by `mcp_server.tools.patch_model` via eppy,
  one file per accepted model edit, numbered monotonically.

Run outputs land in `out/` (gitignored).

## Running

```bash
export ENERGYPLUS_DIR=/usr/local/EnergyPlus-24-1-0   # Windows: set it to C:\EnergyPlusV24-1-0
python -m simulation.run_baseline
```
