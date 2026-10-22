-- analytics: business marts built on shop_analytics staging plus raw Stripe and marketing data.
-- Run as one session (temp tables).

\set ON_ERROR_STOP on
DROP SCHEMA IF EXISTS analytics CASCADE;
CREATE SCHEMA analytics;
COMMENT ON SCHEMA analytics IS 'Business marts: order facts, revenue, customer value, catalog performance, marketing return. Built from shop_analytics staging and raw Stripe/marketing data.';

-- Web sessions with a normalised marketing channel. UTM values arrive in mixed case and with
-- several spellings per platform; untagged sessions fall back to the referrer.
CREATE TEMP TABLE session_channels AS
SELECT session_id,
       converted_order_id,
       started_at,
       (started_at AT TIME ZONE 'America/New_York')::date AS session_date,
       CASE
         WHEN lower(utm_medium) = 'email' OR lower(utm_source) = 'klaviyo' THEN 'email'
         WHEN lower(utm_medium) = 'affiliate' THEN 'affiliate'
         WHEN lower(utm_source) IN ('google', 'adwords') THEN 'google_ads'
         WHEN lower(utm_source) IN ('facebook', 'fb', 'instagram', 'ig') THEN 'meta_ads'
         WHEN lower(utm_source) = 'tiktok' THEN 'tiktok_ads'
         WHEN lower(utm_source) = 'pinterest' THEN 'pinterest_ads'
         WHEN referrer ~ '(google|bing|duckduckgo)\.' THEN 'organic_search'
         WHEN referrer ~ '(instagram|reddit|youtube)\.' THEN 'organic_social'
         ELSE 'direct'
       END AS channel
FROM raw_marketing.web_sessions;

-- Last-touch attribution: the latest tracked session that converted into the order.
CREATE TEMP TABLE order_attribution AS
SELECT DISTINCT ON (converted_order_id) converted_order_id AS order_id, channel
FROM session_channels
WHERE converted_order_id IS NOT NULL
ORDER BY converted_order_id, started_at DESC;

-- ---------------------------------------------------------------------------- orders_enriched
CREATE TABLE analytics.orders_enriched AS
WITH payments AS (
  SELECT order_id,
         (array_agg(gateway ORDER BY created_at) FILTER (WHERE kind IN ('sale', 'capture', 'authorization') AND status = 'success'))[1] AS payment_gateway,
         COALESCE(sum(amount) FILTER (WHERE kind IN ('sale', 'capture') AND status = 'success'), 0) AS paid_amount,
         count(*) FILTER (WHERE status = 'failure') AS failed_payment_attempts
  FROM raw_shopify.transactions
  GROUP BY order_id
), stripe AS (
  SELECT (metadata ->> 'shopify_order_id')::bigint AS order_id,
         sum(fee) FILTER (WHERE captured) / 100.0 AS processing_fees
  FROM raw_stripe.charges
  WHERE metadata ? 'shopify_order_id'
  GROUP BY 1
), firsts AS (
  SELECT customer_id, min(ordered_at) AS first_ordered_at
  FROM shop_analytics.stg_shopify_orders
  WHERE is_valid_order AND customer_id IS NOT NULL
  GROUP BY customer_id
)
SELECT o.order_id,
       o.order_name,
       o.customer_id,
       o.ordered_at,
       o.order_date,
       date_trunc('month', f.first_ordered_at AT TIME ZONE 'America/New_York')::date AS customer_cohort_month,
       CASE
         WHEN o.customer_id IS NULL THEN 'guest'
         WHEN f.first_ordered_at IS NULL OR o.ordered_at <= f.first_ordered_at THEN 'first_order'
         ELSE 'repeat_order'
       END                                                   AS order_sequence,
       o.sales_channel,
       COALESCE(a.channel, CASE WHEN o.sales_channel IN ('online_store', 'mobile_app') THEN 'untracked' ELSE 'offline' END) AS marketing_channel,
       o.financial_status,
       o.fulfillment_status,
       o.is_valid_order,
       o.discount_code,
       o.shipping_country,
       o.presentment_currency,
       o.line_count,
       o.units,
       o.gross_sales,
       o.total_discounts,
       o.shipping_price,
       o.total_tax,
       o.total_price,
       o.net_revenue,
       o.refunded_amount,
       p.payment_gateway,
       COALESCE(p.paid_amount, 0)::numeric(12,2)             AS paid_amount,
       COALESCE(p.failed_payment_attempts, 0)                AS failed_payment_attempts,
       COALESCE(s.processing_fees, 0)::numeric(12,2)         AS processing_fees
FROM shop_analytics.stg_shopify_orders o
LEFT JOIN firsts f ON f.customer_id = o.customer_id
LEFT JOIN order_attribution a ON a.order_id = o.order_id
LEFT JOIN payments p ON p.order_id = o.order_id
LEFT JOIN stripe s ON s.order_id = o.order_id;
ALTER TABLE analytics.orders_enriched ADD PRIMARY KEY (order_id);
ALTER TABLE analytics.orders_enriched
  ADD FOREIGN KEY (order_id) REFERENCES shop_analytics.stg_shopify_orders (order_id),
  ADD FOREIGN KEY (customer_id) REFERENCES shop_analytics.stg_shopify_customers (customer_id);
CREATE INDEX ON analytics.orders_enriched (order_date);
CREATE INDEX ON analytics.orders_enriched (customer_id);
COMMENT ON TABLE analytics.orders_enriched IS 'Order-grain fact: every stg order with cohort, first/repeat flag, last-touch marketing channel, payment summary and Stripe processing fees.';
COMMENT ON COLUMN analytics.orders_enriched.marketing_channel IS 'Last-touch channel of the converting web session; untracked when an online order has no session, offline for POS/draft/app orders.';

-- ---------------------------------------------------------------------------- daily_revenue
CREATE TABLE analytics.daily_revenue AS
SELECT order_date                                                   AS revenue_date,
       sales_channel,
       count(*) FILTER (WHERE is_valid_order)                       AS orders,
       COALESCE(sum(gross_sales) FILTER (WHERE is_valid_order), 0)::numeric(14,2)     AS gross_sales,
       COALESCE(sum(total_discounts) FILTER (WHERE is_valid_order), 0)::numeric(14,2) AS discounts,
       sum(net_revenue)::numeric(14,2)                              AS net_revenue,
       COALESCE(sum(refunded_amount) FILTER (WHERE is_valid_order), 0)::numeric(14,2) AS partial_refunds,
       (sum(net_revenue) - COALESCE(sum(refunded_amount) FILTER (WHERE is_valid_order), 0))::numeric(14,2) AS net_revenue_after_refunds
FROM shop_analytics.stg_shopify_orders
GROUP BY 1, 2;
ALTER TABLE analytics.daily_revenue ADD PRIMARY KEY (revenue_date, sales_channel);
COMMENT ON TABLE analytics.daily_revenue IS 'Revenue by shop-local order date and sales channel. net_revenue follows the stg rule; net_revenue_after_refunds also subtracts partial refunds (refund amounts include refunded tax).';

-- ---------------------------------------------------------------------------- customer_ltv
CREATE TABLE analytics.customer_ltv AS
WITH acq AS (
  SELECT DISTINCT ON (customer_id) customer_id, marketing_channel AS acquisition_channel
  FROM analytics.orders_enriched
  WHERE customer_id IS NOT NULL AND is_valid_order
  ORDER BY customer_id, ordered_at
)
SELECT l.customer_id,
       l.email,
       l.country_code,
       l.cohort_month,
       l.first_order_at,
       l.last_order_at,
       l.order_count,
       l.lifetime_net_revenue,
       l.avg_order_value,
       a.acquisition_channel,
       CASE
         WHEN l.order_count = 0 THEN 'prospect'
         WHEN l.lifetime_net_revenue >= 2500 OR c.is_vip_tagged THEN 'vip'
         WHEN l.order_count >= 2 THEN 'returning'
         ELSE 'new'
       END                                                          AS segment,
       ('2026-09-30'::date - (l.last_order_at AT TIME ZONE 'America/New_York')::date) AS days_since_last_order,
       c.accepts_marketing
FROM shop_analytics.customer_ltv l
JOIN shop_analytics.stg_shopify_customers c ON c.customer_id = l.customer_id
LEFT JOIN acq a ON a.customer_id = l.customer_id;
ALTER TABLE analytics.customer_ltv ADD PRIMARY KEY (customer_id);
ALTER TABLE analytics.customer_ltv
  ADD FOREIGN KEY (customer_id) REFERENCES shop_analytics.stg_shopify_customers (customer_id);
COMMENT ON TABLE analytics.customer_ltv IS 'Customer value with segment (prospect/new/returning/vip) and acquisition channel. vip = lifetime net revenue >= 2500 or tagged vip in Shopify.';

-- ---------------------------------------------------------------------------- category_revenue
CREATE TABLE analytics.category_revenue AS
WITH cat AS (
  SELECT category,
         count(DISTINCT order_id)          AS orders,
         sum(quantity)                     AS units_sold,
         sum(gross_amount)                 AS gross_sales,
         sum(net_amount)                   AS net_sales
  FROM shop_analytics.stg_shopify_order_lines
  WHERE is_valid_order
  GROUP BY category
)
SELECT c.category,
       (SELECT count(*) FROM shop_analytics.stg_shopify_products p WHERE p.category = c.category) AS products,
       c.orders,
       c.units_sold,
       c.gross_sales::numeric(14,2)                                  AS gross_sales,
       c.net_sales::numeric(14,2)                                    AS net_sales,
       round(c.net_sales / NULLIF(c.units_sold, 0), 2)               AS avg_net_unit_price,
       round(100.0 * c.net_sales / sum(c.net_sales) OVER (), 2)      AS share_of_net_sales_pct
FROM cat c;
ALTER TABLE analytics.category_revenue ADD PRIMARY KEY (category);
COMMENT ON TABLE analytics.category_revenue IS 'All-time line-level sales of valid orders by product category.';

-- ---------------------------------------------------------------------------- product_sales
CREATE TABLE analytics.product_sales AS
WITH sold AS (
  SELECT product_id,
         count(DISTINCT order_id)  AS orders,
         sum(quantity)             AS units_sold,
         sum(net_amount)           AS net_sales,
         min(order_date)           AS first_sold_on,
         max(order_date)           AS last_sold_on
  FROM shop_analytics.stg_shopify_order_lines
  WHERE is_valid_order AND product_id IS NOT NULL
  GROUP BY product_id
), returned AS (
  SELECT l.product_id, sum((rli ->> 'quantity')::int) AS units_refunded
  FROM raw_shopify.refunds r
  CROSS JOIN LATERAL jsonb_array_elements(r.refund_line_items) rli
  JOIN shop_analytics.stg_shopify_order_lines l ON l.order_line_id = (rli ->> 'line_item_id')::bigint
  WHERE l.product_id IS NOT NULL
  GROUP BY l.product_id
)
SELECT p.product_id,
       p.title,
       p.category,
       p.vendor,
       p.status,
       p.is_deleted,
       COALESCE(s.orders, 0)                       AS orders,
       COALESCE(s.units_sold, 0)                   AS units_sold,
       COALESCE(s.net_sales, 0)::numeric(14,2)     AS net_sales,
       COALESCE(r.units_refunded, 0)               AS units_refunded,
       s.first_sold_on,
       s.last_sold_on,
       rank() OVER (ORDER BY COALESCE(s.net_sales, 0) DESC) AS net_sales_rank
FROM shop_analytics.stg_shopify_products p
LEFT JOIN sold s ON s.product_id = p.product_id
LEFT JOIN returned r ON r.product_id = p.product_id;
ALTER TABLE analytics.product_sales ADD PRIMARY KEY (product_id);
ALTER TABLE analytics.product_sales
  ADD FOREIGN KEY (product_id) REFERENCES shop_analytics.stg_shopify_products (product_id);
COMMENT ON TABLE analytics.product_sales IS 'One row per catalog product (unsold products included) with valid-order sales and refunded units from refund line items.';

-- ---------------------------------------------------------------------------- marketing_roas
CREATE TABLE analytics.marketing_roas AS
WITH spend AS (
  -- Platforms restate past days; keep the latest load per day and campaign.
  SELECT date, channel, sum(spend) AS spend, sum(impressions) AS impressions, sum(clicks) AS clicks
  FROM (
    SELECT DISTINCT ON (date, campaign_id) *
    FROM raw_marketing.ad_spend
    ORDER BY date, campaign_id, _loaded_at DESC
  ) latest
  GROUP BY date, channel
), sessions AS (
  SELECT session_date AS date, channel, count(*) AS sessions
  FROM session_channels
  GROUP BY 1, 2
), attributed AS (
  SELECT order_date AS date, marketing_channel AS channel,
         count(*) FILTER (WHERE is_valid_order) AS attributed_orders,
         sum(net_revenue)                       AS attributed_net_revenue
  FROM analytics.orders_enriched
  WHERE marketing_channel NOT IN ('untracked', 'offline')
  GROUP BY 1, 2
), keys AS (
  SELECT date, channel FROM spend
  UNION SELECT date, channel FROM sessions
  UNION SELECT date, channel FROM attributed
)
SELECT k.date,
       k.channel,
       k.channel IN ('google_ads', 'meta_ads', 'tiktok_ads', 'pinterest_ads') AS is_paid,
       COALESCE(sp.spend, 0)::numeric(14,2)                    AS spend,
       COALESCE(sp.impressions, 0)                             AS impressions,
       COALESCE(sp.clicks, 0)                                  AS clicks,
       COALESCE(se.sessions, 0)                                AS sessions,
       COALESCE(at.attributed_orders, 0)                       AS attributed_orders,
       COALESCE(at.attributed_net_revenue, 0)::numeric(14,2)   AS attributed_net_revenue,
       round(at.attributed_net_revenue / NULLIF(sp.spend, 0), 3) AS roas,
       round(sp.spend / NULLIF(at.attributed_orders, 0), 2)      AS cost_per_order
FROM keys k
LEFT JOIN spend sp ON sp.date = k.date AND sp.channel = k.channel
LEFT JOIN sessions se ON se.date = k.date AND se.channel = k.channel
LEFT JOIN attributed at ON at.date = k.date AND at.channel = k.channel;
ALTER TABLE analytics.marketing_roas ADD PRIMARY KEY (date, channel);
COMMENT ON TABLE analytics.marketing_roas IS 'Daily spend (deduplicated restatements), sessions and last-touch attributed valid orders per marketing channel. roas = attributed_net_revenue / spend; null for unpaid channels.';
