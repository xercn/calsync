// calsync config page (standalone Worker + Cloudflare Access).
//
// Browser: / and /api/*; accepts only a Cloudflare Access JWT (email = ALLOWED_EMAIL), and rejects everything
// if it isn't configured (fail-closed).
// Server (calsync): /agent/*, Bearer key (the Worker stores only the SHA-256, secret AGENT_KEY_SHA256).
// The server connects outbound; no port is opened.
//
// Flow: page "Save and preview" → new revision (draft) → the server (polls every minute) computes the preview and
// sends it back → the user clicks "Confirm" → confirmed → the server re-runs the preview with the new settings and
// writes the config and syncs only if each deletion count is at most the page preview + 10 → applied
// (otherwise failed, nothing is touched).
import PAGE from "./page.html";   // wrangler treats .html as a text module by default

const JSON_HEADERS = { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" };
const LIMIT = { mirrors: 50, b_merge: 50, name: 100, url: 2048, inventory: 512 * 1024, preview: 256 * 1024 };
const INTERVALS = [5, 10, 15, 30];
const DEFAULT_INTERVAL = 10;
const GCAL_ID = /^[A-Za-z0-9._%+-]+@(group\.calendar\.google\.com|gmail\.com)$/;

class HttpErr extends Error {
  constructor(status, msg) { super(msg); this.status = status; }
}
const bad = (msg) => new HttpErr(400, msg);

const json = (obj, status = 200) => new Response(JSON.stringify(obj), { status, headers: JSON_HEADERS });
const now = () => Math.floor(Date.now() / 1000);

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    try {
      if (url.pathname === "/agent/healthz") return json({ ok: true, version: env.VERSION || "dev" });
      if (url.pathname.startsWith("/agent/")) {
        if (!(await agentAuth(req, env))) return json({ error: "unauthorized" }, 401);
        return await agent(req, env, url);
      }
      const who = await userAuth(req, env, url);
      if (!who) return new Response("Forbidden", { status: 403 });
      if (url.pathname === "/" && req.method === "GET") {
        return new Response(PAGE, {
          headers: {
            "content-type": "text/html; charset=utf-8",
            "cache-control": "no-store",
            "content-security-policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
            "x-content-type-options": "nosniff",
            "referrer-policy": "no-referrer",
          },
        });
      }
      if (url.pathname.startsWith("/api/")) {
        // Cross-site protection: writes must be JSON + a custom header (cross-site forms can't send these;
        // sending them triggers a CORS preflight, which we don't allow)
        if (req.method !== "GET" &&
            (req.headers.get("x-calsync") !== "1" || !(req.headers.get("content-type") || "").startsWith("application/json"))) {
          return json({ error: "bad request" }, 400);
        }
        return await api(req, env, url, who);
      }
      return new Response("Not found", { status: 404 });
    } catch (e) {
      if (e instanceof HttpErr) return json({ error: e.message }, e.status);
      console.log("error", (e && e.stack) || String(e));
      return json({ error: "internal error" }, 500);
    }
  },
};

// ---------------------------------------------------------------- authentication

async function sha256hex(s) {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function agentAuth(req, env) {
  const m = /^Bearer (\S{20,200})$/.exec(req.headers.get("authorization") || "");
  if (!m || !env.AGENT_KEY_SHA256) return false;
  const a = new TextEncoder().encode(await sha256hex(m[1]));
  const b = new TextEncoder().encode(env.AGENT_KEY_SHA256.trim().toLowerCase());
  return a.byteLength === b.byteLength && crypto.subtle.timingSafeEqual(a, b);
}

let certCache = { at: 0, keys: [] };

function b64urlBytes(s) {
  s = s.replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(s + "===".slice((s.length + 3) % 4));
  return Uint8Array.from(bin, (c) => c.charCodeAt(0));
}

async function accessKeys(team, force) {
  if (!force && Date.now() - certCache.at < 3600e3 && certCache.keys.length) return certCache.keys;
  const r = await fetch(`https://${team}/cdn-cgi/access/certs`);
  if (!r.ok) throw new Error(`access certs HTTP ${r.status}`);
  certCache = { at: Date.now(), keys: (await r.json()).keys || [] };
  return certCache.keys;
}

async function userAuth(req, env, url) {
  // For local wrangler dev testing (.dev.vars is not deployed; a production request's hostname can never be localhost)
  if (env.DEV_EMAIL && (url.hostname === "localhost" || url.hostname === "127.0.0.1")) return env.DEV_EMAIL;
  const team = env.ACCESS_TEAM, aud = env.ACCESS_AUD, allowed = (env.ALLOWED_EMAIL || "").toLowerCase();
  if (!team || !aud || !allowed) return null;
  const tok = req.headers.get("cf-access-jwt-assertion");
  if (!tok) return null;
  const parts = tok.split(".");
  if (parts.length !== 3) return null;
  let header, payload;
  try {
    header = JSON.parse(new TextDecoder().decode(b64urlBytes(parts[0])));
    payload = JSON.parse(new TextDecoder().decode(b64urlBytes(parts[1])));
  } catch { return null; }
  if (header.alg !== "RS256") return null;
  let jwk = (await accessKeys(team)).find((k) => k.kid === header.kid);
  if (!jwk) jwk = (await accessKeys(team, true)).find((k) => k.kid === header.kid);
  if (!jwk) return null;
  const key = await crypto.subtle.importKey("jwk", jwk, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
  const ok = await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, b64urlBytes(parts[2]),
    new TextEncoder().encode(`${parts[0]}.${parts[1]}`));
  if (!ok) return null;
  const t = now();
  const auds = Array.isArray(payload.aud) ? payload.aud : [payload.aud];
  if (!auds.includes(aud) || payload.iss !== `https://${team}` || !(payload.exp > t) || (payload.nbf && payload.nbf > t + 60)) return null;
  if ((payload.email || "").toLowerCase() !== allowed) return null;
  return payload.email;
}

// ---------------------------------------------------------------- subscription link encryption

async function subKey(env) {
  if (!env.SUB_KEY) throw new HttpErr(503, "SUB_KEY not configured");
  return crypto.subtle.importKey("raw", b64urlBytes(env.SUB_KEY.trim()), "AES-GCM", false, ["encrypt", "decrypt"]);
}
const b64 = (u8) => btoa(String.fromCharCode(...u8));

async function encryptUrl(env, id, url) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ct = await crypto.subtle.encrypt({ name: "AES-GCM", iv, additionalData: new TextEncoder().encode(id) },
    await subKey(env), new TextEncoder().encode(url));
  return { iv: b64(iv), ct: b64(new Uint8Array(ct)) };
}

async function decryptUrl(env, row) {
  const pt = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: b64urlBytes(row.iv), additionalData: new TextEncoder().encode(row.id) },
    await subKey(env), b64urlBytes(row.ct));
  return new TextDecoder().decode(pt);
}

// ---------------------------------------------------------------- D1 helpers

const first = (env, sql, ...a) => env.DB.prepare(sql).bind(...a).first();
const all = async (env, sql, ...a) => (await env.DB.prepare(sql).bind(...a).all()).results;
const run = (env, sql, ...a) => env.DB.prepare(sql).bind(...a).run();

async function getMeta(env, k) {
  const r = await first(env, "SELECT v, at FROM meta WHERE k = ?", k);
  return r ? { ...JSON.parse(r.v), _at: r.at } : null;
}
const putMeta = (env, k, v) =>
  run(env, "INSERT INTO meta (k, v, at) VALUES (?, ?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v, at = excluded.at",
    k, JSON.stringify(v), now());

const audit = (env, who, action, detail) =>
  run(env, "INSERT INTO audit (at, who, action, detail) VALUES (?, ?, ?, ?)", now(), who, action,
    detail === undefined ? null : JSON.stringify(detail));

async function readJson(req, max = 64 * 1024) {
  const text = await req.text();
  if (text.length > max) throw bad("body too large");
  try { return JSON.parse(text); } catch { throw bad("invalid JSON"); }
}

const parseRev = (r) => r && { ...r, body: JSON.parse(r.body), preview: r.preview ? JSON.parse(r.preview) : null,
  result: r.result ? JSON.parse(r.result) : null };

const latest = async (env, status) =>
  parseRev(await first(env, "SELECT * FROM revs WHERE status = ? ORDER BY id DESC LIMIT 1", status));

// ---------------------------------------------------------------- settings content validation

function cleanName(s, what) {
  if (typeof s !== "string") throw bad(`${what}: not a string`);
  s = s.trim();
  if (!s || s.length > LIMIT.name || /[\u0000-\u001f\u007f]/.test(s)) throw bad(`${what}: invalid`);
  return s;
}

// → normalized body; inv = latest inventory uploaded by the server (may be empty), subIds = existing subscription IDs
export function validateBody(body, inv, subIds) {
  if (!body || typeof body !== "object") throw bad("body must be an object");
  const bm = body.b_merge || [];
  if (!Array.isArray(bm) || bm.length > LIMIT.b_merge) throw bad("b_merge invalid");
  const b_merge = [...new Set(bm.map((s) => cleanName(s, "b_merge")))];
  const ms = body.mirrors || [];
  if (!Array.isArray(ms) || ms.length > LIMIT.mirrors) throw bad("mirrors invalid");
  const bTarget = inv && inv.b_target;
  const twoWay = new Set(((inv && inv.two_way) || []).map((x) => x.target));
  const names = new Set(), outNames = new Set();
  const mirrors = ms.map((m, i) => {
    if (!m || typeof m !== "object") throw bad(`mirror #${i + 1} invalid`);
    const name = cleanName(m.name, `mirror #${i + 1} name`);
    if (names.has(name)) throw bad(`duplicate mirror name: ${name}`);
    names.add(name);
    const out = { name };
    if ((m.icloud ? 1 : 0) + (m.sub ? 1 : 0) !== 1) throw bad(`${name}: exactly one of icloud / sub`);
    if (m.icloud) out.icloud = cleanName(m.icloud, `${name} icloud`);
    else {
      if (typeof m.sub !== "string" || !subIds.has(m.sub)) throw bad(`${name}: unknown subscription`);
      out.sub = m.sub;
    }
    out.outlook = m.outlook !== false;
    out.outlook_name = m.outlook_name ? cleanName(m.outlook_name, `${name} outlook_name`) : "";
    if (out.outlook) {
      const on = out.outlook_name || name;
      if (outNames.has(on)) throw bad(`two mirrors would use the same Outlook calendar: ${on}`);
      outNames.add(on);
      if (b_merge.includes(on)) throw bad(`${on}: a mirrored Outlook calendar cannot also be merged into B (loop)`);
    }
    out.a = typeof m.a === "string" ? m.a.trim() : "";
    if (out.a && !GCAL_ID.test(out.a)) throw bad(`${name}: Google A calendar ID looks wrong`);
    if (out.a && (out.a === bTarget || twoWay.has(out.a))) throw bad(`${name}: that Google calendar is already used by another sync`);
    out.b = !!m.b;
    if (!out.outlook && !out.a && !out.b) throw bad(`${name}: pick at least one target`);
    return out;
  });
  return { b_merge, mirrors };
}

function subRefs(body) {
  return new Set(((body && body.mirrors) || []).filter((m) => m.sub).map((m) => m.sub));
}

// ---------------------------------------------------------------- browser API

async function api(req, env, url, who) {
  const p = url.pathname;
  if (p === "/api/state" && req.method === "GET") return json(await state(env, who));
  if (req.method !== "POST") throw new HttpErr(405, "method not allowed");
  const body = await readJson(req);

  if (p === "/api/draft") {
    const applied = await latest(env, "applied");
    if (await latest(env, "confirmed")) throw new HttpErr(409, "a confirmed change is still being applied; wait a minute");
    if ((body.base ?? null) !== (applied ? applied.id : null)) throw new HttpErr(409, "settings changed since the page loaded; reload");
    const inv = await getMeta(env, "inventory");
    const subIds = new Set((await all(env, "SELECT id FROM subs")).map((r) => r.id));
    const clean = validateBody(body.body, inv, subIds);
    await run(env, "UPDATE revs SET status = 'discarded' WHERE status = 'draft'");
    const r = await run(env, "INSERT INTO revs (body, base, status, created_at, created_by) VALUES (?, ?, 'draft', ?, ?)",
      JSON.stringify(clean), applied ? applied.id : null, now(), who);
    await audit(env, who, "draft", { id: r.meta.last_row_id });
    return json({ ok: true, id: r.meta.last_row_id });
  }
  if (p === "/api/repreview" || p === "/api/discard" || p === "/api/confirm") {
    const draft = await latest(env, "draft");
    if (!draft || draft.id !== body.rev) throw new HttpErr(409, "that draft is no longer current; reload");
    if (p === "/api/repreview") {
      await run(env, "UPDATE revs SET preview = NULL, preview_at = NULL WHERE id = ?", draft.id);
    } else if (p === "/api/discard") {
      await run(env, "UPDATE revs SET status = 'discarded' WHERE id = ?", draft.id);
    } else {
      const applied = await latest(env, "applied");
      if ((draft.base ?? null) !== (applied ? applied.id : null)) throw new HttpErr(409, "settings changed meanwhile; save again");
      if (!draft.preview) throw new HttpErr(409, "no preview yet");
      const errs = (draft.preview.errors || []).length + (draft.preview.rows || []).filter((r) => r.error).length;
      if (errs) throw new HttpErr(409, "the preview has errors; fix them or preview again");
      await run(env, "UPDATE revs SET status = 'confirmed', confirmed_at = ? WHERE id = ? AND status = 'draft'", now(), draft.id);
    }
    await audit(env, who, p.slice(5), { id: draft.id });
    return json({ ok: true });
  }
  if (p === "/api/subs") {
    const label = cleanName(body.label, "label");
    const u = typeof body.url === "string" ? body.url.trim() : "";
    if (u.length > LIMIT.url || !/^(https|webcal):\/\/[^\s/]+\/\S*$/i.test(u)) throw bad("link must start with https:// or webcal://");
    const id = [...crypto.getRandomValues(new Uint8Array(8))].map((b) => b.toString(16).padStart(2, "0")).join("");
    const { iv, ct } = await encryptUrl(env, id, u);
    await run(env, "INSERT INTO subs (id, label, iv, ct, created_at) VALUES (?, ?, ?, ?, ?)", id, label, iv, ct, now());
    await audit(env, who, "sub-add", { id, label });   // the link itself is not logged
    return json({ ok: true, id });
  }
  if (p === "/api/subs/delete") {
    const used = new Set();
    for (const s of ["applied", "confirmed", "draft"]) for (const id of subRefs((await latest(env, s))?.body)) used.add(id);
    if (used.has(body.id)) throw new HttpErr(409, "still used by a saved setting; remove it there first");
    await run(env, "DELETE FROM subs WHERE id = ?", String(body.id));
    await audit(env, who, "sub-delete", { id: body.id });
    return json({ ok: true });
  }
  if (p === "/api/cleanup") {
    const inv = await getMeta(env, "inventory");
    const left = ((inv && inv.leftovers) || []).find((l) => l.kind === body.kind && l.name === body.name && l.target === body.target);
    if (!left) throw new HttpErr(409, "not in the leftover list any more; reload");
    if (await first(env, "SELECT id FROM cleanups WHERE status = 'pending' AND kind = ? AND name = ? AND target = ?",
      left.kind, left.name, left.target)) throw new HttpErr(409, "already queued");
    await run(env, "INSERT INTO cleanups (kind, name, target, count, status, requested_at) VALUES (?, ?, ?, ?, 'pending', ?)",
      left.kind, left.name, left.target, left.count | 0, now());
    await audit(env, who, "cleanup", left);
    return json({ ok: true });
  }
  if (p === "/api/interval") {          // sync interval (5 / 10 / 15 / 30 min): no event changes, effective at once
    const m = body.minutes;
    if (!INTERVALS.includes(m)) throw bad(`minutes must be one of ${INTERVALS.join(", ")}`);
    await putMeta(env, "interval", { minutes: m });
    await audit(env, who, "interval", { minutes: m });
    return json({ ok: true });
  }
  if (p === "/api/sync-now") {
    await putMeta(env, "sync_now", { at: now() });
    await audit(env, who, "sync-now");
    return json({ ok: true });
  }
  if (p === "/api/refresh") {
    await putMeta(env, "want_inventory", { v: 1 });
    return json({ ok: true });
  }
  throw new HttpErr(404, "not found");
}

async function state(env, who) {
  const inv = await getMeta(env, "inventory");
  const seen = await getMeta(env, "agent_seen");
  const revs = await all(env, "SELECT id, base, status, seed, created_at, created_by, preview_at, confirmed_at, applied_at, result FROM revs ORDER BY id DESC LIMIT 15");
  const subs = await all(env, "SELECT id, label, created_at FROM subs ORDER BY created_at");
  const cleanups = await all(env, "SELECT * FROM cleanups ORDER BY id DESC LIMIT 20");
  const want = await getMeta(env, "want_inventory");
  const iv = await getMeta(env, "interval");
  const sn = await getMeta(env, "sync_now");
  return {
    interval: (iv && iv.minutes) || DEFAULT_INTERVAL, intervals: INTERVALS, sync_now: sn && sn.at ? sn.at : null,
    who, now: now(), version: env.VERSION || "dev",
    inventory: inv, agent_seen: seen ? seen._at : null, want_inventory: !!(want && want.v),
    applied: await latest(env, "applied"),
    confirmed: await latest(env, "confirmed"),
    draft: await latest(env, "draft"),
    history: revs.map((r) => ({ ...r, result: r.result ? JSON.parse(r.result) : null })),
    subs, cleanups,
  };
}

// ---------------------------------------------------------------- server (calsync agent)

async function agent(req, env, url) {
  const p = url.pathname;
  await putMeta(env, "agent_seen", { v: 1 });
  if (p === "/agent/poll" && req.method === "GET") {
    const confirmed = await latest(env, "confirmed");
    const draftRow = await first(env, "SELECT * FROM revs WHERE status = 'draft' AND preview IS NULL ORDER BY id DESC LIMIT 1");
    const draft = parseRev(draftRow);
    const applied = await latest(env, "applied");
    const need = new Set([...subRefs(confirmed?.body), ...subRefs(draft?.body), ...subRefs(applied?.body)]);
    const subs = {};
    for (const id of need) {
      const row = await first(env, "SELECT * FROM subs WHERE id = ?", id);
      if (row) subs[id] = await decryptUrl(env, row);
    }
    const want = await getMeta(env, "want_inventory");
    const iv = await getMeta(env, "interval");
    const sn = await getMeta(env, "sync_now");
    return json({
      interval: (iv && iv.minutes) || DEFAULT_INTERVAL,
      sync_now: sn && sn.at ? sn.at : null,
      applied: applied && { id: applied.id, body: applied.body },
      confirmed: confirmed && { id: confirmed.id, body: confirmed.body, seed: !!confirmed.seed, preview: confirmed.preview },
      draft: draft && { id: draft.id, body: draft.body },
      subs,
      cleanups: await all(env, "SELECT id, kind, name, target, count FROM cleanups WHERE status = 'pending' ORDER BY id"),
      want_inventory: !!(want && want.v),
    });
  }
  if (req.method !== "POST") throw new HttpErr(405, "method not allowed");

  if (p === "/agent/inventory") {
    const inv = await readJson(req, LIMIT.inventory);
    await putMeta(env, "inventory", inv);
    await putMeta(env, "want_inventory", { v: 0 });
    // First time: import the server's existing config as revision 1 (settings already running;
    // the server takes over directly once it verifies they match)
    const any = await first(env, "SELECT id FROM revs LIMIT 1");
    if (!any && inv.current) {
      const clean = validateBody(inv.current, inv, new Set());
      await run(env, "INSERT INTO revs (body, base, status, seed, created_at, created_by, confirmed_at) VALUES (?, NULL, 'confirmed', 1, ?, 'server', ?)",
        JSON.stringify(clean), now(), now());
      await audit(env, "server", "seed", clean);
    }
    return json({ ok: true });
  }
  if (p === "/agent/preview") {
    const b = await readJson(req, LIMIT.preview);
    const prev = JSON.stringify({ rows: b.rows || [], errors: b.errors || [] });
    await run(env, "UPDATE revs SET preview = ?, preview_at = ? WHERE id = ? AND status = 'draft'", prev, now(), b.rev | 0);
    return json({ ok: true });
  }
  if (p === "/agent/applied") {
    const b = await readJson(req, LIMIT.preview);
    const rev = await first(env, "SELECT id, status FROM revs WHERE id = ?", b.rev | 0);
    if (!rev || rev.status !== "confirmed") throw new HttpErr(409, "not a confirmed revision");
    const result = JSON.stringify({ ok: !!b.ok, errors: b.errors || [], rows: b.rows || [] });
    if (b.ok) {
      await run(env, "UPDATE revs SET status = 'superseded' WHERE status = 'applied'");
      await run(env, "UPDATE revs SET status = 'applied', applied_at = ?, result = ? WHERE id = ?", now(), result, rev.id);
    } else {
      await run(env, "UPDATE revs SET status = 'failed', applied_at = ?, result = ? WHERE id = ?", now(), result, rev.id);
    }
    await audit(env, "server", b.ok ? "applied" : "apply-failed", { id: rev.id, errors: b.errors || [] });
    return json({ ok: true });
  }
  if (p === "/agent/ack-sync") {         // "Sync now" has finished: clear that same request (if clicked again in the meantime, keep it)
    const b = await readJson(req);
    const sn = await getMeta(env, "sync_now");
    if (sn && sn.at && sn.at === b.at) await run(env, "DELETE FROM meta WHERE k = 'sync_now'");
    return json({ ok: true });
  }
  if (p === "/agent/cleanup") {
    const b = await readJson(req);
    await run(env, "UPDATE cleanups SET status = ?, done_at = ?, result = ? WHERE id = ? AND status = 'pending'",
      b.ok ? "done" : "failed", now(), JSON.stringify({ deleted: b.deleted | 0, error: b.error || "" }), b.id | 0);
    return json({ ok: true });
  }
  throw new HttpErr(404, "not found");
}
