/**
 * Sets up a running parent Metabase for the MB Code demo, through its API.
 * Safe to run again: each step checks what is there first.
 *
 *   MB_URL=http://localhost:3050 bun demo/parent/setup.ts
 *
 * Steps: finish the setup wizard (or sign in), create a non-admin user, add
 * the warehouse from the URL file load.sh wrote, wait for its sync, turn on
 * workspaces for it, check the OAuth server, optionally turn on Git sync,
 * then print a summary. It never starts Metabase and never prints a secret.
 *
 * Environment:
 *   MB_URL                 the parent's URL, which must equal its Site URL (required)
 *   MB_ADMIN_EMAIL         default admin@parent.test
 *   MB_ADMIN_PASSWORD      default parent-admin-1
 *   MB_MEMBER_EMAIL        default analyst@parent.test
 *   MB_MEMBER_PASSWORD     default parent-analyst-1
 *   WAREHOUSE_URL_FILE     default demo/.secrets/warehouse.url (from load.sh)
 *   WAREHOUSE_NAME         default "Shop warehouse"
 *   GIT_SYNC_URL           optional, e.g. https://github.com/you/repo.git
 *   GIT_SYNC_TOKEN         a PAT for GIT_SYNC_URL; Git sync is set up only when both are set
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

const HEALTH_TIMEOUT_MS = 60_000;
const SYNC_TIMEOUT_MS = 10 * 60_000;
const DEFAULT_URL_FILE = join(import.meta.dir, "..", ".secrets", "warehouse.url");

class SetupError extends Error {}

function fail(message: string): never {
  throw new SetupError(message);
}

type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
type JsonObject = { [key: string]: Json };

function asObject(value: Json | undefined, what: string): JsonObject {
  if (value === null || value === undefined || typeof value !== "object" || Array.isArray(value)) {
    fail(`expected an object for ${what}`);
  }
  return value;
}

function asArray(value: Json | undefined, what: string): Json[] {
  if (!Array.isArray(value)) {
    fail(`expected a list for ${what}`);
  }
  return value;
}

function env(name: string, fallback?: string): string {
  const value = process.env[name];
  if (value !== undefined && value !== "") {
    return value;
  }
  if (fallback === undefined) {
    fail(`set ${name}`);
  }
  return fallback;
}

interface Login {
  email: string;
  password: string;
}

interface WarehouseDetails {
  host: string;
  port: number;
  dbname: string;
  user: string;
  password: string;
  ssl: boolean;
}

/** Reads the URL file without echoing it: errors name the file, never its content. */
function readWarehouse(file: string): WarehouseDetails {
  let text: string;
  try {
    text = readFileSync(file, "utf8").trim();
  } catch {
    fail(`can't read the warehouse URL file ${file}; run demo/warehouse/load.sh first`);
  }
  let url: URL;
  try {
    url = new URL(text);
  } catch {
    fail(`${file} doesn't hold a postgres:// URL`);
  }
  const dbname = decodeURIComponent(url.pathname.replace(/^\//, ""));
  if (!["postgres:", "postgresql:"].includes(url.protocol) || url.hostname === "" || dbname === "") {
    fail(`${file} doesn't hold a postgres://user:password@host:port/database URL`);
  }
  const sslmode = url.searchParams.get("sslmode");
  return {
    host: url.hostname.replace(/^\[(.*)\]$/, "$1"),
    port: url.port === "" ? 5432 : Number(url.port),
    dbname,
    user: decodeURIComponent(url.username),
    password: decodeURIComponent(url.password),
    ssl: sslmode !== null && !["disable", "allow", "prefer"].includes(sslmode),
  };
}

class Parent {
  readonly url: string;
  private session: string | null = null;
  /** Values no error message may carry, in case the parent echoes a request. */
  private readonly secrets: string[];

  constructor(url: string, secrets: string[]) {
    this.url = url.replace(/\/+$/, "");
    this.secrets = secrets.filter((secret) => secret.length >= 4);
  }

  private redact(text: string): string {
    return this.secrets.reduce((result, secret) => result.split(secret).join("<redacted>"), text);
  }

  async request(method: string, path: string, body?: Json): Promise<Response> {
    const headers: Record<string, string> = { "content-type": "application/json" };
    if (this.session !== null) {
      headers["x-metabase-session"] = this.session;
    }
    try {
      return await fetch(`${this.url}${path}`, {
        method,
        headers,
        body: body === undefined ? null : JSON.stringify(body),
        signal: AbortSignal.timeout(120_000),
      });
    } catch (error) {
      fail(`${method} ${path} didn't answer: ${error instanceof Error ? error.message : String(error)}`);
    }
  }

  /** The JSON body, or a failure naming the call and the parent's message. */
  async json(method: string, path: string, body?: Json): Promise<Json> {
    const response = await this.request(method, path, body);
    const text = await response.text();
    if (!response.ok) {
      fail(`${method} ${path} -> ${String(response.status)}: ${this.redact(text).slice(0, 400)}`);
    }
    if (text === "") {
      return null;
    }
    try {
      return JSON.parse(text) as Json;
    } catch {
      fail(`${method} ${path} answered something other than JSON: ${this.redact(text).slice(0, 200)}`);
    }
  }

  async waitHealthy(): Promise<void> {
    const deadline = Date.now() + HEALTH_TIMEOUT_MS;
    while (Date.now() < deadline) {
      try {
        const response = await fetch(`${this.url}/api/health`, { signal: AbortSignal.timeout(3000) });
        await response.body?.cancel();
        if (response.ok) {
          return;
        }
      } catch {
        // not up yet
      }
      await Bun.sleep(2000);
    }
    fail(`${this.url}/api/health didn't answer within ${String(HEALTH_TIMEOUT_MS / 1000)} s; start the parent first`);
  }

  async properties(): Promise<JsonObject> {
    return asObject(await this.json("GET", "/api/session/properties"), "session properties");
  }

  /** Finishes the setup wizard with the admin, or signs the admin in. */
  async signInAdmin(admin: Login): Promise<"created" | "signed in"> {
    const properties = await this.properties();
    if (properties["has-user-setup"] !== true) {
      const token = properties["setup-token"];
      if (typeof token !== "string") {
        fail("the parent has no admin yet but gave no setup token");
      }
      const created = asObject(
        await this.json("POST", "/api/setup", {
          token,
          user: {
            email: admin.email,
            password: admin.password,
            first_name: "Parent",
            last_name: "Admin",
          },
          prefs: { site_name: "MB Code demo parent", allow_tracking: false },
        }),
        "setup response",
      );
      this.session = String(created["id"]);
      return "created";
    }
    const response = await this.request("POST", "/api/session", {
      username: admin.email,
      password: admin.password,
    });
    if (!response.ok) {
      fail(
        `the parent is already set up and refused ${admin.email} (${String(response.status)}); set MB_ADMIN_EMAIL and MB_ADMIN_PASSWORD to its admin`,
      );
    }
    this.session = String(asObject((await response.json()) as Json, "session")["id"]);
    const me = asObject(await this.json("GET", "/api/user/current"), "current user");
    if (me["is_superuser"] !== true) {
      fail(`${admin.email} is not an admin on the parent`);
    }
    return "signed in";
  }

  async ensureMember(member: Login): Promise<"created" | "exists"> {
    const users = asArray(asObject(await this.json("GET", "/api/user?status=all"), "users")["data"], "users");
    const existing = users
      .map((user) => asObject(user, "user"))
      .find((user) => String(user["email"]).toLowerCase() === member.email.toLowerCase());
    if (existing !== undefined) {
      if (existing["is_superuser"] === true) {
        fail(`${member.email} exists and is an admin; the demo needs a non-admin`);
      }
      if (existing["is_active"] === false) {
        fail(`${member.email} exists but is deactivated; reactivate it in the parent's Admin -> People`);
      }
      return "exists";
    }
    await this.json("POST", "/api/user", {
      email: member.email,
      password: member.password,
      first_name: "Parent",
      last_name: "Analyst",
    });
    return "created";
  }

  async findDatabase(name: string): Promise<JsonObject | undefined> {
    const databases = asArray(asObject(await this.json("GET", "/api/database"), "databases")["data"], "databases");
    return databases.map((database) => asObject(database, "database")).find((database) => database["name"] === name);
  }

  async ensureDatabase(name: string, details: WarehouseDetails): Promise<{ id: number; added: boolean }> {
    const existing = await this.findDatabase(name);
    if (existing !== undefined) {
      return { id: Number(existing["id"]), added: false };
    }
    const created = asObject(
      await this.json("POST", "/api/database", {
        engine: "postgres",
        name,
        details: { ...details },
      }),
      "new database",
    );
    return { id: Number(created["id"]), added: true };
  }

  /** Waits for the first sync, then returns the number of tables it found. */
  async waitForSync(id: number): Promise<number> {
    const deadline = Date.now() + SYNC_TIMEOUT_MS;
    while (Date.now() < deadline) {
      const metadata = asObject(
        await this.json("GET", `/api/database/${String(id)}/metadata?include_hidden=true`),
        "database metadata",
      );
      const tables = asArray(metadata["tables"], "tables");
      if (metadata["initial_sync_status"] === "complete" && tables.length > 0) {
        return tables.length;
      }
      if (metadata["initial_sync_status"] === "aborted") {
        fail(`the parent's sync of database ${String(id)} aborted; check the parent's log`);
      }
      await Bun.sleep(3000);
    }
    fail(`the parent didn't sync database ${String(id)} within ${String(SYNC_TIMEOUT_MS / 60_000)} minutes`);
  }

  async enableWorkspaces(id: number): Promise<"enabled" | "already on"> {
    const path = `/api/database/${String(id)}`;
    const settingOn = async (): Promise<boolean> => {
      const settings = asObject(await this.json("GET", path), "database")["settings"];
      return settings !== null && settings !== undefined && asObject(settings, "settings")["database-enable-workspaces"] === true;
    };
    if (await settingOn()) {
      return "already on";
    }
    await this.json("PUT", path, { settings: { "database-enable-workspaces": true } });
    if (!(await settingOn())) {
      fail(`the parent didn't turn on workspaces for database ${String(id)}`);
    }
    return "enabled";
  }

  /** Throws when the OAuth server can't serve MB Code; returns warnings otherwise. */
  async checkOAuth(): Promise<string[]> {
    const response = await this.request("GET", "/.well-known/oauth-authorization-server");
    if (response.status !== 200) {
      fail(`the OAuth server metadata answered ${String(response.status)}; MB Code can't connect`);
    }
    const metadata = asObject((await response.json()) as Json, "OAuth metadata");
    const warnings: string[] = [];
    if (metadata["issuer"] !== this.url) {
      warnings.push(
        `the OAuth issuer is ${String(metadata["issuer"])}, not ${this.url}: set the Site URL (MB_SITE_URL) to the URL you give MB Code`,
      );
    }
    const methods = metadata["code_challenge_methods_supported"];
    if (!Array.isArray(methods) || !methods.includes("S256")) {
      warnings.push("the OAuth server doesn't advertise PKCE S256");
    }
    const properties = await this.properties();
    // Dynamic client registration is on only while MCP is; its own setting
    // isn't readable over the API, so MCP is the check.
    if (properties["mcp-enabled?"] !== true) {
      warnings.push(
        "MCP is off on the parent, which also turns off OAuth dynamic client registration: MB Code can't register. Turn MCP on in Admin -> AI.",
      );
    }
    return warnings;
  }

  async workspacesLicensed(): Promise<boolean> {
    const features = (await this.properties())["token-features"];
    return features !== null && features !== undefined && asObject(features, "token features")["workspaces"] === true;
  }

  /** Read-only Git sync on main. The token goes in a request body only. */
  async configureGitSync(url: string, token: string): Promise<void> {
    // The demo repo holds a custom collection (collections/main/analytics.yaml),
    // which an import refuses unless this is on.
    await this.json("PUT", "/api/setting/remote-sync-custom-collections-enabled", { value: true });
    await this.json("PUT", "/api/ee/remote-sync/settings", {
      "remote-sync-url": url,
      "remote-sync-token": token,
      "remote-sync-type": "read-only",
      "remote-sync-branch": "main",
      "remote-sync-transforms": true,
    });
  }
}

async function main(): Promise<void> {
  const admin: Login = {
    email: env("MB_ADMIN_EMAIL", "admin@parent.test"),
    password: env("MB_ADMIN_PASSWORD", "parent-admin-1"),
  };
  const member: Login = {
    email: env("MB_MEMBER_EMAIL", "analyst@parent.test"),
    password: env("MB_MEMBER_PASSWORD", "parent-analyst-1"),
  };
  const warehouseName = env("WAREHOUSE_NAME", "Shop warehouse");
  const warehouse = readWarehouse(env("WAREHOUSE_URL_FILE", DEFAULT_URL_FILE));
  const gitUrl = process.env["GIT_SYNC_URL"] ?? "";
  const gitToken = process.env["GIT_SYNC_TOKEN"] ?? "";
  if ((gitUrl === "") !== (gitToken === "")) {
    fail("set both GIT_SYNC_URL and GIT_SYNC_TOKEN, or neither");
  }
  const parent = new Parent(env("MB_URL"), [warehouse.password, gitToken, admin.password, member.password]);

  await parent.waitHealthy();
  console.log(`admin ${admin.email}: ${await parent.signInAdmin(admin)}`);
  if (!(await parent.workspacesLicensed())) {
    fail("the parent's license has no workspaces feature; start it with MB_PREMIUM_EMBEDDING_TOKEN set to an EE license that includes workspaces");
  }
  console.log(`non-admin ${member.email}: ${await parent.ensureMember(member)}`);

  const { id, added } = await parent.ensureDatabase(warehouseName, warehouse);
  console.log(`warehouse "${warehouseName}" (id ${String(id)}): ${added ? "added" : "exists"}; waiting for its sync`);
  const tables = await parent.waitForSync(id);
  console.log(`warehouse synced: ${String(tables)} tables`);
  console.log(`workspaces: ${await parent.enableWorkspaces(id)}`);

  const warnings = await parent.checkOAuth();
  for (const warning of warnings) {
    console.warn(`warning: ${warning}`);
  }
  console.log(`OAuth server metadata: ok${warnings.length > 0 ? " (see the warnings)" : ""}`);

  let gitSync = "left as it is (GIT_SYNC_URL unset)";
  if (gitUrl !== "") {
    await parent.configureGitSync(gitUrl, gitToken);
    gitSync = `${gitUrl}, read-only, branch main`;
  }

  console.log(
    [
      "",
      "Parent Metabase is ready for MB Code",
      `  URL:        ${parent.url}`,
      `  admin:      ${admin.email}`,
      `  non-admin:  ${member.email}`,
      `  warehouse:  "${warehouseName}", database id ${String(id)}, ${String(tables)} tables, workspaces on`,
      `  Git sync:   ${gitSync}`,
    ].join("\n"),
  );
}

try {
  await main();
} catch (error) {
  if (error instanceof SetupError) {
    console.error(`setup failed: ${error.message}`);
  } else {
    console.error(`setup failed unexpectedly: ${error instanceof Error ? error.message : String(error)}`);
  }
  process.exit(1);
}
