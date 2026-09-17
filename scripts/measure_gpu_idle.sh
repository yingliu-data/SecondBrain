#!/usr/bin/env bash
# PW-3 — idle GPU draw, resident vs unloaded.
#
# WHY: SYSTEM_DESIGN.md section 8.4 says electricity is the dominant running
# cost and that the resident-vs-on-demand decision "needs a real measurement of
# idle GPU draw before deciding" (open question 11.4). It also gates P4-6.
# Keeping a 14B model resident costs idle watts continuously; loading on demand
# costs 30-60s on the first query of the day. Nobody has measured either side.
#
#   ./scripts/measure_gpu_idle.sh resident 3600 > resident.csv
#   docker compose stop llm
#   ./scripts/measure_gpu_idle.sh unloaded 3600 > unloaded.csv
#   ./scripts/measure_gpu_idle.sh compare resident.csv unloaded.csv
set -euo pipefail

sample() {
  local label="$1" secs="${2:-3600}" interval=5
  command -v nvidia-smi >/dev/null || { echo "nvidia-smi not found — run this on the GPU box" >&2; exit 1; }
  echo "label,timestamp,power_w,mem_used_mib,util_pct"
  local end=$(( $(date +%s) + secs ))
  while [ "$(date +%s)" -lt "$end" ]; do
    nvidia-smi --query-gpu=power.draw,memory.used,utilization.gpu \
               --format=csv,noheader,nounits \
      | awk -v l="$label" -v t="$(date -Is)" -F', *' '{print l","t","$1","$2","$3}'
    sleep "$interval"
  done
}

compare() {
  local a="$1" b="$2"
  command -v python3 >/dev/null || { echo "python3 required" >&2; exit 1; }
  python3 - "$a" "$b" <<'PY'
import csv, statistics, sys

def load(path):
    with open(path) as f:
        rows = [r for r in csv.DictReader(f)]
    return ([float(r["power_w"]) for r in rows],
            [float(r["mem_used_mib"]) for r in rows],
            rows[0]["label"] if rows else path)

(pa, ma, la), (pb, mb, lb) = load(sys.argv[1]), load(sys.argv[2])
med_a, med_b = statistics.median(pa), statistics.median(pb)
delta = med_a - med_b
print(f"{la:>10}: median {med_a:6.1f} W   VRAM {statistics.median(ma):7.0f} MiB   n={len(pa)}")
print(f"{lb:>10}: median {med_b:6.1f} W   VRAM {statistics.median(mb):7.0f} MiB   n={len(pb)}")
print(f"\ndelta: {delta:.1f} W continuous for keeping the model resident")
kwh = delta * 24 * 365 / 1000
print(f"       {kwh:.0f} kWh/year")
for tariff, name in ((0.25, "UK ~25p/kWh"),):
    print(f"       ~£{kwh*tariff:.0f}/year at {name}")
print("\nTrade-off: that is the standing cost of avoiding a 30-60s cold load on")
print("the first query of the day. Design section 8.4 says start resident and")
print("revisit — this number is what 'revisit' needs. Note the tier split makes")
print("on-demand SAFE to try: nothing in the deterministic tier needs the GPU warm.")
PY
}

case "${1:-}" in
  resident|unloaded) sample "$1" "${2:-3600}" ;;
  compare)           compare "${2:?need resident.csv}" "${3:?need unloaded.csv}" ;;
  *) echo "usage: $0 {resident|unloaded} [seconds] | compare <a.csv> <b.csv>" >&2; exit 2 ;;
esac
