-- Raw landing tables, shaped the way an ingestion connector lands them: no primary keys or
-- foreign keys (the connector appends), connector bookkeeping columns, JSON payload columns,
-- timestamps with time zones. raw_shopify.orders and raw_shopify.customers can hold more than
-- one row per id: an append-mode sync lands every version it saw, so readers keep the row
-- with the latest updated_at.

DROP SCHEMA IF EXISTS raw_shopify, raw_stripe, raw_marketing, shop_analytics, analytics CASCADE;
CREATE SCHEMA raw_shopify;
CREATE SCHEMA raw_stripe;
CREATE SCHEMA raw_marketing;

COMMENT ON SCHEMA raw_shopify IS 'Shopify Admin API landed by the ingestion connector (append mode; _fivetran_* bookkeeping).';
COMMENT ON SCHEMA raw_stripe IS 'Stripe (Shopify Payments processor) landed by the ingestion connector. Amounts in integer cents.';
COMMENT ON SCHEMA raw_marketing IS 'Ad-platform spend exports and first-party web analytics sessions.';

CREATE TABLE raw_shopify.customers (
  id                bigint,
  email             text,
  first_name        text,
  last_name         text,
  phone             text,
  state             text,
  verified_email    boolean,
  accepts_marketing boolean,
  currency          text,
  orders_count      integer,
  total_spent       numeric(12,2),
  tags              text,
  default_address   jsonb,
  created_at        timestamptz,
  updated_at        timestamptz,
  _fivetran_synced  timestamptz,
  _fivetran_deleted boolean
);

CREATE TABLE raw_shopify.products (
  id                bigint,
  title             text,
  handle            text,
  vendor            text,
  product_type      text,
  status            text,
  tags              text,
  published_at      timestamptz,
  created_at        timestamptz,
  updated_at        timestamptz,
  _fivetran_synced  timestamptz,
  _fivetran_deleted boolean
);

CREATE TABLE raw_shopify.product_variants (
  id                 bigint,
  product_id         bigint,
  title              text,
  sku                text,
  price              numeric(12,2),
  compare_at_price   numeric(12,2),
  option1            text,
  option2            text,
  inventory_quantity integer,
  grams              integer,
  requires_shipping  boolean,
  created_at         timestamptz,
  updated_at         timestamptz,
  _fivetran_synced   timestamptz,
  _fivetran_deleted  boolean
);

CREATE TABLE raw_shopify.discounts (
  id                bigint,
  code              text,
  title             text,
  value_type        text,
  value             numeric(12,2),
  target_type       text,
  once_per_customer boolean,
  usage_count       integer,
  starts_at         timestamptz,
  ends_at           timestamptz,
  created_at        timestamptz,
  updated_at        timestamptz,
  _fivetran_synced  timestamptz
);

CREATE TABLE raw_shopify.orders (
  id                       bigint,
  name                     text,
  order_number             integer,
  customer_id              bigint,
  email                    text,
  created_at               timestamptz,
  processed_at             timestamptz,
  updated_at               timestamptz,
  cancelled_at             timestamptz,
  cancel_reason            text,
  closed_at                timestamptz,
  currency                 text,
  presentment_currency     text,
  total_line_items_price   numeric(12,2),
  subtotal_price           numeric(12,2),
  total_discounts          numeric(12,2),
  total_tax                numeric(12,2),
  total_price              numeric(12,2),
  total_price_set          jsonb,
  total_shipping_price_set jsonb,
  financial_status         text,
  fulfillment_status       text,
  source_name              text,
  test                     boolean,
  landing_site             text,
  referring_site           text,
  tags                     text,
  note_attributes          jsonb,
  shipping_address         jsonb,
  billing_address          jsonb,
  discount_codes           jsonb,
  _fivetran_synced         timestamptz,
  _fivetran_deleted        boolean
);

CREATE TABLE raw_shopify.order_lines (
  id                 bigint,
  order_id           bigint,
  index              integer,
  product_id         bigint,
  variant_id         bigint,
  title              text,
  variant_title      text,
  sku                text,
  vendor             text,
  quantity           integer,
  price              numeric(12,2),
  total_discount     numeric(12,2),
  taxable            boolean,
  gift_card          boolean,
  requires_shipping  boolean,
  fulfillment_status text,
  properties         jsonb,
  _fivetran_synced   timestamptz
);

CREATE TABLE raw_shopify.refunds (
  id                bigint,
  order_id          bigint,
  created_at        timestamptz,
  processed_at      timestamptz,
  note              text,
  restock           boolean,
  refund_line_items jsonb,
  _fivetran_synced  timestamptz
);

CREATE TABLE raw_shopify.transactions (
  id               bigint,
  order_id         bigint,
  refund_id        bigint,
  parent_id        bigint,
  kind             text,
  status           text,
  gateway          text,
  amount           numeric(12,2),
  currency         text,
  "authorization"  text,
  error_code       text,
  test             boolean,
  created_at       timestamptz,
  processed_at     timestamptz,
  receipt          jsonb,
  _fivetran_synced timestamptz
);

CREATE TABLE raw_stripe.charges (
  id                     text,
  amount                 bigint,
  amount_captured        bigint,
  amount_refunded        bigint,
  currency               text,
  status                 text,
  paid                   boolean,
  captured               boolean,
  refunded               boolean,
  customer_id            text,
  description            text,
  failure_code           text,
  failure_message        text,
  fee                    bigint,
  payout_id              text,
  metadata               jsonb,
  payment_method_details jsonb,
  created                timestamptz,
  captured_at            timestamptz,
  _fivetran_synced       timestamptz
);

CREATE TABLE raw_stripe.refunds (
  id               text,
  charge_id        text,
  amount           bigint,
  currency         text,
  reason           text,
  status           text,
  payout_id        text,
  metadata         jsonb,
  created          timestamptz,
  _fivetran_synced timestamptz
);

CREATE TABLE raw_stripe.payouts (
  id               text,
  amount           bigint,
  currency         text,
  arrival_date     date,
  created          timestamptz,
  status           text,
  method           text,
  type             text,
  description      text,
  _fivetran_synced timestamptz
);

CREATE TABLE raw_marketing.ad_spend (
  date                      date,
  channel                   text,
  account_id                text,
  campaign_id               text,
  campaign_name             text,
  impressions               bigint,
  clicks                    bigint,
  spend                     numeric(12,2),
  currency                  text,
  platform_conversions      numeric(12,2),
  platform_conversion_value numeric(14,2),
  _loaded_at                timestamptz
);

CREATE TABLE raw_marketing.web_sessions (
  session_id         uuid,
  anonymous_id       uuid,
  customer_id        bigint,
  started_at         timestamptz,
  duration_seconds   integer,
  landing_page       text,
  referrer           text,
  utm_source         text,
  utm_medium         text,
  utm_campaign       text,
  device_type        text,
  country_code       text,
  pageviews          integer,
  converted_order_id bigint,
  _loaded_at         timestamptz
);
