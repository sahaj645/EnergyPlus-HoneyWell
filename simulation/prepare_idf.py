"""Turn ``baseline.idf`` into ``agentic.idf`` - an IDF the runtime API can actually drive.

Three transformations, each of which exists because of a specific way the runtime API bites:

**(a) Thermostat setpoint schedules become ``Schedule:Constant``.**
The obvious way to move a setpoint is a thermostat actuator, and it is a trap: the available
thermostat actuators vary by HVAC template, some prototypes expose none, and a missing one
surfaces as a ``-1`` handle with no diagnostic. Every thermostat instead *references a
schedule*, and ``Schedule:Constant / Schedule Value`` is universally actuatable. So we rewrite
each referenced ``Schedule:Compact`` into a ``Schedule:Constant`` of the same name - references
elsewhere in the file keep resolving - initialised to the original schedule's occupied value.

The cost is honest and worth stating: the daily setback profile baked into the compact schedule
is flattened. The agent (or the fallback) is now responsible for all scheduling. That is the
point - but it means ``agentic.idf`` is **not** the A/B control arm. ``baseline.idf`` is.

**(b) People objects get an explicit Fanger comfort model.**
Without ``Thermal Comfort Model 1 Type = Fanger`` the PMV output variable does not exist, and
the handle request fails silently.

**(c) Output:Variable / Output:Meter requests are added.**
Note the asymmetry that catches people out: zone air temperature and occupancy are keyed by
*zone name*, but **PMV is keyed by the People object name**. Those differ in most prototypes,
so we build the zone -> People map here and persist it in the :class:`PreparedModel` index.

Run it::

    python -m simulation.prepare_idf
"""

from __future__ import annotations

import argparse
from pathlib import Path

from common import eplus_path
from common.config import Settings
from common.log import get_logger
from common.models import PreparedModel, ZoneBinding

log = get_logger("simulation.prepare_idf")

# Plausible band for a thermostat setpoint in degrees C. Values outside it in a compact
# schedule are sentinels ("off" markers like 99 / -99), not setpoints, and must not be picked.
_SETPOINT_MIN_C = 5.0
_SETPOINT_MAX_C = 40.0

#: Fallbacks if a schedule yields no usable value at all.
_DEFAULT_COOLING_C = 24.0
_DEFAULT_HEATING_C = 21.0

#: (variable name, how the key is derived) for the runtime API and the SQL output.
ZONE_VARIABLES = (
    "Zone Mean Air Temperature",
    "Zone People Occupant Count",
    "Zone Thermostat Cooling Setpoint Temperature",
    "Zone Thermostat Heating Setpoint Temperature",
)
PEOPLE_VARIABLES = ("Zone Thermal Comfort Fanger Model PMV",)
SITE_VARIABLES = ("Site Outdoor Air Drybulb Temperature",)
SITE_KEY = "Environment"
FACILITY_METER = "Electricity:Facility"

_PEOPLE_ZONE_FIELDS = (
    "Zone_or_ZoneList_or_Space_or_SpaceList_Name",
    "Zone_or_ZoneList_Name",
)
_THERMOSTAT_ZONE_FIELDS = (
    "Zone_or_ZoneList_Name",
    "Zone_or_ZoneList_or_Space_or_SpaceList_Name",
)


# --------------------------------------------------------------------------------------
# Pure logic (unit-tested without eppy or EnergyPlus)
# --------------------------------------------------------------------------------------


def numeric_fields(values: list[str]) -> list[float]:
    """Extract the parseable numbers from ``Schedule:Compact`` field values.

    Compact schedules interleave directives (``Through: 12/31``, ``For: Weekdays``,
    ``Until: 06:00``) with the numbers that follow them. We only want the numbers, and only
    those in a plausible setpoint band - a ``Until: 24:00`` directive contains digits but is
    not a temperature, and prototypes use 99/-99 as "no control" sentinels.
    """
    out: list[float] = []
    for raw in values:
        text = str(raw).strip()
        if not text or ":" in text:
            continue
        try:
            number = float(text)
        except ValueError:
            continue
        if _SETPOINT_MIN_C <= number <= _SETPOINT_MAX_C:
            out.append(number)
    return out


def occupied_setpoint(values: list[str], kind: str) -> float | None:
    """Pick the occupied-hours setpoint out of a compact schedule's values.

    Setback always relaxes the setpoint: an unoccupied cooling setpoint is *higher* than the
    occupied one, an unoccupied heating setpoint is *lower*. So the occupied value is the
    minimum of the cooling values and the maximum of the heating values. That is a heuristic,
    but a directional one - it can only ever err toward tighter comfort, never looser.
    """
    numbers = numeric_fields(values)
    if not numbers:
        return None
    if kind == "cooling":
        return min(numbers)
    if kind == "heating":
        return max(numbers)
    raise ValueError(f"kind must be 'cooling' or 'heating', got {kind!r}")


# --------------------------------------------------------------------------------------
# eppy helpers
# --------------------------------------------------------------------------------------


def _load_idf(idf_path: Path, install_dir: Path):
    from eppy.modeleditor import IDF

    idd = install_dir / "Energy+.idd"
    if not idd.is_file():
        raise FileNotFoundError(
            f"Energy+.idd not found at {idd}; is {install_dir} an EnergyPlus root?"
        )
    IDF.setiddname(str(idd))
    return IDF(str(idf_path))


def _field(obj, candidates: tuple[str, ...]) -> str | None:
    """First populated field on ``obj`` whose name is in ``candidates``."""
    for name in candidates:
        if name in obj.fieldnames:
            value = getattr(obj, name, "")
            if value:
                return str(value)
    return None


def _compact_field_values(sched) -> list[str]:
    """Raw ``Field_N`` values of a Schedule:Compact, in order."""
    # obj[0] is the object type, obj[1] the Name, obj[2] the type-limits name; the rest are
    # the Field_N entries. Slicing the raw list avoids depending on how many Field_N slots
    # the IDD happens to declare.
    return [str(v) for v in sched.obj[3:]]


def _expand_zone_reference(idf, name: str) -> list[str]:
    """Resolve a Zone-or-ZoneList reference to concrete zone names."""
    for zone_list in idf.idfobjects.get("ZONELIST", []):
        if str(zone_list.Name).upper() == name.upper():
            return [str(v) for v in zone_list.obj[2:] if str(v).strip()]
    return [name]


# --------------------------------------------------------------------------------------
# Transformations
# --------------------------------------------------------------------------------------


def _thermostat_schedules(idf) -> dict[str, tuple[str | None, str | None]]:
    """Map zone name -> ``(heating_schedule, cooling_schedule)``."""
    by_name = {}
    for kind, heat_field, cool_field in (
        ("DUAL", "Heating_Setpoint_Temperature_Schedule_Name",
         "Cooling_Setpoint_Temperature_Schedule_Name"),
        ("HEAT", "Setpoint_Temperature_Schedule_Name", None),
        ("COOL", None, "Setpoint_Temperature_Schedule_Name"),
    ):
        objects = {
            "DUAL": "THERMOSTATSETPOINT:DUALSETPOINT",
            "HEAT": "THERMOSTATSETPOINT:SINGLEHEATING",
            "COOL": "THERMOSTATSETPOINT:SINGLECOOLING",
        }[kind]
        for obj in idf.idfobjects.get(objects, []):
            heat = getattr(obj, heat_field, None) if heat_field else None
            cool = getattr(obj, cool_field, None) if cool_field else None
            by_name[str(obj.Name).upper()] = (heat or None, cool or None)

    zone_map: dict[str, tuple[str | None, str | None]] = {}
    for control in idf.idfobjects.get("ZONECONTROL:THERMOSTAT", []):
        reference = _field(control, _THERMOSTAT_ZONE_FIELDS)
        if not reference:
            continue
        control_name = str(getattr(control, "Control_1_Name", "") or "").upper()
        schedules = by_name.get(control_name)
        if schedules is None:
            continue
        for zone in _expand_zone_reference(idf, reference):
            zone_map[zone] = schedules
    return zone_map


def convert_setpoint_schedules(
    idf, zone_map: dict[str, tuple[str | None, str | None]]
) -> dict[str, float]:
    """Rewrite referenced Schedule:Compact setpoint schedules as Schedule:Constant.

    Returns ``{schedule_name: initial_value}``. Idempotent: a schedule that is already
    Schedule:Constant is recorded and left alone.
    """
    wanted: dict[str, str] = {}
    for heating, cooling in zone_map.values():
        if heating:
            wanted[str(heating).upper()] = "heating"
        if cooling:
            wanted[str(cooling).upper()] = "cooling"

    compacts = {str(s.Name).upper(): s for s in idf.idfobjects.get("SCHEDULE:COMPACT", [])}
    constants = {str(s.Name).upper(): s for s in idf.idfobjects.get("SCHEDULE:CONSTANT", [])}

    initial: dict[str, float] = {}
    for key, kind in sorted(wanted.items()):
        if key in constants:
            existing = constants[key]
            initial[str(existing.Name)] = float(existing.Hourly_Value or 0.0)
            log.info("schedule %s already Schedule:Constant", existing.Name)
            continue

        sched = compacts.get(key)
        if sched is None:
            log.warning("setpoint schedule %s referenced but not found; skipping", key)
            continue

        name = str(sched.Name)
        limits = str(getattr(sched, "Schedule_Type_Limits_Name", "") or "")
        value = occupied_setpoint(_compact_field_values(sched), kind)
        if value is None:
            value = _DEFAULT_COOLING_C if kind == "cooling" else _DEFAULT_HEATING_C
            log.warning("no usable value in %s; defaulting to %.1f C", name, value)

        idf.removeidfobject(sched)
        new = idf.newidfobject("SCHEDULE:CONSTANT", Name=name, Hourly_Value=value)
        if limits and "Schedule_Type_Limits_Name" in new.fieldnames:
            new.Schedule_Type_Limits_Name = limits
        initial[name] = float(value)
        log.info("converted %s (%s) -> Schedule:Constant @ %.2f C", name, kind, value)

    return initial


def ensure_fanger_comfort(idf) -> dict[str, str]:
    """Give every People object a Fanger comfort model. Returns ``{zone: people_name}``."""
    zone_people: dict[str, str] = {}
    for people in idf.idfobjects.get("PEOPLE", []):
        name = str(people.Name)
        field = "Thermal_Comfort_Model_1_Type"
        if field in people.fieldnames:
            current = str(getattr(people, field, "") or "")
            if current.strip().lower() != "fanger":
                setattr(people, field, "Fanger")
                log.info("people %s: comfort model -> Fanger", name)
        else:
            log.warning("people %s has no %s field; PMV may be unavailable", name, field)

        reference = _field(people, _PEOPLE_ZONE_FIELDS)
        if reference:
            for zone in _expand_zone_reference(idf, reference):
                zone_people.setdefault(zone, name)
    return zone_people


def ensure_outputs(idf, zones: list[str], people_names: list[str]) -> None:
    """Add the Output:Variable / Output:Meter requests the bus and KPI extractor need."""
    existing = {
        (str(o.Key_Value).upper(), str(o.Variable_Name).upper())
        for o in idf.idfobjects.get("OUTPUT:VARIABLE", [])
    }

    def add(key: str, variable: str) -> None:
        if (key.upper(), variable.upper()) in existing:
            return
        idf.newidfobject(
            "OUTPUT:VARIABLE",
            Key_Value=key,
            Variable_Name=variable,
            Reporting_Frequency="Timestep",
        )
        existing.add((key.upper(), variable.upper()))

    for zone in zones:
        for variable in ZONE_VARIABLES:
            add(zone, variable)
    # PMV is keyed by People object name, NOT by zone. This is the whole reason for the map.
    for people in people_names:
        for variable in PEOPLE_VARIABLES:
            add(people, variable)
    for variable in SITE_VARIABLES:
        add(SITE_KEY, variable)

    meters = {str(m.Key_Name).upper() for m in idf.idfobjects.get("OUTPUT:METER", [])}
    if FACILITY_METER.upper() not in meters:
        idf.newidfobject(
            "OUTPUT:METER", Key_Name=FACILITY_METER, Reporting_Frequency="Timestep"
        )

    if not idf.idfobjects.get("OUTPUT:SQLITE", []):
        idf.newidfobject("OUTPUT:SQLITE", Option_Type="SimpleAndTabular")


# --------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------


def prepare(
    baseline_idf: Path,
    output_idf: Path,
    index_path: Path,
    install_dir: Path,
) -> PreparedModel:
    """Produce ``agentic.idf`` plus its :class:`PreparedModel` index."""
    idf = _load_idf(baseline_idf, install_dir)

    zone_map = _thermostat_schedules(idf)
    if not zone_map:
        raise ValueError(
            f"{baseline_idf} has no ZoneControl:Thermostat -> ThermostatSetpoint chain that "
            "this script can follow; nothing would be actuatable."
        )

    constants = convert_setpoint_schedules(idf, zone_map)
    zone_people = ensure_fanger_comfort(idf)

    zones = sorted(zone_map)
    ensure_outputs(idf, zones, sorted(set(zone_people.values())))

    output_idf.parent.mkdir(parents=True, exist_ok=True)
    idf.saveas(str(output_idf))

    model = PreparedModel(
        idf_path=str(output_idf),
        zones=[
            ZoneBinding(
                zone=zone,
                heating_schedule=zone_map[zone][0],
                cooling_schedule=zone_map[zone][1],
                people=zone_people.get(zone),
            )
            for zone in zones
        ],
        constant_schedules=constants,
    )
    model.save(index_path)

    missing_pmv = [z.zone for z in model.zones if not z.people]
    if missing_pmv:
        log.warning("no People object for zones %s - PMV will be unavailable there", missing_pmv)

    log.info("wrote %s (%d zones) and %s", output_idf, len(model.zones), index_path)
    return model


def main(argv: list[str] | None = None) -> int:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(description="Prepare an agent-actuatable IDF.")
    parser.add_argument("--baseline", default=str(settings.idf_path))
    parser.add_argument("--out", default=str(settings.simulation_dir / "agentic.idf"))
    parser.add_argument("--index", default=str(settings.simulation_dir / "agentic_model.json"))
    args = parser.parse_args(argv)

    install_dir = eplus_path.require_energyplus()
    baseline = Path(args.baseline)
    if not baseline.is_file():
        raise FileNotFoundError(
            f"{baseline} not found. Run `python simulation/fetch_assets.py` first."
        )

    model = prepare(baseline, Path(args.out), Path(args.index), install_dir)
    print(f"prepared {args.out}")
    print(f"  zones     : {', '.join(model.zone_names)}")
    print(f"  schedules : {len(model.constant_schedules)} converted to Schedule:Constant")
    return 0


__all__ = [
    "FACILITY_METER",
    "PEOPLE_VARIABLES",
    "SITE_VARIABLES",
    "ZONE_VARIABLES",
    "convert_setpoint_schedules",
    "ensure_fanger_comfort",
    "ensure_outputs",
    "numeric_fields",
    "occupied_setpoint",
    "prepare",
]


if __name__ == "__main__":
    raise SystemExit(main())
