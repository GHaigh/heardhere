/**
 * heardhere.uk — API Worker
 * Serves detection data, stats, and unit config to public-facing sites.
 * Read-only. No auth required for public endpoints.
 * Admin endpoints (health, fleet) require X-Admin-Key header.
 *
 * Routes:
 *   GET /api/unit/:id/config        — site name, w3w, enabled sensors (public)
 *   GET /api/unit/:id/birds         — latest bird detections + today's stats
 *   GET /api/unit/:id/bats          — latest bat detections + today's stats
 *   GET /api/unit/:id/weather       — latest weather reading (if enabled)
 *   GET /api/unit/:id/summary       — combined summary for homepage widget
 *   GET /api/admin/fleet            — all units + health (admin only)
 *   GET /api/admin/unit/:id/health  — single unit health detail (admin only)
 *
 * Bound resources (wrangler.toml):
 *   - D1 database: DB
 *   - KV namespace: ADMIN_KEYS
 */

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, X-Admin-Key",
};

export default {
  async fetch(request, env, ctx) {
    // CORS preflight
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }
    if (request.method !== "GET") {
      return jsonError(405, "Method not allowed");
    }

    const url = new URL(request.url);
    const path = url.pathname;

    // ── Admin routes (require X-Admin-Key) ──────────────────────────────────
    if (path.startsWith("/api/admin/")) {
      const adminKey = request.headers.get("X-Admin-Key");
      const storedKey = await env.ADMIN_KEYS.get("admin:dashboard:key");
      if (!adminKey || adminKey !== storedKey) {
        return jsonError(403, "Forbidden");
      }
      if (path === "/api/admin/fleet") {
        return handleFleet(env);
      }
      const healthMatch = path.match(/^\/api\/admin\/unit\/([^/]+)\/health$/);
      if (healthMatch) {
        return handleUnitHealth(env, healthMatch[1]);
      }
      return jsonError(404, "Not found");
    }

    // ── Public routes ────────────────────────────────────────────────────────
    const unitMatch = path.match(/^\/api\/unit\/([^/]+)\/(.+)$/);
    if (!unitMatch) {
      return jsonError(404, "Not found");
    }

    const [, unitId, resource] = unitMatch;

    // Validate unit exists
    const unit = await getUnit(env.DB, unitId);
    if (!unit) {
      return jsonError(404, `Unit '${unitId}' not found`);
    }

    switch (resource) {
      case "config":   return handleConfig(unit);
      case "birds":    return handleDetections(env.DB, unitId, "bird", url);
      case "bats":     return handleDetections(env.DB, unitId, "bat", url);
      case "weather":  return handleWeather(env.DB, unitId);
      case "summary":  return handleSummary(env.DB, unitId, unit);
      default:         return jsonError(404, `Unknown resource '${resource}'`);
    }
  },
};

// ── Unit lookup ──────────────────────────────────────────────────────────────

async function getUnit(db, unitId) {
  const result = await db
    .prepare("SELECT * FROM units WHERE id = ?")
    .bind(unitId)
    .first();
  return result || null;
}

// ── Public: /config ──────────────────────────────────────────────────────────

function handleConfig(unit) {
  // Only return public-safe fields — never API keys or health metrics
  return jsonResponse(200, {
    unit_id: unit.id,
    site_name: unit.site_name,
    w3w: unit.w3w,
    lat: unit.lat,
    lon: unit.lon,
    enabled_sensors: JSON.parse(unit.enabled_sensors || "[]"),
    theme_colour: unit.theme_colour || "#2d6a4f",
  });
}

// ── Public: /birds or /bats ──────────────────────────────────────────────────

async function handleDetections(db, unitId, type, url) {
  const limit = Math.min(parseInt(url.searchParams.get("limit") || "50"), 200);
  const since = url.searchParams.get("since"); // ISO date string, optional

  // Latest detections
  let query, params;
  if (since) {
    query = `
      SELECT species, scientific, confidence, detected_at, audio_r2_key
      FROM detections
      WHERE unit_id = ? AND type = ? AND detected_at > ?
      ORDER BY detected_at DESC
      LIMIT ?
    `;
    params = [unitId, type, since, limit];
  } else {
    query = `
      SELECT species, scientific, confidence, detected_at, audio_r2_key
      FROM detections
      WHERE unit_id = ? AND type = ?
      ORDER BY detected_at DESC
      LIMIT ?
    `;
    params = [unitId, type, limit];
  }

  const { results: detections } = await db.prepare(query).bind(...params).all();

  // Today's stats
  const today = new Date().toISOString().split("T")[0];
  const statsRow = await db
    .prepare(`
      SELECT ${type}_count as count, species_seen
      FROM daily_stats
      WHERE unit_id = ? AND stat_date = ?
    `)
    .bind(unitId, today)
    .first();

  // Species seen this week (for the phenology-style list)
  const weekAgo = new Date(Date.now() - 7 * 24 * 60 * 60 * 1000)
    .toISOString()
    .split("T")[0];
  const { results: weekSpecies } = await db
    .prepare(`
      SELECT species, COUNT(*) as detections, MAX(confidence) as peak_confidence
      FROM detections
      WHERE unit_id = ? AND type = ? AND detected_at >= ?
      GROUP BY species
      ORDER BY detections DESC
      LIMIT 20
    `)
    .bind(unitId, type, weekAgo + "T00:00:00")
    .all();

  return jsonResponse(200, {
    unit_id: unitId,
    type,
    detections,
    today: {
      count: statsRow?.count || 0,
      species_seen: JSON.parse(statsRow?.species_seen || "[]"),
    },
    this_week: weekSpecies,
    generated_at: new Date().toISOString(),
  });
}

// ── Public: /weather ─────────────────────────────────────────────────────────

async function handleWeather(db, unitId) {
  // Weather readings stored in a separate table (added when weather sensor enabled)
  // Returns the most recent reading
  try {
    const reading = await db
      .prepare(`
        SELECT temperature_c, humidity_pct, pressure_hpa,
               wind_speed_ms, wind_direction_deg,
               rainfall_mm_1h, uv_index, lux,
               recorded_at
        FROM weather_readings
        WHERE unit_id = ?
        ORDER BY recorded_at DESC
        LIMIT 1
      `)
      .bind(unitId)
      .first();

    if (!reading) {
      return jsonResponse(200, { unit_id: unitId, reading: null });
    }

    return jsonResponse(200, { unit_id: unitId, reading });
  } catch {
    // Table may not exist yet if weather sensor not yet enabled
    return jsonResponse(200, { unit_id: unitId, reading: null });
  }
}

// ── Public: /summary ─────────────────────────────────────────────────────────

async function handleSummary(db, unitId, unit) {
  // Lightweight combined summary for homepage cards
  // Avoids multiple API calls from the site

  const today = new Date().toISOString().split("T")[0];

  const stats = await db
    .prepare(`
      SELECT bird_count, bat_count, species_seen
      FROM daily_stats
      WHERE unit_id = ? AND stat_date = ?
    `)
    .bind(unitId, today)
    .first();

  // Most recent detection of each type
  const lastBird = await db
    .prepare(`
      SELECT species, confidence, detected_at
      FROM detections WHERE unit_id = ? AND type = 'bird'
      ORDER BY detected_at DESC LIMIT 1
    `)
    .bind(unitId)
    .first();

  const lastBat = await db
    .prepare(`
      SELECT species, confidence, detected_at
      FROM detections WHERE unit_id = ? AND type = 'bat'
      ORDER BY detected_at DESC LIMIT 1
    `)
    .bind(unitId)
    .first();

  return jsonResponse(200, {
    unit_id: unitId,
    site_name: unit.site_name,
    w3w: unit.w3w,
    today: {
      birds: stats?.bird_count || 0,
      bats: stats?.bat_count || 0,
    },
    last_bird: lastBird || null,
    last_bat: lastBat || null,
    generated_at: new Date().toISOString(),
  });
}

// ── Admin: /admin/fleet ───────────────────────────────────────────────────────

async function handleFleet(env) {
  const { results: units } = await env.DB
    .prepare(`
      SELECT
        id, site_name, w3w, enabled_sensors, version,
        last_heartbeat, battery_percent, battery_voltage,
        signal_dbm, disk_free_gb, cpu_percent,
        last_recording_birds, last_recording_bats
      FROM units
      ORDER BY site_name ASC
    `)
    .all();

  const now = Math.floor(Date.now() / 1000);

  const fleet = units.map(u => ({
    ...u,
    enabled_sensors: JSON.parse(u.enabled_sensors || "[]"),
    heartbeat_age_minutes: u.last_heartbeat
      ? Math.floor((now - u.last_heartbeat) / 60)
      : null,
    // Green < 30min, Amber < 2hr, Red = problem
    status: heartbeatStatus(now, u.last_heartbeat),
    birds_stale: isStale(now, u.last_recording_birds, 6 * 3600), // stale if >6hr
    bats_stale: isStale(now, u.last_recording_bats, 6 * 3600),
  }));

  return jsonResponse(200, { fleet, count: fleet.length });
}

// ── Admin: /admin/unit/:id/health ────────────────────────────────────────────

async function handleUnitHealth(env, unitId) {
  const unit = await getUnit(env.DB, unitId);
  if (!unit) return jsonError(404, "Unit not found");

  const now = Math.floor(Date.now() / 1000);

  return jsonResponse(200, {
    unit_id: unitId,
    site_name: unit.site_name,
    version: unit.version,
    status: heartbeatStatus(now, unit.last_heartbeat),
    heartbeat_age_minutes: unit.last_heartbeat
      ? Math.floor((now - unit.last_heartbeat) / 60)
      : null,
    battery_percent: unit.battery_percent,
    battery_voltage: unit.battery_voltage,
    signal_dbm: unit.signal_dbm,
    disk_free_gb: unit.disk_free_gb,
    cpu_percent: unit.cpu_percent,
    last_recording_birds: unit.last_recording_birds,
    last_recording_bats: unit.last_recording_bats,
  });
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function heartbeatStatus(now, lastHeartbeat) {
  if (!lastHeartbeat) return "unknown";
  const age = now - lastHeartbeat;
  if (age < 30 * 60)  return "green";   // < 30 min
  if (age < 2 * 3600) return "amber";   // < 2 hours
  return "red";
}

function isStale(now, lastTimestamp, thresholdSeconds) {
  if (!lastTimestamp) return true;
  return (now - lastTimestamp) > thresholdSeconds;
}

function jsonResponse(status, body) {
  return new Response(JSON.stringify(body, null, 2), {
    status,
    headers: {
      "Content-Type": "application/json",
      ...CORS_HEADERS,
    },
  });
}

function jsonError(status, message) {
  return jsonResponse(status, { ok: false, error: message });
}
