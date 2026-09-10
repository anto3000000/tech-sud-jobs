/**
 * sudtechjobs — endpoint d'inscription aux alertes email (Cloudflare Worker).
 *
 * Existe pour que la clé API EmailOctopus ne soit jamais dans le front statique.
 *
 *   POST /subscribe   { email, search, search_label }
 *     -> crée un contact dans la liste EmailOctopus. La liste doit avoir le
 *        double opt-in activé : EmailOctopus envoie alors l'email de confirmation.
 *
 * Secrets (wrangler secret put ...) :
 *   EO_API_KEY   clé API EmailOctopus
 *   EO_LIST_ID   id de la liste « Alertes sudtechjobs »
 *
 * Vars optionnelles (wrangler.toml [vars]) :
 *   ALLOW_ORIGIN      origine autorisée en CORS   (défaut https://sudtechjobs.com)
 *   EO_FIELD_SEARCH   tag du champ perso « recherche »        (défaut search)
 *   EO_FIELD_LABEL    tag du champ perso « libellé lisible »  (défaut search_label)
 */

const DEFAULT_ORIGIN = "https://sudtechjobs.com";

function corsHeaders(origin, allow) {
  const ok = origin === allow || /^http:\/\/localhost(:\d+)?$/.test(origin);
  return {
    "Access-Control-Allow-Origin": ok ? origin : allow,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
  };
}

const json = (obj, status, headers) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });

export default {
  async fetch(request, env) {
    const allow = env.ALLOW_ORIGIN || DEFAULT_ORIGIN;
    const origin = request.headers.get("Origin") || "";
    const ch = corsHeaders(origin, allow);

    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: ch });
    if (request.method !== "POST") return json({ error: "method_not_allowed" }, 405, ch);
    if (!env.EO_API_KEY || !env.EO_LIST_ID) return json({ error: "not_configured" }, 500, ch);

    let body;
    try {
      body = await request.json();
    } catch {
      return json({ error: "bad_json" }, 400, ch);
    }

    const email = String(body.email || "").trim().toLowerCase();
    const search = String(body.search || "").slice(0, 600);
    const label = String(body.search_label || "").slice(0, 200);
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) return json({ error: "bad_email" }, 400, ch);

    const fields = {};
    fields[env.EO_FIELD_SEARCH || "search"] = search;
    fields[env.EO_FIELD_LABEL || "search_label"] = label;

    let r;
    try {
      r = await fetch(`https://api.emailoctopus.com/lists/${env.EO_LIST_ID}/contacts`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${env.EO_API_KEY}`,
        },
        body: JSON.stringify({ email_address: email, fields, tags: ["alert"] }),
      });
    } catch (e) {
      return json({ error: "upstream_unreachable" }, 502, ch);
    }

    if (r.ok) return json({ ok: true }, 200, ch);

    // 409 : le contact existe déjà. On considère l'inscription acquise ; la
    // réconciliation multi-recherche se fera côté alerts.py.
    if (r.status === 409) return json({ ok: true, already: true }, 200, ch);

    const detail = await r.text();
    return json({ error: "provider", status: r.status, detail: detail.slice(0, 500) }, 502, ch);
  },
};
