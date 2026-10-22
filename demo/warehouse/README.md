# shop_warehouse

A Postgres 16 warehouse for a Shopify direct-to-consumer outdoor-apparel store ("Northbound Supply Co.") covering two years, 2024-10-01 to 2026-09-30. It holds raw connector landings and the marts transformed from them. A parent Metabase connects to it for MB Code workspace testing. It replaces rde-dogfood's Neon `neondb` and keeps that database's `shop_analytics` names.

- Load it for the demo with `load.sh` ([the demo guide](../README.md#step-1-load-the-warehouse)). It builds the dump, restores it, and makes the parent's login the owner of the data.
- Dump: `shop_warehouse.dump` (`pg_dump -Fc -Z 9 --no-owner --no-privileges`, made with pg_dump 16, 128 MB). It isn't in git: `load.sh` builds it. Restore it with pg_restore 16. pg_restore 17 emits `SET transaction_timeout`, which a 16 server rejects.
- Built data: 522 MB in Postgres, about 2.5M rows.
- Deterministic: seed `20241001`. Reruns give byte-identical CSVs.
- Modeled on metabase/evals `datasets/ecommerce_v1`, with five times its marts, real raw messiness and more volume.

## Layout

| Path | What |
|---|---|
| `generate.py` | Seeded generator, Python stdlib only. Writes `data/raw_*.csv` and `data/expected.json` (an oracle computed from the in-memory rows). |
| `sql/00_raw_schema.sql` | Raw landing DDL. |
| `sql/10_shop_analytics.sql` | Staging tables and the original marts (`CREATE TABLE ... AS SELECT`). |
| `sql/20_analytics.sql` | Business marts. |
| `sql/90_validate.sql` | Row counts, reconciliation, oracle and orphan checks, sanity queries. Fails if any check fails. |
| `build.sh` | Generates the CSVs, loads them with `\copy`, runs the transforms, then validates and writes `validation.txt`. |
| `validation.txt` | Output of the last build. |
| `load.sh` | Builds or reuses the dump, restores it into `WAREHOUSE_ADMIN_URL`, creates the parent's login and hands it the data. |

Rebuild into an empty database, then dump:

```bash
docker run -d --name mbc-dataset-build -e POSTGRES_PASSWORD=build -e POSTGRES_DB=warehouse -p 127.0.0.1:55432:5432 postgres:16
PGHOST=127.0.0.1 PGPORT=55432 PGUSER=postgres PGPASSWORD=build PGDATABASE=warehouse ./build.sh
docker exec mbc-dataset-build pg_dump -U postgres -d warehouse -Fc -Z 9 --no-owner --no-privileges > shop_warehouse.dump
```

The generator takes about 30 seconds and the full build about 45 seconds.

## Schemas and row counts

### Raw layer (connector-shaped)

The raw tables have no primary or foreign keys, and they carry connector bookkeeping columns (`_fivetran_synced`, `_fivetran_deleted`, `_loaded_at`). Timestamps are `timestamptz`. Shopify timestamps arrive with the shop's UTC offset, and Stripe and marketing timestamps arrive in UTC.

| Table | Rows | Notes |
|---|---:|---|
| `raw_shopify.customers` | 20,972 | Append mode: 1,151 rows are stale earlier versions. |
| `raw_shopify.products` | 500 | 8 categories in `product_type`. Statuses: active, archived, draft. |
| `raw_shopify.product_variants` | 3,439 | Size and color options, sale `compare_at_price`. |
| `raw_shopify.discounts` | 30 | Welcome, BFCM, summer, VIP, creator and expired codes. |
| `raw_shopify.orders` | 140,795 | 119,663 distinct ids. Append mode: 21,132 stale versions. JSON columns for addresses, discount codes, `total_price_set` and `note_attributes`. |
| `raw_shopify.order_lines` | 292,094 | |
| `raw_shopify.refunds` | 6,788 | `refund_line_items` is JSON. |
| `raw_shopify.transactions` | 217,898 | Kinds: authorization, capture, sale, void, refund. Includes failed attempts. |
| `raw_stripe.charges` | 94,614 | Integer cents. `metadata.shopify_order_id` links a charge to its Shopify order. |
| `raw_stripe.refunds` | 4,714 | |
| `raw_stripe.payouts` | 523 | |
| `raw_marketing.ad_spend` | 7,485 | Rows by day, channel and campaign for google, meta, tiktok and pinterest. Pinterest starts 2025-03. |
| `raw_marketing.web_sessions` | 841,786 | UTM parameters, referrer, device, `converted_order_id`. |

The raw data contains these deliberate problems (counts from `validation.txt`):

- **Stale versions:** 21,132 order rows and 1,151 customer rows are older versions. Keep the latest `updated_at` per id.
- **Test orders:** 216 orders have `test = true` and use the `bogus` gateway and staff accounts.
- **Soft deletes:** 221 orders, 268 customers and 13 products have `_fivetran_deleted = true`.
- **Duplicate customer accounts:** 690 accounts share an email that differs only by case or by padding, for example `Jane.Doe@Gmail.com` and ` jane.doe@gmail.com`. 348 accounts store an email that is not lowercase and trimmed.
- **Redacted customers:** 59 GDPR-redacted customers have a null email, name and address.
- **Guest checkouts:** 4,710 orders have `customer_id IS NULL`. Some of these emails match an existing customer.
- **Currencies:** 32,286 orders have a presentment currency other than USD (CAD, GBP, EUR or AUD). Shop money is always USD. The presentment amount is only in `total_price_set` JSON.
- **Unmatched order lines:** 473 lines point at four products that were deleted before the first sync and never landed. 2,328 custom lines (gift wrap) have no product.
- **Late syncs:** 707 order rows were synced more than 2 days after their last update.
- **Ad spend restatements:** 272 rows are the same day and campaign landed again with a later `_loaded_at`.
- **Stripe charges outside Shopify:** 240 wholesale-invoice charges have no Shopify order.
- **UTM spellings:** `utm_source` has 33 spellings, for example `facebook`/`fb`/`ig`/`Facebook` and `google`/`Google`/`adwords`.

### `shop_analytics`: staging and original marts (names match the Neon `neondb`)

| Table | Rows | Grain and key |
|---|---:|---|
| `stg_shopify_customers` | 19,205 | One row per person. PK `customer_id`. |
| `stg_shopify_products` | 500 | PK `product_id`. Archived and deleted products are kept. |
| `stg_shopify_orders` | 119,226 | One row per real order. PK `order_id`, FK `customer_id`. |
| `stg_shopify_order_lines` | 291,083 | PK `order_line_id`, FK to orders and products. |
| `daily_sales` | 730 | One row per shop-local day. PK `order_date`. |
| `customer_ltv` | 19,205 | One row per customer. PK and FK `customer_id`. |
| `monthly_category_sales` | 218 | PK `(month, category)`. |

### `analytics`: business marts

| Table | Rows | Grain and key |
|---|---:|---|
| `orders_enriched` | 119,226 | One row per order with cohort, first or repeat flag, last-touch `marketing_channel`, payment gateway, paid amount, failed attempts and Stripe fees. |
| `daily_revenue` | 3,527 | PK `(revenue_date, sales_channel)`. |
| `customer_ltv` | 19,205 | Adds `segment` (prospect, new, returning, vip) and `acquisition_channel`. |
| `category_revenue` | 10 | PK `category`. Includes share of net sales. |
| `product_sales` | 500 | One row per product, unsold products included. `units_refunded` comes from the refund line-item JSON. |
| `marketing_roas` | 6,570 | PK `(date, channel)`: spend, impressions, clicks, sessions, attributed orders and revenue, `roas`, `cost_per_order`. |

Tables and the main columns have `COMMENT`s, so Metabase shows them as descriptions.

## Business rules

- **Order date:** shop-local, `(created_at AT TIME ZONE 'America/New_York')::date`.
- **Money:** USD shop currency, `numeric(12,2)`, in Shopify and every transformed table. Stripe amounts are integer cents.
- **Real order:** the latest version per id, excluding `test` orders and `_fivetran_deleted` orders.
- **`net_revenue`** in `stg_shopify_orders` is `total_price - total_tax`, which equals subtotal after discounts plus shipping. It is 0 for cancelled, voided or fully refunded orders. Partial refunds are not netted out. They are in `refunded_amount`, and `analytics.daily_revenue.net_revenue_after_refunds` subtracts them.
- **Valid order:** `is_valid_order = NOT (cancelled OR financial_status IN ('voided','refunded'))`. Pending and authorized orders count as valid.
- **Customer merge:** accounts are grouped by `lower(btrim(email))`, and the earliest account becomes `customer_id`. Guest checkouts attach to a customer by email. Staff accounts (tag `staff`) and deleted accounts are excluded.
- **`sales_channel`** comes from `source_name`: `web` is online_store, `iphone`/`android` is mobile_app, `pos` is pos, `shopify_draft_order` is draft_order, and a numeric app id is shop_app.
- **`marketing_channel`:** UTM values are normalized case-insensitively. Untagged sessions fall back to the referrer (organic_search, organic_social, direct). Attribution is the last converting session. An online order with no session is `untracked`, and POS, draft and app orders are `offline`.
- **Ad spend:** keep the latest `_loaded_at` per `(date, campaign_id)`.
- **VIP:** lifetime net revenue of at least 2,500, or the Shopify tag `vip`.

### Segments and measures

Metabase defines these. They are not database objects:

| Name | Kind | Table | Definition |
|---|---|---|---|
| `valid_orders` | segment | `shop_analytics.stg_shopify_orders` | `is_valid_order = true` |
| `repeat_customers` | segment | `shop_analytics.customer_ltv` | `order_count >= 2` (equivalently, `is_repeat_customer`) |
| `units_sold` | measure | `shop_analytics.stg_shopify_order_lines` | `sum(quantity)` |
| `vip_customers` | segment | `analytics.customer_ltv` | `segment = 'vip'` |
| `fulfilled_orders` | segment | `shop_analytics.stg_shopify_orders` | `fulfillment_status = 'fulfilled'` |
| `refunded_orders` | segment | `shop_analytics.stg_shopify_orders` | `financial_status IN ('refunded','partially_refunded')` |
| `web_orders` | segment | `shop_analytics.stg_shopify_orders` | `sales_channel = 'online_store'` |
| `net_revenue` | measure | `shop_analytics.stg_shopify_orders` | `sum(net_revenue)` |
| `payment_total` | measure | `analytics.orders_enriched` | `sum(paid_amount)` |

## Data shape

- **Volume:** 119,226 real orders (114,304 valid), 291,083 order lines, 19,205 customers and 500 products.
- **Growth:** FY25 (Oct 2024 to Sep 2025) has 43,952 valid orders and $11.89M net revenue. FY26 has 70,352 orders and $19.35M, which is +60% in orders.
- **Seasonality:**
  - Black Friday through Cyber Monday is the top of the year. 2025-11-28 had 891 valid orders and 2025-12-01 had 895, against a mean of about 157 per day.
  - December gift buying runs until the shipping cutoff, then sales drop. January dips.
  - The mid-July sale and the week before Mother's Day are smaller peaks.
  - Sunday and Monday are the strongest days of the week.
- **Category seasons:** Outerwear peaks in Q4 and Camp & Home peaks in Q2 and Q3.
- **Customers and cohorts:** 45% of customers who ordered bought once. 55% are repeat customers, with a heavy-tailed VIP group. Monthly cohorts decay, and older cohorts have higher order counts and LTV.
- **Order outcomes:** about 2% of orders are cancelled (voided before capture or refunded), 2% are fully refunded returns, and 3% are partially refunded.
- **Order-to-customer ratio:** the requested volumes (about 120k orders for about 20k customers) give about 6 orders per customer. That is more than typical apparel retail. The excess is in the repeat and VIP tail.

## Validation (last build)

All 37 checks pass. The full output, including the sanity queries, is in `validation.txt`. The checks cover:

- **Raw to staging:** stg order count equals latest raw orders minus test and deleted orders. Valid net revenue recomputed directly from raw with the rule matches staging (31,244,097.07).
- **Oracle:** order count, valid orders, valid net revenue, line count, units sold (348,363), customer count and Black Friday 2025 orders all equal `data/expected.json`, which the generator computes without SQL.
- **Marts:**
  - `sum(daily_sales.net_revenue)` and `sum(analytics.daily_revenue.net_revenue)` equal valid net revenue.
  - `daily_sales` order and unit totals match staging, and `monthly_category_sales` unit totals match too.
  - Category net sales equal the valid orders' subtotal.
  - Customer LTV sums equal the valid net revenue of identified customers, in both schemas.
  - `product_sales` units match staging.
  - Marketing spend equals the deduplicated raw spend, and attributed orders equal valid orders that have a session.
- **Payments:**
  - Stripe captured charges equal Shopify Payments captures and sales.
  - Stripe refunds equal Shopify Payments refunds.
  - Payouts equal captures minus fees minus refunds.
- **Orphans:** 0 for stg lines to orders, stg lines to products, stg orders to customers, raw lines to orders, variants to products, transactions and refunds to orders, Stripe refunds to charges, charges to payouts, and converted sessions to orders. The transformed tables also enforce these as FK constraints.
