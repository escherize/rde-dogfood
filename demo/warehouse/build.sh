#!/usr/bin/env bash
# Build the shop_warehouse dataset into an empty Postgres 16 database.
# Connection comes from the standard libpq env vars (PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE).
#   ./build.sh    generate CSVs, load raw, run transforms, validate (writes validation.txt)
set -euo pipefail
cd "$(dirname "$0")"

python3 -I generate.py > /dev/null
psql -v ON_ERROR_STOP=1 -q -f sql/00_raw_schema.sql
for f in data/raw_*.csv; do
  table=$(basename "$f" .csv)
  psql -v ON_ERROR_STOP=1 -q -c "\\copy $table FROM '$f' WITH (FORMAT csv, HEADER true)"
done
psql -v ON_ERROR_STOP=1 -q -f sql/10_shop_analytics.sql
psql -v ON_ERROR_STOP=1 -q -f sql/20_analytics.sql
psql -v ON_ERROR_STOP=1 -q -c 'VACUUM ANALYZE'

eval "$(python3 -I -c '
import json, sys
e = json.load(open("data/expected.json"))
for k in ("stg_orders", "valid_orders", "valid_net_revenue", "valid_units_sold", "stg_customers", "stg_order_lines", "black_friday_2025_valid_orders"):
    print(f"export EXP_{k.upper()}={e[k]}")
')"
psql -v ON_ERROR_STOP=1 -X \
  -v exp_stg_orders="$EXP_STG_ORDERS" -v exp_valid_orders="$EXP_VALID_ORDERS" \
  -v exp_valid_net_revenue="$EXP_VALID_NET_REVENUE" -v exp_valid_units_sold="$EXP_VALID_UNITS_SOLD" \
  -v exp_stg_customers="$EXP_STG_CUSTOMERS" -v exp_stg_order_lines="$EXP_STG_ORDER_LINES" \
  -v exp_bf2025="$EXP_BLACK_FRIDAY_2025_VALID_ORDERS" \
  -f sql/90_validate.sql > validation.txt
echo "validation passed; see validation.txt"
