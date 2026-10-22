# MB Code demo: sessions in workspaces of your own Metabase

MB Code runs each AI session in an isolated workspace that the customer's own Metabase (the "parent") creates. When a session starts, the parent makes a schema `mb__isolation_*` and a database role in the warehouse for it. The session's Metabase and agent read the real tables, but every table they write lands in that schema, under the name the agent asked for. Ending the session drops the schema and the role. This guide walks a presenter and an audience through it on a synthetic Shopify-style shop ("Northbound Supply Co."): load the warehouse, set up the parent, connect MB Code, run a session, prove the real data never changed, and end the session.

| Step | What you do | Time |
|---|---|---|
| [1](#step-1-load-the-warehouse) | Load the warehouse | 3 to 5 min |
| [2](#step-2-set-up-the-parent-metabase) | Start and set up the parent Metabase | 5 min (plus a build the first time) |
| [3](#step-3-start-mb-code-and-connect-the-parent) | Start MB Code and connect the parent | 10 min |
| [4](#step-4-run-a-session) | Run a session with scripted prompts | 15 min |
| [5](#step-5-prove-isolation) | Prove isolation in `psql` | 5 min |
| [6](#step-6-end-the-session) | End the session and show the cleanup | 2 min |

## Prerequisites

- **Docker** (Docker Desktop, colima, or Engine 28 or later) with at least 8 GB of memory. MB Code runs each session as two containers.
- **Bun**, **git**, **python3**, and **psql** (any version).
- **The parent Metabase build**: metabase/metabase at commit `ec859565ee5` (branch `bcm/headless-workspaces`, metabase#85175). No release has the workspace manager API yet. Use a checkout at that commit, or an EE jar or image built from it ([Step 2](#step-2-set-up-the-parent-metabase)).
- **An EE license that includes workspaces.** The parent checks it on Metabase's production token-check server. MB Code's own setup wizard also asks for a license, which session Metabases check on the staging server.
- **A Postgres warehouse that you are admin on.** A local `postgres:16` container works. A managed Postgres (RDS, Lightsail, Cloud SQL) works if the admin login can create roles.
- **A GitHub repository** for the sessions' work, and a fine-grained personal access token for it with these repository permissions: Contents (read and write), Pull requests (read and write), Metadata (read). The repository can start empty, or as a fork of this one.
- **An Anthropic API key.**

Clone this repository and run every command below from its root.

## Step 1: Load the warehouse

`demo/warehouse/load.sh` loads the dataset, about 2.5M rows in 26 tables, and creates the login the parent uses. [`demo/warehouse/README.md`](warehouse/README.md) describes the schemas, the business rules and the deliberate messiness in the raw data.

1. Get a Postgres database and an admin connection URL for it. For a local one:

   ```sh
   docker run -d --name demo-warehouse -e POSTGRES_PASSWORD=change-me -e POSTGRES_DB=warehouse \
     -p 127.0.0.1:5442:5432 postgres:16
   ```

2. Run the loader with that URL:

   ```sh
   WAREHOUSE_ADMIN_URL=postgres://postgres:change-me@localhost:5442/warehouse demo/warehouse/load.sh
   ```

The loader does this, and you can run it again safely:

- It builds `demo/warehouse/shop_warehouse.dump` the first time, in a throwaway `postgres:16` container (about 2 minutes). Later runs reuse the dump.
- It restores the dump unless the 26 tables are there already. It uses `pg_restore` from a `postgres:<server major>` container when the local `pg_restore` has another major version: `pg_restore` 17 sends `transaction_timeout`, which a 16 server rejects.
- It creates the login `metabase_parent` with a generated password, `CREATEROLE`, and `CREATE` on the database. A later run keeps the password. The parent needs these to make each session's schema and role.
- It makes `metabase_parent` the owner of the five data schemas and their tables. See [MBC10-73 and MBC10-77](#troubleshooting-and-known-issues) for why.
- It writes the parent's connection URL to `demo/.secrets/warehouse.url`, and the same login as the psql service `demo_parent` to `demo/.secrets/pg_service.conf`. Both are mode 600, and `demo/.secrets/` is in `.gitignore`. It never prints the URL.
- A later run keeps the password in the URL file. To rotate it, delete the file and run again, then give the parent the new password in its Admin -> Databases.

## Step 2: Set up the parent Metabase

Start the parent yourself, then run the setup script against it. The script never starts Metabase.

### Run the parent

Pick one way. Both need the license in `MB_PREMIUM_EMBEDDING_TOKEN`, and the Site URL must be the URL you give MB Code in step 3.

**From source**, in a metabase/metabase checkout at `ec859565ee5`:

```sh
unset MB_CONFIG_FILE_PATH   # a mage dev config seeds users and skips the setup wizard
export MB_EDITION=ee MB_PREMIUM_EMBEDDING_TOKEN=<license>
export MB_JETTY_PORT=3050 MB_SITE_URL=http://localhost:3050
clj -M:dev:ee:ee-dev:drivers:drivers-dev:dev-start
```

In a second terminal, in the same checkout, start the frontend. The admin approves MB Code in the parent's browser UI, so the parent needs it. Run `bun install` once first.

```sh
MB_EDITION=ee bun build-hot
```

Without `MB_DB_*` settings the app DB is an H2 file in the checkout. Wait until `curl localhost:3050/api/health` answers `{"status":"ok"}`, about a minute.

**From an EE jar** built at `ec859565ee5`. `scripts/build-workspace-metabase.sh` in metabase/rde-control-plane builds it, and `./metabase-code --stubs` caches it as `~/.cache/mbc-parent-metabase/metabase-ec859565ee5303999abde5c9884194dbfc749ebb.jar`. The build needs the `:python` step (SQLGlot), or workspace remapping fails closed.

```sh
env -u MB_CONFIG_FILE_PATH MB_PREMIUM_EMBEDDING_TOKEN=<license> \
  MB_JETTY_PORT=3050 MB_SITE_URL=http://localhost:3050 MB_DB_FILE=$PWD/parent-app-db \
  java -jar metabase-ec859565ee5303999abde5c9884194dbfc749ebb.jar
```

If the parent runs in a container, it can't reach a warehouse at `localhost`. Change the host in `demo/.secrets/warehouse.url` to `host.docker.internal` before you run the setup script.

### Run the setup script

```sh
MB_URL=http://localhost:3050 bun demo/parent/setup.ts
```

It talks to the parent's API only, and you can run it again safely. It:

1. Finishes the setup wizard with the setup token and creates the admin, or signs the admin in if the parent is set up already.
2. Fails unless the license includes workspaces.
3. Creates a non-admin user, for the refusal in step 3.
4. Adds the warehouse "Shop warehouse" from `demo/.secrets/warehouse.url`, and waits for the first sync.
5. Turns on workspaces for it (the database setting `database-enable-workspaces`).
6. Checks that the OAuth server metadata answers, and warns when the OAuth issuer isn't `MB_URL` or MCP is off.
7. Turns on Git sync, read-only on `main` with transforms, if you set `GIT_SYNC_URL` and `GIT_SYNC_TOKEN`. The token goes in the request body, never on a command line.
8. Prints a summary: the URL, both logins, the database id and the table count.

| Variable | Default |
|---|---|
| `MB_URL` | required |
| `MB_ADMIN_EMAIL` / `MB_ADMIN_PASSWORD` | `admin@parent.test` / `parent-admin-1` |
| `MB_MEMBER_EMAIL` / `MB_MEMBER_PASSWORD` | `analyst@parent.test` / `parent-analyst-1` |
| `WAREHOUSE_URL_FILE` | `demo/.secrets/warehouse.url` |
| `WAREHOUSE_NAME` | `Shop warehouse` |
| `GIT_SYNC_URL`, `GIT_SYNC_TOKEN` | unset: no Git sync |

To turn on Git sync without typing the token on a command line, read it into the environment first:

```sh
read -rs GIT_SYNC_TOKEN && export GIT_SYNC_TOKEN
MB_URL=http://localhost:3050 GIT_SYNC_URL=https://github.com/<you>/<repo>.git bun demo/parent/setup.ts
```

Expect 26 tables in the summary. Each failed step exits non-zero with the cause.

## Step 3: Start MB Code and connect the parent

1. Clone metabase/rde-control-plane and check out the branch `mbc/workspaces-local` (PR #167).
2. Run `./metabase-code`, without `--stubs` ([MBC10-75](#troubleshooting-and-known-issues)). The first start builds the session images, which takes several minutes. It prints MB Code's URL, `http://localhost:5678` by default, and opens it.
3. Walk the setup wizard:

| Wizard step | Enter | Show the audience |
|---|---|---|
| **Account** | Your name, email, password, and a team name. | The first account is the deployment's admin. |
| **Metabase** | The parent's URL, `http://localhost:3050`. Click **Connect**. | MB Code checks the Site URL and the OAuth server, then sends you to the parent. |
| (on the parent) | Sign in as the **non-admin**, `analyst@parent.test`, and click **Authorize**. | MB Code refuses: only an admin may create workspaces. It tells you to sign out of the parent first. |
| (on the parent) | Sign out at `http://localhost:3050`. Click **Connect** again, sign in as `admin@parent.test`, and click **Authorize**. | The step completes. MB Code stores only a refresh token. |
| **Metabase license** | Paste the license. | MB Code doesn't take it from the parent ([MBC10-79](#troubleshooting-and-known-issues)). |
| **Git repository** | The repository URL and the fine-grained PAT. | |
| **Database** | Skip it. | The parent's warehouses take its place. |
| **AI provider** | The Anthropic API key. | |

## Step 4: Run a session

1. Go to **Sessions -> New session**. The session reaches **Running** in about a minute.
2. Show the parent's side. Signed in to the parent as the admin, open `http://localhost:3050/api/ee/workspace-manager/`. It lists a workspace `mbc-<handle>-<nonce>`, and its `output_namespace` is the session's schema `mb__isolation_<x>_<id>`.
3. Give the agent these prompts in order.

**Prompt 1: explore.**

> Which schemas and tables does the warehouse have? How many valid orders, and how much net revenue, did fiscal 2026 (October 2025 to September 2026) have?

Show: the agent finds the five schemas and uses `shop_analytics.stg_shopify_orders`. Expect 70,352 valid orders and $19,354,073.90 net revenue.

**Prompt 2: build and run a transform.**

> Build a transform that writes daily web-store sales (valid online_store orders: orders, units, net revenue, average order value) to shop_analytics.web_daily_sales, and run it.

Show: the transform's SQL, and the run. Expect 730 rows with total net revenue 23,625,209.51. The SQL should be close to this:

```sql
SELECT order_date, count(*) AS orders, sum(units) AS units_sold,
       sum(net_revenue) AS net_revenue, round(avg(total_price), 2) AS avg_order_value
FROM shop_analytics.stg_shopify_orders
WHERE is_valid_order AND sales_channel = 'online_store'
GROUP BY order_date
```

**Prompt 3: read the result by its canonical name.**

> Show me the five days with the highest web net revenue from shop_analytics.web_daily_sales.

Show: the session reads `shop_analytics.web_daily_sales` as if it existed. The top days are Cyber Monday 2025-12-01 (207,062.42) and Black Friday 2025-11-28 (197,016.25). [Step 5](#step-5-prove-isolation) shows that the real `shop_analytics` has no such table.

**Prompt 4: a second transform from the raw layer.**

> Recompute category revenue from the raw Shopify product types, joined to shop_analytics.stg_shopify_order_lines, into analytics.category_revenue_raw. Run it and compare it with analytics.category_revenue.

Show: the agent joins the raw `raw_shopify.products.product_type` and handles lines with no product (gift wrap, deleted products). Expect the 8 product types plus rows for lines without a product, with total net sales 30,933,840.12, the same as `analytics.category_revenue`.

Then show the Git side: the session's branch in the GitHub repository holds the transforms' YAML.

Use new table names, as these prompts do. A transform that targets an existing table fails with 409 ([MBC10-78](#troubleshooting-and-known-issues)).

## Step 5: Prove isolation

Run these while the session is running. They connect as the parent's warehouse login through the psql service from step 1, so no password reaches the command line.

```sh
export PGSERVICEFILE=$PWD/demo/.secrets/pg_service.conf PGSERVICE=demo_parent

# The session's schema and role
psql -Atc "select 'schema', nspname from pg_namespace where nspname like 'mb\_\_%'
                union all select 'role', rolname from pg_roles where rolname like 'mb\_\_%'"

# The session's tables, with their rows
psql -Atc "select table_schema, table_name from information_schema.tables where table_schema like 'mb\_\_%'"
psql -Atc "select count(*), sum(net_revenue) from <mb__isolation schema>.shop_analytics__web_daily_sales"

# The canonical name doesn't exist in the real schema: expect 0
psql -Atc "select count(*) from information_schema.tables
                where table_schema = 'shop_analytics' and table_name = 'web_daily_sales'"

# The real marts are unchanged
psql -Atc "select count(*), md5(string_agg(x::text, '|' order by x::text)) from analytics.category_revenue x"
psql -Atc "select count(*), md5(string_agg(x::text, '|' order by x::text)) from shop_analytics.daily_sales x"
```

| Check | Expect |
|---|---|
| Session schema and role | One `mb__isolation_<x>_<id>` of each |
| `shop_analytics__web_daily_sales` in the session schema | 730 rows, net revenue 23,625,209.51 |
| `shop_analytics.web_daily_sales` in the real schema | 0 tables |
| `analytics.category_revenue` | 10 rows, md5 `0e5d24e3674dfa5699ccb98848a64425` |
| `shop_analytics.daily_sales` | 730 rows, md5 `6f4f66b20034120da0e14e716b425692` |

Run the two md5 queries before step 4 too, so the audience sees the same values before and after. Prompt 3 is the other half of the proof: inside the session, the canonical name reads the workspace's rows.

## Step 6: End the session

1. In MB Code, open the session and click **End session**.
2. Show that the parent deleted its workspace: `http://localhost:3050/api/ee/workspace-manager/` no longer lists the `mbc-` name.
3. Run the first query of step 5 again. It returns no rows: the schema and the role are gone.

If End session fails, the session page shows what the parent said. See MBC10-77 below.

## Troubleshooting and known issues

| Issue | Symptom | What to do |
|---|---|---|
| **MBC10-73**: the parent's warehouse login must own the data | Session start fails with "Current user is not a member of N role(s) owning objects in schema ...". | Make the parent's login the owner of the input schemas and tables, or a member of their owner role, with `CREATEROLE` and `CREATE` on the database. `load.sh` does this. |
| **MBC10-77**: teardown fails when the login isn't the owner | End session fails with "role ... cannot be dropped because some objects depend on it". It happens when another role owns the tables and the parent's login holds grant options on them. | Make the parent's login the owner (as `load.sh` does), then revoke what the leftover `mb__isolation_*` role holds on the input schemas, running as the parent's login. MB Code retries the delete. |
| **MBC10-78**: transforms can't target an existing table | 409, "A table with that name already exists." The parent checks the canonical target, not the remapped one. | Target new table names, for example `shop_analytics.web_daily_sales`. They still land in the session's schema. |
| **MBC10-75**: `--stubs` and real GitHub don't mix | `./metabase-code --stubs` points the GitHub API at a local git stub, so the Git step refuses a real PAT with "GitHub rejected this token". `--stubs` also starts its own dev parent on port 3340. | For this demo, run `./metabase-code` without `--stubs`, with a real Anthropic key. |
| **MBC10-79**: the license is pasted by hand | The wizard's **Metabase license** step needs the key even though the parent has one. Without it no workspace can start. | Paste it. |
| `MB_CONFIG_FILE_PATH` leaks into a from-source parent | A from-source parent skips the setup wizard and has seeded users (for example `crowberto@metabase.com`). The setup script then can't sign in as its admin. | `unset MB_CONFIG_FILE_PATH` before you start the parent, and start from a fresh app DB. |
| OAuth registration needs MCP | Connect fails at client registration. The setup script warns "MCP is off on the parent". | Turn MCP on in the parent's Admin settings. Dynamic client registration is on only while MCP is. |
| OAuth issuer mismatch | The setup script warns that the issuer isn't `MB_URL`, and MB Code refuses to connect. | Set `MB_SITE_URL` (or the Site URL in Admin) to exactly the URL you give MB Code. |
| Disconnecting the parent | After Admin -> Metabase -> Disconnect, new sessions run without a workspace, so nothing isolates them. | Reconnect before you start a session. |
| Misleading failure banner | A parent refusal shows "The session's containers couldn't be started", though no container was involved. | Read the parent's message below the banner. |
| A from-source parent reports version `vLOCAL_DEV` | It doesn't report its commit. | Check it with `git rev-parse HEAD` in the checkout. |
| Public repository | Each session pushes a branch with its transforms' YAML. | Use a private repository for real data, and delete session branches after the demo. |

`collections/` at the root of this repository holds the Library and Analytics collections that every session's Git sync expects. With them on `main`, a session's pull request shows only the session's own changes.
