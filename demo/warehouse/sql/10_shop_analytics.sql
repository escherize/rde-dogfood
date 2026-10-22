-- shop_analytics: cleaned staging tables over raw_shopify plus the reporting marts the
-- team built first. Run as one session: the temp table is shared by later statements.
-- Business dates are shop-local (America/New_York).

\set ON_ERROR_STOP on
DROP SCHEMA IF EXISTS shop_analytics CASCADE;
CREATE SCHEMA shop_analytics;
COMMENT ON SCHEMA shop_analytics IS 'Cleaned Shopify staging tables (stg_*) and reporting marts. Shop-local dates (America/New_York), money in USD.';

-- Latest landed version of every Shopify account.
CREATE TEMP TABLE latest_accounts AS
SELECT DISTINCT ON (id) *
FROM raw_shopify.customers
ORDER BY id, updated_at DESC, _fivetran_synced DESC;

-- One person can hold several accounts whose emails differ only by case or whitespace.
-- The earliest account is the canonical customer; redacted (GDPR) accounts have no email
-- and stand alone.
CREATE TEMP TABLE customer_accounts AS
SELECT id AS account_id,
       first_value(id) OVER (PARTITION BY person_key ORDER BY created_at, id) AS customer_id,
       person_key
FROM (
  SELECT *, COALESCE(lower(btrim(email)), 'account:' || id) AS person_key
  FROM latest_accounts
  WHERE NOT _fivetran_deleted
    AND COALESCE(tags, '') NOT ILIKE '%staff%'
) a;

-- ---------------------------------------------------------------------------- stg_shopify_customers
CREATE TABLE shop_analytics.stg_shopify_customers AS
SELECT c.id                                        AS customer_id,
       lower(btrim(c.email))                       AS email,
       c.first_name,
       c.last_name,
       c.default_address ->> 'country_code'        AS country_code,
       c.default_address ->> 'province_code'       AS province_code,
       c.default_address ->> 'city'                AS city,
       c.accepts_marketing,
       c.currency                                  AS preferred_currency,
       COALESCE(c.tags, '') ILIKE '%vip%'          AS is_vip_tagged,
       c.email IS NULL                             AS is_redacted,
       acc.account_count,
       c.created_at
FROM latest_accounts c
JOIN (SELECT customer_id, count(*)::int AS account_count FROM customer_accounts GROUP BY customer_id) acc
  ON acc.customer_id = c.id;
ALTER TABLE shop_analytics.stg_shopify_customers ADD PRIMARY KEY (customer_id);
COMMENT ON TABLE shop_analytics.stg_shopify_customers IS 'One row per person. Duplicate Shopify accounts (same email, different casing) are merged into the earliest account; staff and deleted accounts are excluded.';
COMMENT ON COLUMN shop_analytics.stg_shopify_customers.account_count IS 'Number of Shopify accounts merged into this customer.';

-- ---------------------------------------------------------------------------- stg_shopify_products
CREATE TABLE shop_analytics.stg_shopify_products AS
SELECT p.id                    AS product_id,
       p.title,
       p.handle,
       p.vendor,
       p.product_type          AS category,
       p.status,
       p._fivetran_deleted     AS is_deleted,
       p.created_at,
       p.published_at,
       v.variant_count,
       v.min_price,
       v.max_price
FROM raw_shopify.products p
LEFT JOIN (
  SELECT product_id, count(*)::int AS variant_count, min(price) AS min_price, max(price) AS max_price
  FROM raw_shopify.product_variants
  GROUP BY product_id
) v ON v.product_id = p.id;
ALTER TABLE shop_analytics.stg_shopify_products ADD PRIMARY KEY (product_id);
COMMENT ON TABLE shop_analytics.stg_shopify_products IS 'Product catalog, including archived and deleted products so historical order lines still resolve.';

-- ---------------------------------------------------------------------------- stg_shopify_orders
CREATE TEMP TABLE latest_orders AS
SELECT DISTINCT ON (id) *
FROM raw_shopify.orders
ORDER BY id, updated_at DESC, _fivetran_synced DESC;

CREATE TABLE shop_analytics.stg_shopify_orders AS
WITH refunds AS (
  SELECT order_id, sum(amount) AS refunded_amount
  FROM raw_shopify.transactions
  WHERE kind = 'refund' AND status = 'success'
  GROUP BY order_id
), lines AS (
  SELECT order_id, count(*)::int AS line_count, sum(quantity)::int AS units
  FROM raw_shopify.order_lines
  GROUP BY order_id
), base AS (
  SELECT o.*,
         COALESCE(ca.customer_id, ge.customer_id) AS canonical_customer_id
  FROM latest_orders o
  LEFT JOIN customer_accounts ca ON ca.account_id = o.customer_id
  LEFT JOIN customer_accounts ge ON o.customer_id IS NULL AND ge.person_key = lower(btrim(o.email))
                                AND ge.account_id = ge.customer_id
  WHERE NOT o.test
    AND NOT o._fivetran_deleted
)
SELECT b.id                                                     AS order_id,
       b.name                                                   AS order_name,
       b.order_number,
       b.canonical_customer_id                                  AS customer_id,
       b.customer_id                                            AS shopify_customer_id,
       lower(btrim(b.email))                                    AS customer_email,
       b.customer_id IS NULL                                    AS is_guest_checkout,
       b.created_at                                             AS ordered_at,
       (b.created_at AT TIME ZONE 'America/New_York')::date     AS order_date,
       b.source_name,
       CASE
         WHEN b.source_name = 'web' THEN 'online_store'
         WHEN b.source_name IN ('iphone', 'android') THEN 'mobile_app'
         WHEN b.source_name = 'pos' THEN 'pos'
         WHEN b.source_name = 'shopify_draft_order' THEN 'draft_order'
         WHEN b.source_name ~ '^[0-9]+$' THEN 'shop_app'
         ELSE 'other'
       END                                                      AS sales_channel,
       b.financial_status,
       b.fulfillment_status,
       b.cancelled_at IS NOT NULL                               AS is_cancelled,
       b.cancelled_at,
       b.cancel_reason,
       b.currency,
       b.presentment_currency,
       (b.total_price_set -> 'presentment_money' ->> 'amount')::numeric(12,2) AS presentment_total_price,
       b.total_line_items_price                                 AS gross_sales,
       b.total_discounts,
       b.subtotal_price,
       (b.total_shipping_price_set -> 'shop_money' ->> 'amount')::numeric(12,2) AS shipping_price,
       b.total_tax,
       b.total_price,
       COALESCE(r.refunded_amount, 0)::numeric(12,2)            AS refunded_amount,
       CASE
         WHEN b.cancelled_at IS NOT NULL OR b.financial_status IN ('voided', 'refunded') THEN 0
         ELSE b.total_price - b.total_tax
       END::numeric(12,2)                                       AS net_revenue,
       NOT (b.cancelled_at IS NOT NULL OR b.financial_status IN ('voided', 'refunded')) AS is_valid_order,
       b.discount_codes -> 0 ->> 'code'                         AS discount_code,
       COALESCE(b.shipping_address ->> 'country_code', b.billing_address ->> 'country_code') AS shipping_country,
       b.shipping_address ->> 'province_code'                   AS shipping_province,
       COALESCE(l.line_count, 0)                                AS line_count,
       COALESCE(l.units, 0)                                     AS units
FROM base b
LEFT JOIN refunds r ON r.order_id = b.id
LEFT JOIN lines l ON l.order_id = b.id;
ALTER TABLE shop_analytics.stg_shopify_orders ADD PRIMARY KEY (order_id);
ALTER TABLE shop_analytics.stg_shopify_orders
  ADD FOREIGN KEY (customer_id) REFERENCES shop_analytics.stg_shopify_customers (customer_id);
CREATE INDEX ON shop_analytics.stg_shopify_orders (order_date);
CREATE INDEX ON shop_analytics.stg_shopify_orders (customer_id);
COMMENT ON TABLE shop_analytics.stg_shopify_orders IS 'One row per real order: latest landed version, test orders and deleted orders removed, customer resolved to the merged customer (guest checkouts matched by email).';
COMMENT ON COLUMN shop_analytics.stg_shopify_orders.net_revenue IS 'total_price - total_tax; 0 for cancelled, voided or fully refunded orders. Partial refunds are not netted out (see refunded_amount).';
COMMENT ON COLUMN shop_analytics.stg_shopify_orders.is_valid_order IS 'False for cancelled, voided or fully refunded orders. The valid_orders segment.';
COMMENT ON COLUMN shop_analytics.stg_shopify_orders.order_date IS 'Order date in the shop timezone (America/New_York).';

-- ---------------------------------------------------------------------------- stg_shopify_order_lines
CREATE TABLE shop_analytics.stg_shopify_order_lines AS
SELECT l.id                                              AS order_line_id,
       l.order_id,
       l.index                                           AS line_number,
       p.product_id,
       l.product_id                                      AS source_product_id,
       l.variant_id,
       l.title                                           AS product_title,
       l.variant_title,
       l.sku,
       COALESCE(p.category, CASE WHEN l.product_id IS NULL THEN 'Add-ons' ELSE 'Unknown' END) AS category,
       l.quantity,
       l.price                                           AS unit_price,
       (l.price * l.quantity)::numeric(12,2)             AS gross_amount,
       l.total_discount                                  AS discount_amount,
       (l.price * l.quantity - l.total_discount)::numeric(12,2) AS net_amount,
       l.gift_card                                       AS is_gift_card,
       o.ordered_at,
       o.order_date,
       o.is_valid_order
FROM raw_shopify.order_lines l
JOIN shop_analytics.stg_shopify_orders o ON o.order_id = l.order_id
LEFT JOIN shop_analytics.stg_shopify_products p ON p.product_id = l.product_id;
ALTER TABLE shop_analytics.stg_shopify_order_lines ADD PRIMARY KEY (order_line_id);
ALTER TABLE shop_analytics.stg_shopify_order_lines
  ADD FOREIGN KEY (order_id) REFERENCES shop_analytics.stg_shopify_orders (order_id),
  ADD FOREIGN KEY (product_id) REFERENCES shop_analytics.stg_shopify_products (product_id);
CREATE INDEX ON shop_analytics.stg_shopify_order_lines (order_id);
CREATE INDEX ON shop_analytics.stg_shopify_order_lines (product_id);
COMMENT ON TABLE shop_analytics.stg_shopify_order_lines IS 'Line items of stg_shopify_orders. product_id is null for custom items (gift wrap) and for products deleted before the connector''s first sync (source_product_id keeps the raw id).';
COMMENT ON COLUMN shop_analytics.stg_shopify_order_lines.quantity IS 'Units on the line. units_sold measure = sum(quantity).';

-- ---------------------------------------------------------------------------- daily_sales
CREATE TABLE shop_analytics.daily_sales AS
WITH days AS (
  SELECT d::date AS order_date
  FROM generate_series((SELECT min(order_date) FROM shop_analytics.stg_shopify_orders),
                       (SELECT max(order_date) FROM shop_analytics.stg_shopify_orders), interval '1 day') d
), firsts AS (
  SELECT customer_id, min(ordered_at) AS first_ordered_at
  FROM shop_analytics.stg_shopify_orders
  WHERE is_valid_order AND customer_id IS NOT NULL
  GROUP BY customer_id
)
SELECT d.order_date,
       count(o.order_id) FILTER (WHERE o.is_valid_order)                       AS orders,
       count(o.order_id) FILTER (WHERE NOT o.is_valid_order)                   AS cancelled_or_refunded_orders,
       count(DISTINCT o.customer_id) FILTER (WHERE o.is_valid_order)           AS customers,
       count(o.order_id) FILTER (WHERE o.is_valid_order AND o.ordered_at = f.first_ordered_at) AS new_customer_orders,
       COALESCE(sum(o.units) FILTER (WHERE o.is_valid_order), 0)               AS units_sold,
       COALESCE(sum(o.gross_sales) FILTER (WHERE o.is_valid_order), 0)::numeric(14,2)     AS gross_sales,
       COALESCE(sum(o.total_discounts) FILTER (WHERE o.is_valid_order), 0)::numeric(14,2) AS discounts,
       COALESCE(sum(o.shipping_price) FILTER (WHERE o.is_valid_order), 0)::numeric(14,2)  AS shipping,
       COALESCE(sum(o.total_tax) FILTER (WHERE o.is_valid_order), 0)::numeric(14,2)       AS tax,
       COALESCE(sum(o.net_revenue), 0)::numeric(14,2)                          AS net_revenue,
       round(COALESCE(sum(o.net_revenue), 0) / NULLIF(count(o.order_id) FILTER (WHERE o.is_valid_order), 0), 2) AS avg_order_value
FROM days d
LEFT JOIN shop_analytics.stg_shopify_orders o ON o.order_date = d.order_date
LEFT JOIN firsts f ON f.customer_id = o.customer_id
GROUP BY d.order_date;
ALTER TABLE shop_analytics.daily_sales ADD PRIMARY KEY (order_date);
COMMENT ON TABLE shop_analytics.daily_sales IS 'One row per shop-local day. Counts and money cover valid orders only; sum(net_revenue) equals the valid orders'' net revenue.';

-- ---------------------------------------------------------------------------- customer_ltv
CREATE TABLE shop_analytics.customer_ltv AS
SELECT c.customer_id,
       c.email,
       c.first_name,
       c.last_name,
       c.country_code,
       date_trunc('month', min(o.order_date))::date                    AS cohort_month,
       min(o.ordered_at)                                               AS first_order_at,
       max(o.ordered_at)                                               AS last_order_at,
       count(o.order_id)                                               AS order_count,
       COALESCE(sum(o.net_revenue), 0)::numeric(14,2)                  AS lifetime_net_revenue,
       round(sum(o.net_revenue) / NULLIF(count(o.order_id), 0), 2)     AS avg_order_value,
       count(o.order_id) >= 2                                          AS is_repeat_customer
FROM shop_analytics.stg_shopify_customers c
LEFT JOIN shop_analytics.stg_shopify_orders o ON o.customer_id = c.customer_id AND o.is_valid_order
GROUP BY c.customer_id, c.email, c.first_name, c.last_name, c.country_code;
ALTER TABLE shop_analytics.customer_ltv ADD PRIMARY KEY (customer_id);
ALTER TABLE shop_analytics.customer_ltv
  ADD FOREIGN KEY (customer_id) REFERENCES shop_analytics.stg_shopify_customers (customer_id);
COMMENT ON TABLE shop_analytics.customer_ltv IS 'One row per customer (including those with no valid orders). The repeat_customers segment is order_count >= 2.';

-- ---------------------------------------------------------------------------- monthly_category_sales
CREATE TABLE shop_analytics.monthly_category_sales AS
SELECT date_trunc('month', l.order_date)::date        AS month,
       l.category,
       count(DISTINCT l.order_id)                     AS orders,
       sum(l.quantity)                                AS units_sold,
       sum(l.gross_amount)::numeric(14,2)             AS gross_sales,
       sum(l.discount_amount)::numeric(14,2)          AS discounts,
       sum(l.net_amount)::numeric(14,2)               AS net_sales
FROM shop_analytics.stg_shopify_order_lines l
WHERE l.is_valid_order
GROUP BY 1, 2;
ALTER TABLE shop_analytics.monthly_category_sales ADD PRIMARY KEY (month, category);
COMMENT ON TABLE shop_analytics.monthly_category_sales IS 'Line-level sales of valid orders by month and product category (pre-tax, pre-shipping).';
