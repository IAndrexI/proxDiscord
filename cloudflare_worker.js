/**
 * Protutech Cloud - Multi-User Discord RPC Hub
 * Cloudflare Worker with KV Storage
 * 
 * Setup Instructions:
 * 1. In Cloudflare Dashboard -> Workers & Pages -> Create Worker.
 * 2. In Worker Settings -> Bindings -> Add KV Namespace:
 *    - Variable name: DASHBOARD_KV
 * 3. Paste this code into your worker and Deploy!
 * 4. Assign your custom domain (e.g. dash.protutech.vip or stats.protutech.vip).
 */

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization"
};

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const pathname = url.pathname;

    // Handle CORS preflight
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: CORS_HEADERS });
    }

    // 1. API: List all registered users
    if (pathname === "/api/users" && request.method === "GET") {
      try {
        const list = await env.DASHBOARD_KV.list({ prefix: "user_meta:" });
        const users = [];
        for (const key of list.keys) {
          const val = await env.DASHBOARD_KV.get(key.name, { type: "json" });
          if (val) {
            const isOnline = (Date.now() - (val.last_updated || 0)) < 120000;
            users.push({
              ...val,
              online: isOnline
            });
          }
        }
        return new Response(JSON.stringify({ users, default_user: "andrex" }), {
          headers: { ...CORS_HEADERS, "Content-Type": "application/json" }
        });
      } catch (e) {
        return new Response(JSON.stringify({ users: [], error: e.message }), {
          headers: { ...CORS_HEADERS, "Content-Type": "application/json" }
        });
      }
    }

    // 2. API: Fetch screens for a specific user (?user=...)
    if ((pathname === "/api/stats" || pathname === "/api/screens") && request.method === "GET") {
      const userId = (url.searchParams.get("user") || "andrex").toLowerCase();
      try {
        let userData = await env.DASHBOARD_KV.get("user_data:" + userId, { type: "json" });
        if (!userData) {
          // Fallback to primary user if specific user not found
          userData = await env.DASHBOARD_KV.get("user_data:andrex", { type: "json" }) || {
            user_id: userId,
            user_name: userId,
            screens: [],
            current_screen_name: "Awaiting sync",
            screen_index: 0,
            total_screens: 0,
            last_updated: 0
          };
        }
        return new Response(JSON.stringify(userData), {
          headers: { ...CORS_HEADERS, "Content-Type": "application/json" }
        });
      } catch (e) {
        return new Response(JSON.stringify({ screens: [], error: e.message }), {
          headers: { ...CORS_HEADERS, "Content-Type": "application/json" }
        });
      }
    }

    // 3. API: Ingest live telemetry heartbeat from client
    if (pathname === "/api/push" && request.method === "POST") {
      try {
        const data = await request.json();
        const userId = String(data.user_id || "").trim().toLowerCase();
        if (!userId) {
          return new Response(JSON.stringify({ error: "Missing user_id" }), { status: 400, headers: CORS_HEADERS });
        }

        const now = Date.now();
        const statePayload = {
          user_id: userId,
          user_name: data.user_name || userId,
          user_avatar: data.user_avatar || "",
          screens: data.screens || [],
          current_screen_name: data.current_screen_name || "",
          screen_index: data.screen_index || 0,
          total_screens: (data.screens || []).length,
          last_updated: now
        };

        const metaPayload = {
          id: userId,
          name: data.user_name || userId,
          avatar: data.user_avatar || "",
          current_screen: data.current_screen_name || "",
          total_screens: (data.screens || []).length,
          last_updated: now
        };

        // Store user state in KV (7-day persistence)
        await env.DASHBOARD_KV.put("user_data:" + userId, JSON.stringify(statePayload), { expirationTtl: 604800 });
        await env.DASHBOARD_KV.put("user_meta:" + userId, JSON.stringify(metaPayload), { expirationTtl: 604800 });

        return new Response(JSON.stringify({ status: "ok", user: userId }), {
          headers: { ...CORS_HEADERS, "Content-Type": "application/json" }
        });
      } catch (e) {
        return new Response(JSON.stringify({ error: e.message }), { status: 400, headers: CORS_HEADERS });
      }
    }

    // 4. Default: Fetch dashboard HTML from your assets or GitHub raw
    // Fetches the latest committed dashboard.html directly from your GitHub repo
    try {
      const ghRaw = await fetch("https://raw.githubusercontent.com/IAndrexI/proxDiscord/main/dashboard.html", {
        cf: { cacheTtl: 60 }
      });
      if (ghRaw.ok) {
        const html = await ghRaw.text();
        return new Response(html, {
          headers: { "Content-Type": "text/html; charset=utf-8" }
        });
      }
    } catch (e) {
      // Fallback
    }

    return new Response("<!DOCTYPE html><html><body><h1>Protutech Cloud Hub</h1><p>Worker active.</p></body></html>", {
      headers: { "Content-Type": "text/html; charset=utf-8" }
    });
  }
};
