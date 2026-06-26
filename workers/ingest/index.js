/**
 * heardhere.uk — Ingest Worker
 * Receives detection batches and heartbeats from NatureWarden units.
 * Authenticates via per-unit API key stored in KV.
 * Writes detections to D1, updates unit health metrics.
 *
 * Bound resources (set in wrangler.toml):
 *   - KV namespace: UNIT_KEYS  (unit_id → api_key_hash)
 *   - D1 database:  DB
 *   - R2 bucket:    AUDIO  (for future audio clip uploads)
 */

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // Route
    if (request.method === "POST" && url.pathname === "/api/ingest") {
      return handleIngest(request, env);
    }
    if (request.method === "GET" && url.pathname === "/api/health") {
      return new Response("ok", { status: 200 });
    }

    return new Response("Not found", { status: 404 });
  },
};

// ---------------------------------------------------------------------------
// Ingest handler
// ---------------------------------------------------------------------------
async function handleIngest(request, env) {
  // 1. Auth
  const apiKey = request.headers.get("X-API-Key");
  if (!apiKey) {
    return jsonError(401, "Missing X-API-Key");
  }

  let body;
  try {
    body = await request.json();
  } catch {
    return jsonError(400, "Invalid JSON");
  }

  const { unit_id } = body;
  if (!unit_id) {
    return jsonError(400, "Missing unit_id");
  }

  // Look up expected key hash from KV
  const storedHash = await env.UNIT_KEYS.get(`unit:${unit_id}:key_hash`);
  if (!storedHash) {
    return jsonError(403, "Unknown unit");
  }

  // Compare SHA-256 of provided key against stored hash
  const providedHash = await sha256(apiKey);
  if (providedHash !== storedHash) {
    return jsonError(403, "Invalid API key");
  }

  // 2. Validate payload structure
  const { timestamp, detections = [], health = {}, version } = body;

  if (!timestamp || typeof timestamp !== "number") {
    return jsonError(400, "Missing or invalid timestamp");
  }

  // 3. Write detections to D1
  const insertedIds = [];
  if (detections.length > 0) {
    // Batch insert in chunks of 50 (D1 limit awareness)
    const chunks = chunkArray(detections, 50);
    for (const chunk of chunks) {
      const ids = await insertDetections(env.DB, unit_id, chunk);
      insertedIds.push(...ids);
    }
  }

  // 4. Update unit heartbeat and health in D1
  await updateUnitHealth(env.DB, unit_id, {
    timestamp,
    version,
    health,
    detection_count: detections.length,
  });

  // 5. Respond
  return jsonResponse(200, {
    ok: true,
    unit_id,
    detections_stored: insertedIds.length,
    server_time: Date.now(),
  });
}

// ---------------------------------------------------------------------------
// D1 helpers
// ---------------------------------------------------------------------------
async function insertDetections(db, unit_id, detections) {
  const ids = [];
  for (const d of detections) {
    try {
      const result = await db
        .prepare(`
          INSERT INTO detections (unit_id, type, species, scientific, confidence, detected_at, audio_r2_key)
          VALUES (?, ?, ?, ?, ?, ?, ?)
        `)
        .bind(
          unit_id,
          d.type || "unknown",
          d.species || "Unknown",
          d.scientific || null,
          d.confidence || 0,
          d.detected_at || null,
          d.audio_r2_key || null
        )
        .run();
      if (result.meta?.last_row_id) {
        ids.push(result.meta.last_row_id);
      }
    } catch (e) {
      // Log but continue — don't fail the whole batch for one bad row
      console.error(`Insert failed for detection: ${e.message}`, d);
    }
  }
  return ids;
}

async function updateUnitHealth(db, unit_id, { timestamp, version, health, detection_count }) {
  // Find the last bird and bat detection times from this batch's context
  // (simplified — real impl tracks per-type in detections table)
  await db
    .prepare(`
      UPDATE units SET
        last_heartbeat = ?,
        version = ?,
        battery_voltage = ?,
        battery_percent = ?,
        signal_dbm = ?,
        disk_free_gb = ?,
        cpu_percent = ?
      WHERE id = ?
    `)
    .bind(
      timestamp,
      version || null,
      health.battery_voltage || null,
      health.battery_percent || null,
      health.signal_dbm || null,
      health.disk_free_gb || null,
      health.cpu_percent || null,
      unit_id
    )
    .run();
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------
async function sha256(str) {
  const buf = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(str)
  );
  return Array.from(new Uint8Array(buf))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function chunkArray(arr, size) {
  const chunks = [];
  for (let i = 0; i < arr.length; i += size) {
    chunks.push(arr.slice(i, i + size));
  }
  return chunks;
}

function jsonResponse(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function jsonError(status, message) {
  return jsonResponse(status, { ok: false, error: message });
}
