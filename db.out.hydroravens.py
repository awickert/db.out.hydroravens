#!/usr/bin/python3
# -*- coding: utf-8 -*-

"""
db.out.hydroravens - Export GRASS SQLite time series to hydroRaVENS forcing CSV
and config YAML (reads v.in.waterdata discharge + v.interp.timeseries basin mean).
"""

#%module
#% description: Export GRASS time series to hydroRaVENS forcing CSV and config YAML
#% keyword: hydrology
#% keyword: time series
#%end

#%option G_OPT_V_INPUT
#% key: basin
#% label: Basin polygon vector map (output of v.in.waterdata -b)
#% required: yes
#%end

#%option
#% key: forcing_table
#% type: string
#% required: no
#% label: SQLite table with basin-mean forcing (default: {basin}_timeseries)
#% description: Created by v.interp.timeseries with sample= option
#%end

#%option
#% key: discharge_table
#% type: string
#% required: yes
#% label: SQLite table with discharge time series (from v.in.waterdata)
#%end

#%option
#% key: lat
#% type: double
#% required: no
#% label: Basin centroid latitude [degrees N] for photoperiod (auto-computed if omitted)
#%end

#%option
#% key: discharge_unit
#% type: string
#% options: ft3/s,m3/s
#% answer: ft3/s
#% label: Unit of discharge values in the SQLite table
#%end

#%option G_OPT_F_OUTPUT
#% key: output
#% label: Output CSV file path
#% required: yes
#%end

#%option G_OPT_F_OUTPUT
#% key: config
#% label: Output hydroRaVENS config YAML file path
#% required: no
#%end

#%option
#% key: start_date
#% type: string
#% required: no
#% label: Start date YYYY-MM-DD (default: earliest in forcing table)
#%end

#%option
#% key: end_date
#% type: string
#% required: no
#% label: End date YYYY-MM-DD (default: latest in forcing table)
#%end

import datetime
import math
import os
import sqlite3

import grass.script as gs

if os.path.exists('/usr/share/proj/proj.db'):
    os.environ['PROJ_DATA'] = '/usr/share/proj'


CFS_TO_CMS = 0.0283168  # ft³/s → m³/s


def _mapset_db_path():
    env = gs.gisenv()
    return os.path.join(env['GISDBASE'], env['LOCATION_NAME'],
                        env['MAPSET'], 'sqlite', 'sqlite.db')


def photoperiod_forsythe(doy, lat_deg):
    """Day-length [hr] — Forsythe et al. (1995), Ecol. Modelling 80:87-95."""
    lat = math.radians(lat_deg)
    theta = 0.2163108 + 2.0 * math.atan(0.9671396 * math.tan(0.00860 * (doy - 186.0)))
    delta = math.asin(0.39795 * math.cos(theta))
    arg = (math.sin(math.radians(0.8333)) + math.sin(lat) * math.sin(delta)) / (
        math.cos(lat) * math.cos(delta)
    )
    arg = max(-1.0, min(1.0, arg))
    return 24.0 - (24.0 / math.pi) * math.acos(arg)


def basin_area_km2(basin_map):
    """Return basin area in km² using v.to.db -p."""
    out = gs.read_command('v.to.db', map=basin_map, option='area', flags='p', quiet=True)
    for line in out.splitlines():
        parts = line.strip().split('|')
        if len(parts) == 2:
            try:
                return float(parts[1]) / 1e6
            except ValueError:
                pass
    gs.fatal("Could not read area from '{}'.".format(basin_map))


def basin_centroid_lat(basin_map):
    """Return basin centroid latitude [°N] using bbox midpoint + m.proj."""
    info = gs.parse_command('v.info', map=basin_map, flags='g')
    cx = (float(info['east']) + float(info['west'])) / 2
    cy = (float(info['north']) + float(info['south'])) / 2
    out = gs.read_command('m.proj', coordinates='{},{}'.format(cx, cy),
                          flags='od', quiet=True)
    parts = out.strip().split('|')
    if len(parts) < 2:
        gs.fatal("m.proj failed to project basin centroid to lat/lon.")
    return float(parts[1])


def read_forcing(db_path, table, start, end):
    """
    Read PRCP, TMAX, TMIN from a v.interp.timeseries sample table.
    Returns {date_str: {'PRCP': v, 'TMAX': v, 'TMIN': v}}.
    """
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    sql = 'SELECT datetime, element, value FROM "{}" WHERE cat=1'.format(table)
    args = []
    if start:
        sql += ' AND datetime >= ?'
        args.append(start)
    if end:
        sql += ' AND datetime <= ?'
        args.append(end)
    try:
        cur.execute(sql, args)
        rows = cur.fetchall()
    except sqlite3.OperationalError as e:
        gs.fatal("Cannot read forcing table '{}': {}".format(table, e))
    conn.close()

    data = {}
    for dt, element, value in rows:
        date = dt[:10]
        data.setdefault(date, {})[element.upper()] = value
    return data


def read_discharge(db_path, table, start, end, unit):
    """
    Read daily discharge from a v.in.waterdata table.
    Returns {date_str: Q_m3s}.
    """
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    # SUBSTR(datetime,1,10) handles both bare dates and ISO timestamps (YYYY-MM-DDThh:mm:ss)
    sql = 'SELECT datetime, value FROM "{}"'.format(table)
    conds, args = [], []
    if start:
        conds.append('SUBSTR(datetime,1,10) >= ?')
        args.append(start)
    if end:
        conds.append('SUBSTR(datetime,1,10) <= ?')
        args.append(end)
    if conds:
        sql += ' WHERE ' + ' AND '.join(conds)
    try:
        cur.execute(sql, args)
        rows = cur.fetchall()
    except sqlite3.OperationalError as e:
        gs.fatal("Cannot read discharge table '{}': {}".format(table, e))
    conn.close()

    scale = CFS_TO_CMS if unit == 'ft3/s' else 1.0
    result = {}
    for dt, value in rows:
        if value is not None:
            result[dt[:10]] = float(value) * scale
    return result


def iter_dates(start, end):
    """Yield YYYY-MM-DD strings from start to end inclusive."""
    d = datetime.date.fromisoformat(start)
    end_d = datetime.date.fromisoformat(end)
    while d <= end_d:
        yield d.strftime('%Y-%m-%d')
        d += datetime.timedelta(days=1)


def write_config(path, csv_path, area_km2):
    content = (
        'timeseries:\n'
        '  datafile: {csv}\n'
        '\n'
        'initial_conditions:\n'
        '  water_reservoir_effective_depths__mm:\n'
        '    - 1.0       # shallow / overland-flow reservoir\n'
        '    - 100.0     # soil-zone reservoir\n'
        '    - 1000.0    # deep / karst reservoir\n'
        '  snowpack__mm_SWE: 0\n'
        '\n'
        'catchment:\n'
        '  drainage_basin_area__km2: {area:.1f}\n'
        '  evapotranspiration_method: ThorntwaiteChang2019\n'
        '  water_year_start_month: 10\n'
        '  baseflow_Q: 0.0\n'
        '\n'
        'general:\n'
        '  spin_up_cycles: 1\n'
        '\n'
        'reservoirs:\n'
        '  e_folding_residence_times__days:\n'
        '    - 16       # shallow (placeholder — calibrate)\n'
        '    - 200      # soil zone (placeholder — calibrate)\n'
        '    - 3650     # deep / karst (placeholder — calibrate)\n'
        '  exfiltration_fractions:\n'
        '    - 0.8      # shallow: fraction to stream vs. percolation to soil zone\n'
        '    - 0.1      # soil zone: fraction to stream vs. deep recharge\n'
        '    - 1.0      # deep: all to stream (mass conservation)\n'
        '  maximum_effective_depths__mm:\n'
        '    - .inf\n'
        '    - .inf\n'
        '    - .inf\n'
        '\n'
        'snowmelt:\n'
        '  PDD_melt_factor: 3.0   # mm SWE per positive degC per day (placeholder)\n'
        '  fgi_decay_coeff: 0.97\n'
        '  snow_insulation_k: 0.0\n'
        '\n'
        'modules:\n'
        '  snowpack: true\n'
        '  frozen_ground: true\n'
        '  rain_on_snow: true\n'
        '  direct_runoff: false\n'
    ).format(csv=os.path.abspath(csv_path), area=area_km2)
    with open(path, 'w') as f:
        f.write(content)


def main():
    options, _flags = gs.parser()

    basin          = options['basin']
    forcing_table  = (options['forcing_table']
                      or '{}_timeseries'.format(basin.split('@')[0]))
    discharge_table = options['discharge_table']
    lat_opt        = options['lat']
    discharge_unit  = options['discharge_unit']
    output_csv     = options['output']
    config_path    = options['config'] or None
    start_date     = options['start_date'] or None
    end_date       = options['end_date'] or None

    db_path = _mapset_db_path()

    # ── basin geometry ────────────────────────────────────────────────────────
    gs.message("Reading basin info from '{}'...".format(basin))
    area_km2 = basin_area_km2(basin)
    if lat_opt:
        lat = float(lat_opt)
    else:
        lat = basin_centroid_lat(basin)
    gs.message("  Area: {:.1f} km²  |  Centroid lat: {:.3f} °N".format(area_km2, lat))

    # ── forcing ───────────────────────────────────────────────────────────────
    gs.message("Reading basin-mean forcing from table '{}'...".format(forcing_table))
    forcing = read_forcing(db_path, forcing_table, start_date, end_date)
    if not forcing:
        gs.fatal("No forcing records in '{}' for the requested period.".format(forcing_table))

    all_dates = sorted(forcing.keys())
    start = start_date or all_dates[0]
    end   = end_date   or all_dates[-1]
    gs.message("  Period: {} to {} ({} days)".format(
        start, end,
        (datetime.date.fromisoformat(end) - datetime.date.fromisoformat(start)).days + 1))

    # ── discharge ─────────────────────────────────────────────────────────────
    gs.message("Reading discharge from table '{}'...".format(discharge_table))
    discharge = read_discharge(db_path, discharge_table, start, end, discharge_unit)

    # ── assemble CSV ──────────────────────────────────────────────────────────
    header = ('Date,Precipitation [mm/day],Discharge [m^3/s],'
              'Mean Temperature [C],Minimum Temperature [C],'
              'Maximum Temperature [C],Photoperiod [hr]')

    n_missing_forcing = 0
    n_missing_q = 0
    lines = [header]

    for date in iter_dates(start, end):
        doy   = datetime.date.fromisoformat(date).timetuple().tm_yday
        photo = photoperiod_forsythe(doy, lat)
        date_out = date.replace('-', '.')

        day_f = forcing.get(date, {})
        prcp  = day_f.get('PRCP')
        tmax  = day_f.get('TMAX')
        tmin  = day_f.get('TMIN')

        if None in (prcp, tmax, tmin):
            n_missing_forcing += 1

        tmean = (tmax + tmin) / 2.0 if (tmax is not None and tmin is not None) else None

        q = discharge.get(date)
        if q is None:
            n_missing_q += 1

        def _f(v):
            return '{:.6g}'.format(v) if v is not None else ''

        lines.append('{},{},{},{},{},{},{:.6f}'.format(
            date_out, _f(prcp), _f(q),
            _f(tmean), _f(tmin), _f(tmax),
            photo,
        ))

    with open(output_csv, 'w') as fh:
        fh.write('\n'.join(lines) + '\n')

    gs.message("Wrote {}: {} rows, {} missing forcing, {} missing discharge.".format(
        output_csv, len(lines) - 1, n_missing_forcing, n_missing_q))

    # ── config YAML ───────────────────────────────────────────────────────────
    if config_path:
        write_config(config_path, output_csv, area_km2)
        gs.message("Wrote config YAML: {}".format(config_path))

    gs.message("Done.")


if __name__ == '__main__':
    main()
