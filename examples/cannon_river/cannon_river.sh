#!/bin/bash
# Cannon River hydroRaVENS input pipeline
#
# Builds forcing.csv and config.yml for the Cannon River near Red Wing, MN
# (USGS 05355200, ~3800 km²) using GHCN station data (~1890–present).
#
# Create a new GRASS location before running (UTM Zone 15N, EPSG:32615):
#   grass -c EPSG:32615 ~/grassdata/CannonRiver/PERMANENT
#
# Then run from within that session, or via:
#   grass ~/grassdata/CannonRiver/PERMANENT --exec bash cannon_river.sh
#
# Required addons (install with g.extension if needed):
#   v.in.waterdata  v.in.ghcn  v.interp.timeseries  db.out.hydroravens

set -e

GAUGE=05355200
START=1890-01-01
END=2024-12-31
OUTDIR=$(dirname "$0")

# ── 1. Fetch discharge time series and upstream basin polygon ─────────────────
# -b imports the upstream drainage basin as a polygon map (cannon_basin)
v.in.waterdata \
    sites=$GAUGE \
    output=discharge_${GAUGE} \
    basins=cannon_basin \
    start_date=$START \
    end_date=$END \
    -t

# ── 2. Set region to basin extent with padding for station search ─────────────
g.region vector=cannon_basin res=1000 -a

# ── 3. Import GHCN stations and time series ───────────────────────────────────
# min_stations=20 ensures adequate spatial coverage; the bbox expands
# automatically (0.5° per step) until 20 stations with ≥10 years are found.
# PRCP + TMAX + TMIN are the three forcing variables hydroRaVENS needs.
v.in.ghcn \
    output=ghcn_stations \
    elements=PRCP,TMAX,TMIN \
    start_date=$START \
    end_date=$END \
    min_years=10 \
    min_stations=20

# ── 4. Interpolate station data to basin-mean time series ─────────────────────
# IDW interpolation; sample=cannon_basin averages over the basin polygon.
# Runs three times — once per element — so that error tables track per-element
# station counts and LOO RMSE.
for ELEM in PRCP TMAX TMIN; do
    v.interp.timeseries \
        input=ghcn_stations \
        element=$ELEM \
        method=idw \
        sample=cannon_basin \
        start_date=$START \
        end_date=$END \
        -f
done

# cannon_basin_timeseries now holds PRCP, TMAX, TMIN for the basin.
# cannon_basin_errors holds n_stations and LOO RMSE per time step per element.

# ── 5. Export to hydroRaVENS format ──────────────────────────────────────────
db.out.hydroravens \
    basin=cannon_basin \
    discharge_table=discharge_${GAUGE}_timeseries \
    output=${OUTDIR}/cannon_forcing.csv \
    config=${OUTDIR}/cannon_config.yml

echo "Done. Inputs written to ${OUTDIR}/"
echo "  cannon_forcing.csv  — daily P, Q, T, photoperiod"
echo "  cannon_config.yml   — template config (calibrate reservoir parameters)"
