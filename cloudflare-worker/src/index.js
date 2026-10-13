const CHANNELS_URL = "https://raw.githubusercontent.com/Varad-S-K/Channel-Json/refs/heads/main/JioTv_MOB.json";
const EPG_URL = "https://jiotv.data.cdn.jio.com/apis/v1.3/getepg/get";
const OFFSETS = [0, 1, 2];
const BATCH_SIZE = 10;
const CONCURRENCY = 5;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/status") {
      const meta = await env.EPG_KV.get("meta", "json");
      return reply(meta || { ready: false, message: "Wait for scheduled batches to run." }, meta ? 200 : 503);
    }
    if (url.pathname !== "/" && url.pathname !== "/epg.xml") {
      return new Response("Use /epg.xml or /status", { status: 404 });
    }

    const meta = await env.EPG_KV.get("meta", "json");
    if (!meta) return new Response("EPG is not built yet. Wait for the first scheduled refresh.", { status: 503 });

    const parts = await Promise.all(
      Array.from({ length: meta.chunkCount }, (_, i) => env.EPG_KV.get(`chunk:${i}`, "json"))
    );
    const programmes = [];
    for (const part of parts) {
      if (part?.signature !== meta.signature || !Array.isArray(part.channels)) continue;
      for (const channel of part.channels) programmes.push(...(channel.programmes || []));
    }
    const xml = makeXml(meta.channels, programmes);
    return new Response(xml, {
      headers: {
        "content-type": "application/xml; charset=utf-8",
        "cache-control": "public, max-age=300",
        "access-control-allow-origin": "*"
      }
    });
  },

  scheduled(event, env, ctx) {
    ctx.waitUntil(refreshBatch(env));
  }
};

async function refreshBatch(env) {
  const response = await fetch(CHANNELS_URL, { headers: { accept: "application/json" } });
  if (!response.ok) throw new Error(`Channel list HTTP ${response.status}`);
  const source = await response.json();
  const raw = findArray(source);
  const channels = [];
  const seen = new Set();
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const id = first(item, ["id", "channel_id", "channelId", "channelid", "channelID", "channel_id_int"]);
    if (id == null || String(id).trim() === "") continue;
    const sid = String(id).trim();
    if (seen.has(sid)) continue;
    seen.add(sid);
    channels.push({
      id: sid,
      name: String(first(item, ["name", "channel_name", "channelName", "channelname", "channel"]) ?? sid).trim(),
      logo: String(first(item, ["logo", "logoUrl", "logo_url", "channel_logo"]) ?? "").trim()
    });
  }
  if (!channels.length) throw new Error("No channel IDs found in the channel JSON.");

  const signature = await sha256(channels.map(c => `${c.id}:${c.name}`).join("|"));
  const chunkCount = Math.ceil(channels.length / BATCH_SIZE);
  let meta = await env.EPG_KV.get("meta", "json");
  if (!meta || meta.signature !== signature) {
    meta = { signature, channels, chunkCount, cursor: 0, completedChunks: [], lastRefresh: null };
  } else {
    meta.channels = channels;
    meta.chunkCount = chunkCount;
    meta.completedChunks = (meta.completedChunks || []).filter(i => i < chunkCount);
    if (meta.cursor >= chunkCount) meta.cursor = 0;
  }

  const index = meta.cursor || 0;
  const batch = channels.slice(index * BATCH_SIZE, (index + 1) * BATCH_SIZE);
  const oldChunk = await env.EPG_KV.get(`chunk:${index}`, "json");
  const oldById = new Map((oldChunk?.channels || []).map(c => [c.id, c]));
  const current = new Map(batch.map(c => [c.id, { ...c, programmes: [], failures: 0 }]));
  const jobs = batch.flatMap(channel => OFFSETS.map(offset => ({ channel, offset })));

  await pool(jobs, CONCURRENCY, async ({ channel, offset }) => {
    const q = new URLSearchParams({ offset: String(offset), channel_id: channel.id, langId: "6" });
    try {
      const r = await fetch(`${EPG_URL}?${q}`, {
        headers: {
          accept: "application/json",
          "user-agent": "Mozilla/5.0",
          referer: "https://www.jiotv.com/"
        }
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      const rows = Array.isArray(data) ? data : data?.epg;
      if (!Array.isArray(rows)) throw new Error("Response has no epg array");
      const target = current.get(channel.id);
      for (const row of rows) {
        const p = normalize(row, channel.id);
        if (p) target.programmes.push(p);
      }
    } catch (e) {
      current.get(channel.id).failures++;
      console.log(`channel=${channel.id} offset=${offset}: ${String(e)}`);
    }
  });

  const failedChannels = batch.filter(channel =>
    current.get(channel.id).failures >= OFFSETS.length
  );
  if (failedChannels.length === batch.length) {
    throw new Error(
      `All EPG requests failed for batch ${index}; check the Worker logs for API errors.`
    );
  }

  const saved = batch.map(c => {
    const fresh = current.get(c.id);
    const old = oldById.get(c.id);
    if (fresh.failures && old?.programmes?.length) fresh.programmes.push(...old.programmes);
    const unique = new Map();
    for (const p of fresh.programmes) unique.set(`${p.channelId}|${p.start}|${p.stop}|${p.title}`, p);
    return { ...c, programmes: [...unique.values()] };
  });

  await env.EPG_KV.put(`chunk:${index}`, JSON.stringify({ signature, channels: saved, updatedAt: new Date().toISOString() }));
  const done = new Set(meta.completedChunks || []);
  done.add(index);
  meta.completedChunks = [...done].sort((a, b) => a - b);
  meta.cursor = (index + 1) % chunkCount;
  meta.lastRefresh = new Date().toISOString();
  meta.lastBatch = index;
  await env.EPG_KV.put("meta", JSON.stringify(meta));
}

function findArray(value) {
  if (Array.isArray(value)) return value;
  if (value && typeof value === "object") {
    for (const k of ["channels", "data", "result", "results", "channelList", "channel_list"]) {
      if (Array.isArray(value[k])) return value[k];
    }
    for (const child of Object.values(value)) {
      if (child && typeof child === "object") {
        try { const a = findArray(child); if (a.length) return a; } catch {}
      }
    }
  }
  throw new Error("Could not find a channel array in JSON");
}
function first(o, keys) { for (const k of keys) if (o[k] != null && o[k] !== "") return o[k]; return null; }
function epoch(v) {
  if (v == null || v === "") return 0;
  if (typeof v === "string" && !/^\d+$/.test(v)) {
    const parsed = Date.parse(v); return Number.isFinite(parsed) ? Math.floor(parsed / 1000) : 0;
  }
  let n = Number(v); if (!Number.isFinite(n)) return 0;
  if (n > 1e12) n = Math.floor(n / 1000);
  return Math.floor(n);
}
function normalize(row, id) {
  if (!row || typeof row !== "object") return null;
  const start = epoch(first(row, ["startEpoch", "start_epoch", "start", "startTime"]));
  const stop = epoch(first(row, ["endEpoch", "end_epoch", "end", "endTime"]));
  const title = first(row, ["showname", "title", "programName", "program_name"]);
  if (!start || !stop || stop <= start || !title) return null;
  return {
    channelId: String(first(row, ["channel_id", "channelId"]) ?? id),
    start, stop,     title: String(title).trim(),
    description: String(first(row, ["description", "desc"]) ?? "").trim(),
    category: String(first(row, ["showCategory", "category"]) ?? "").trim(),
    icon: String(first(row, ["episodeThumbnail", "episodePoster", "icon"]) ?? "").trim()
  };
}
function xmlEscape(v) {
  return String(v ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&apos;");
}
function time(s) {
  const d = new Date(s * 1000); const p = n => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}${p(d.getUTCMonth()+1)}${p(d.getUTCDate())}${p(d.getUTCHours())}${p(d.getUTCMinutes())}${p(d.getUTCSeconds())} +0000`;
}
function makeXml(channels, programmes) {
  const out = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="JioTV EPG Worker">'];
  for (const c of channels) {
    out.push(`  <channel id="${xmlEscape(c.id)}">`);
    out.push(`    <display-name>${xmlEscape(c.name)}</display-name>`);
    if (/^https?:\/\//i.test(c.logo)) out.push(`    <icon src="${xmlEscape(c.logo)}" />`);
    out.push("  </channel>");
  }
  const unique = new Map();
  for (const p of programmes) unique.set(`${p.channelId}|${p.start}|${p.stop}|${p.title}`, p);
  for (const p of [...unique.values()].sort((a, b) =>
    a.channelId.localeCompare(b.channelId) || a.start - b.start || a.stop - b.stop
  )) {
    out.push(`  <programme start="${time(p.start)}" stop="${time(p.stop)}" channel="${xmlEscape(p.channelId)}">`);
    out.push(`    <title lang="en">${xmlEscape(p.title)}</title>`);
    if (p.description) out.push(`    <desc lang="en">${xmlEscape(p.description)}</desc>`);
    if (p.category) out.push(`    <category lang="en">${xmlEscape(p.category)}</category>`);
    if (/^https?:\/\//i.test(p.icon)) out.push(`    <icon src="${xmlEscape(p.icon)}" />`);
    out.push("  </programme>");
  }
  out.push("</tv>");
  return `${out.join("\n")}\n`;
}
async function pool(items, limit, fn) {
  let next = 0;
  await Promise.all(Array.from({length: Math.min(limit, items.length)}, async () => {
    while (true) { const i = next++; if (i >= items.length) return; await fn(items[i]); }
  }));
}
async function sha256(s) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, "0")).join("");
}
function reply(obj, status=200) {
  return new Response(JSON.stringify(obj, null, 2), { status, headers: { "content-type": "application/json; charset=utf-8" } });
}
