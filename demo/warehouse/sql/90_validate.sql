-- Validation: row counts, reconciliation against raw and against the generator's oracle
-- (data/expected.json), FK orphan checks and sanity queries. Every check row must end in
-- ok = true; the script errors out otherwise.
\set ON_ERROR_STOP on
\pset footer off

\echo '== Row counts'
SELECT table_schema || '.' || table_name AS "table",
       (xpath('/row/n/text()', query_to_xml(format('SELECT count(*) AS n FROM %I.%I', table_schema, table_name), false, true, '')))[1]::text::bigint AS row_count
FROM information_schema.tables
WHERE table_schema IN ('raw_shopify', 'raw_stripe', 'raw_marketing', 'shop_analytics', 'analytics')
ORDER BY 1;

\echo '== Reconciliation and oracle checks'
CREATE TEMP TABLE checks (check_name text, expected text, actual text, ok boolean);

-- Raw -> staging, recomputed independently from raw with the documented rule.
INSERT INTO checks
WITH latest AS (
  SELECT DISTINCT ON (id) * FROM raw_shopify.orders ORDER BY id, updated_at DESC, _fivetran_synced DESC
), real_orders AS (
  SELECT * FROM latest WHERE NOT test AND NOT _fivetran_deleted
)
SELECT 'stg_shopify_orders rows = latest raw orders minus test/deleted',
       (SELECT count(*) FROM real_orders)::text, (SELECT count(*) FROM shop_analytics.stg_shopify_orders)::text, NULL::boolean
UNION ALL
SELECT 'stg net revenue = raw (total_price - total_tax) of non-cancelled/voided/refunded orders',
       (SELECT sum(total_price - total_tax) FROM real_orders
         WHERE cancelled_at IS NULL AND financial_status NOT IN ('voided', 'refunded'))::text,
       (SELECT sum(net_revenue) FROM shop_analytics.stg_shopify_orders WHERE is_valid_order)::text, NULL;

-- Staging matches the generator's independent oracle.
INSERT INTO checks VALUES
 ('stg orders = oracle', :'exp_stg_orders', (SELECT count(*) FROM shop_analytics.stg_shopify_orders)::text, NULL),
 ('valid orders = oracle', :'exp_valid_orders', (SELECT count(*) FROM shop_analytics.stg_shopify_orders WHERE is_valid_order)::text, NULL),
 ('valid net revenue = oracle', :'exp_valid_net_revenue', (SELECT sum(net_revenue) FROM shop_analytics.stg_shopify_orders WHERE is_valid_order)::text, NULL),
 ('invalid orders carry zero net revenue', '0.00', (SELECT COALESCE(sum(net_revenue), 0) FROM shop_analytics.stg_shopify_orders WHERE NOT is_valid_order)::text, NULL),
 ('stg order lines = oracle', :'exp_stg_order_lines', (SELECT count(*) FROM shop_analytics.stg_shopify_order_lines)::text, NULL),
 ('units sold (valid) = oracle', :'exp_valid_units_sold', (SELECT sum(quantity) FROM shop_analytics.stg_shopify_order_lines WHERE is_valid_order)::text, NULL),
 ('stg customers (deduplicated people) = oracle', :'exp_stg_customers', (SELECT count(*) FROM shop_analytics.stg_shopify_customers)::text, NULL),
 ('Black Friday 2025 valid orders = oracle', :'exp_bf2025', (SELECT orders FROM shop_analytics.daily_sales WHERE order_date = '2025-11-28')::text, NULL);

-- Marts reconcile with staging.
INSERT INTO checks VALUES
 ('sum(daily_sales.net_revenue) = valid net revenue', :'exp_valid_net_revenue', (SELECT sum(net_revenue) FROM shop_analytics.daily_sales)::text, NULL),
 ('sum(daily_sales.orders) = valid orders', :'exp_valid_orders', (SELECT sum(orders) FROM shop_analytics.daily_sales)::text, NULL),
 ('sum(daily_sales.units_sold) = valid units', :'exp_valid_units_sold', (SELECT sum(units_sold) FROM shop_analytics.daily_sales)::text, NULL),
 ('sum(monthly_category_sales.units_sold) = valid units', :'exp_valid_units_sold', (SELECT sum(units_sold) FROM shop_analytics.monthly_category_sales)::text, NULL),
 ('sum(monthly_category_sales.net_sales) = valid subtotal (line items - discounts)',
   (SELECT sum(subtotal_price) FROM shop_analytics.stg_shopify_orders WHERE is_valid_order)::text,
   (SELECT sum(net_sales) FROM shop_analytics.monthly_category_sales)::text, NULL),
 ('sum(customer_ltv.lifetime_net_revenue) = valid net revenue of identified customers',
   (SELECT sum(net_revenue) FROM shop_analytics.stg_shopify_orders WHERE is_valid_order AND customer_id IS NOT NULL)::text,
   (SELECT sum(lifetime_net_revenue) FROM shop_analytics.customer_ltv)::text, NULL),
 ('customer_ltv rows = stg customers', :'exp_stg_customers', (SELECT count(*) FROM shop_analytics.customer_ltv)::text, NULL),
 ('sum(analytics.daily_revenue.net_revenue) = valid net revenue', :'exp_valid_net_revenue', (SELECT sum(net_revenue) FROM analytics.daily_revenue)::text, NULL),
 ('analytics.orders_enriched rows = stg orders', :'exp_stg_orders', (SELECT count(*) FROM analytics.orders_enriched)::text, NULL),
 ('sum(analytics.customer_ltv) = shop_analytics.customer_ltv',
   (SELECT sum(lifetime_net_revenue) FROM shop_analytics.customer_ltv)::text, (SELECT sum(lifetime_net_revenue) FROM analytics.customer_ltv)::text, NULL),
 ('sum(category_revenue.net_sales) = sum(monthly_category_sales.net_sales)',
   (SELECT sum(net_sales) FROM shop_analytics.monthly_category_sales)::text, (SELECT sum(net_sales) FROM analytics.category_revenue)::text, NULL),
 ('sum(product_sales.units_sold) = valid units on catalogued products',
   (SELECT sum(quantity) FROM shop_analytics.stg_shopify_order_lines WHERE is_valid_order AND product_id IS NOT NULL)::text,
   (SELECT sum(units_sold) FROM analytics.product_sales)::text, NULL),
 ('marketing_roas spend = deduplicated raw ad spend',
   (SELECT sum(spend) FROM (SELECT DISTINCT ON (date, campaign_id) spend FROM raw_marketing.ad_spend ORDER BY date, campaign_id, _loaded_at DESC) x)::text,
   (SELECT sum(spend) FROM analytics.marketing_roas)::text, NULL),
 ('marketing_roas attributed orders = valid orders with a tracked session',
   (SELECT count(*) FROM analytics.orders_enriched WHERE is_valid_order AND marketing_channel NOT IN ('untracked', 'offline'))::text,
   (SELECT sum(attributed_orders) FROM analytics.marketing_roas)::text, NULL);

-- Payments reconcile across Shopify and Stripe.
INSERT INTO checks VALUES
 ('Stripe captured charges (Shopify-linked) = Shopify Payments captures/sales',
   (SELECT sum(amount * 100)::bigint FROM raw_shopify.transactions WHERE gateway = 'shopify_payments' AND kind IN ('capture', 'sale') AND status = 'success')::text,
   (SELECT sum(amount_captured) FROM raw_stripe.charges WHERE captured AND metadata ? 'shopify_order_id')::text, NULL),
 ('Stripe refunds = Shopify Payments refund transactions',
   (SELECT sum(amount * 100)::bigint FROM raw_shopify.transactions WHERE gateway = 'shopify_payments' AND kind = 'refund' AND status = 'success')::text,
   (SELECT sum(amount) FROM raw_stripe.refunds)::text, NULL),
 ('Stripe payouts = captured charges - fees - refunds',
   ((SELECT sum(amount_captured - fee) FROM raw_stripe.charges WHERE captured) - (SELECT sum(amount) FROM raw_stripe.refunds))::text,
   (SELECT sum(amount) FROM raw_stripe.payouts)::text, NULL);

-- FK orphans where FKs are meant to hold (the transformed tables also enforce them as constraints).
INSERT INTO checks VALUES
 ('orphan stg order lines -> stg orders', '0', (SELECT count(*) FROM shop_analytics.stg_shopify_order_lines l LEFT JOIN shop_analytics.stg_shopify_orders o USING (order_id) WHERE o.order_id IS NULL)::text, NULL),
 ('orphan stg order lines -> stg products', '0', (SELECT count(*) FROM shop_analytics.stg_shopify_order_lines l LEFT JOIN shop_analytics.stg_shopify_products p USING (product_id) WHERE l.product_id IS NOT NULL AND p.product_id IS NULL)::text, NULL),
 ('orphan stg orders -> stg customers', '0', (SELECT count(*) FROM shop_analytics.stg_shopify_orders o LEFT JOIN shop_analytics.stg_shopify_customers c USING (customer_id) WHERE o.customer_id IS NOT NULL AND c.customer_id IS NULL)::text, NULL),
 ('orphan raw order lines -> raw orders', '0', (SELECT count(*) FROM raw_shopify.order_lines l WHERE NOT EXISTS (SELECT 1 FROM raw_shopify.orders o WHERE o.id = l.order_id))::text, NULL),
 ('orphan raw variants -> raw products', '0', (SELECT count(*) FROM raw_shopify.product_variants v WHERE NOT EXISTS (SELECT 1 FROM raw_shopify.products p WHERE p.id = v.product_id))::text, NULL),
 ('orphan raw transactions -> raw orders', '0', (SELECT count(*) FROM raw_shopify.transactions t WHERE NOT EXISTS (SELECT 1 FROM raw_shopify.orders o WHERE o.id = t.order_id))::text, NULL),
 ('orphan raw refunds -> raw orders', '0', (SELECT count(*) FROM raw_shopify.refunds r WHERE NOT EXISTS (SELECT 1 FROM raw_shopify.orders o WHERE o.id = r.order_id))::text, NULL),
 ('orphan stripe refunds -> charges', '0', (SELECT count(*) FROM raw_stripe.refunds r WHERE NOT EXISTS (SELECT 1 FROM raw_stripe.charges c WHERE c.id = r.charge_id))::text, NULL),
 ('orphan stripe charges -> payouts', '0', (SELECT count(*) FROM raw_stripe.charges c WHERE c.payout_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM raw_stripe.payouts p WHERE p.id = c.payout_id))::text, NULL),
 ('orphan converted sessions -> raw orders', '0', (SELECT count(*) FROM raw_marketing.web_sessions s WHERE s.converted_order_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM raw_shopify.orders o WHERE o.id = s.converted_order_id))::text, NULL);

UPDATE checks SET ok = (expected = actual);
SELECT check_name, expected, actual, ok FROM checks;
SELECT CASE WHEN bool_and(ok) THEN 'ALL CHECKS PASSED' ELSE 'CHECKS FAILED: ' || count(*) FILTER (WHERE NOT ok) END AS result FROM checks;
SELECT bool_and(ok) AS all_ok FROM checks \gset
\if :all_ok
\else
  \echo 'validation failed'
  SELECT 1 / 0;
\endif

\echo '== Intended raw messiness (expected non-zero)'
SELECT 'raw order rows that are stale versions' AS item, (SELECT count(*) - count(DISTINCT id) FROM raw_shopify.orders) AS n
UNION ALL SELECT 'raw customer rows that are stale versions', (SELECT count(*) - count(DISTINCT id) FROM raw_shopify.customers)
UNION ALL SELECT 'test orders', (SELECT count(DISTINCT id) FROM raw_shopify.orders WHERE test)
UNION ALL SELECT 'soft-deleted orders', (SELECT count(DISTINCT id) FROM raw_shopify.orders WHERE _fivetran_deleted)
UNION ALL SELECT 'soft-deleted customers', (SELECT count(DISTINCT id) FROM raw_shopify.customers WHERE _fivetran_deleted)
UNION ALL SELECT 'soft-deleted products', (SELECT count(*) FROM raw_shopify.products WHERE _fivetran_deleted)
UNION ALL SELECT 'customer accounts sharing an email modulo case/whitespace', (SELECT count(*) FROM (SELECT DISTINCT id, lower(btrim(email)) e FROM raw_shopify.customers WHERE email IS NOT NULL) x WHERE e IN (SELECT lower(btrim(email)) FROM raw_shopify.customers GROUP BY 1 HAVING count(DISTINCT id) > 1))
UNION ALL SELECT 'accounts with email stored in upper/mixed case or padded', (SELECT count(DISTINCT id) FROM raw_shopify.customers WHERE email <> lower(btrim(email)))
UNION ALL SELECT 'redacted (GDPR) customers', (SELECT count(DISTINCT id) FROM raw_shopify.customers WHERE tags LIKE '%redacted%')
UNION ALL SELECT 'guest-checkout orders (no customer_id)', (SELECT count(DISTINCT id) FROM raw_shopify.orders WHERE customer_id IS NULL)
UNION ALL SELECT 'non-USD presentment orders', (SELECT count(DISTINCT id) FROM raw_shopify.orders WHERE presentment_currency <> 'USD')
UNION ALL SELECT 'order lines pointing at products never landed', (SELECT count(*) FROM raw_shopify.order_lines l WHERE l.product_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM raw_shopify.products p WHERE p.id = l.product_id))
UNION ALL SELECT 'custom order lines (no product)', (SELECT count(*) FROM raw_shopify.order_lines WHERE product_id IS NULL)
UNION ALL SELECT 'order rows synced > 2 days after last update (late arriving)', (SELECT count(*) FROM raw_shopify.orders WHERE _fivetran_synced > updated_at + interval '2 days')
UNION ALL SELECT 'restated ad_spend rows', (SELECT count(*) - count(DISTINCT (date, campaign_id)) FROM raw_marketing.ad_spend)
UNION ALL SELECT 'Stripe charges with no Shopify order (wholesale)', (SELECT count(*) FROM raw_stripe.charges WHERE NOT metadata ? 'shopify_order_id')
UNION ALL SELECT 'distinct utm_source spellings', (SELECT count(DISTINCT utm_source) FROM raw_marketing.web_sessions);

\echo '== Sanity: growth, fiscal years Oct-Sep'
SELECT CASE WHEN order_date < '2025-10-01' THEN 'FY25 (2024-10..2025-09)' ELSE 'FY26 (2025-10..2026-09)' END AS period,
       sum(orders) AS orders, sum(net_revenue) AS net_revenue, round(sum(net_revenue) / sum(orders), 2) AS aov
FROM shop_analytics.daily_sales GROUP BY 1 ORDER BY 1;

\echo '== Sanity: top 8 days by valid orders (BFCM should dominate)'
SELECT order_date, to_char(order_date, 'Dy') AS dow, orders, net_revenue FROM shop_analytics.daily_sales ORDER BY orders DESC LIMIT 8;

\echo '== Sanity: day-of-week profile'
SELECT to_char(order_date, 'ID Dy') AS dow, round(avg(orders), 1) AS avg_orders
FROM shop_analytics.daily_sales GROUP BY 1 ORDER BY 1;

\echo '== Sanity: order status mix'
SELECT financial_status, is_cancelled, count(*) AS orders, sum(net_revenue) AS net_revenue
FROM shop_analytics.stg_shopify_orders GROUP BY 1, 2 ORDER BY 3 DESC;

\echo '== Sanity: customers'
SELECT count(*) AS customers,
       count(*) FILTER (WHERE order_count >= 1) AS with_orders,
       count(*) FILTER (WHERE is_repeat_customer) AS repeat_customers,
       round(100.0 * count(*) FILTER (WHERE is_repeat_customer) / NULLIF(count(*) FILTER (WHERE order_count >= 1), 0), 1) AS repeat_pct,
       round(avg(lifetime_net_revenue) FILTER (WHERE order_count >= 1), 2) AS avg_ltv
FROM shop_analytics.customer_ltv;
SELECT segment, count(*) AS customers, sum(lifetime_net_revenue) AS net_revenue FROM analytics.customer_ltv GROUP BY 1 ORDER BY 2 DESC;

\echo '== Sanity: cohorts (first-order quarter -> customers, avg orders, avg LTV)'
SELECT to_char(date_trunc('quarter', cohort_month), 'YYYY-"Q"Q') AS cohort, count(*) AS customers,
       round(avg(order_count), 2) AS avg_orders, round(avg(lifetime_net_revenue), 2) AS avg_ltv
FROM shop_analytics.customer_ltv WHERE cohort_month IS NOT NULL GROUP BY 1 ORDER BY 1;

\echo '== Sanity: category revenue'
SELECT category, products, orders, units_sold, net_sales, share_of_net_sales_pct FROM analytics.category_revenue ORDER BY net_sales DESC;

\echo '== Sanity: seasonality of Outerwear vs Camp & Home (units by calendar quarter)'
SELECT category, extract(quarter FROM month)::int AS q, sum(units_sold) AS units
FROM shop_analytics.monthly_category_sales WHERE category IN ('Outerwear', 'Camp & Home') GROUP BY 1, 2 ORDER BY 1, 2;

\echo '== Sanity: marketing ROAS by channel (paid)'
SELECT channel, sum(spend) AS spend, sum(attributed_orders) AS orders, sum(attributed_net_revenue) AS revenue,
       round(sum(attributed_net_revenue) / NULLIF(sum(spend), 0), 2) AS roas
FROM analytics.marketing_roas WHERE is_paid GROUP BY 1 ORDER BY 2 DESC;

\echo '== Sanity: sales channel mix'
SELECT sales_channel, sum(orders) AS orders, sum(net_revenue) AS net_revenue FROM analytics.daily_revenue GROUP BY 1 ORDER BY 2 DESC;
