/**
 * /data/<fichier> — sert les caches de données depuis le dépôt de collecte.
 *
 * POURQUOI CETTE FONCTION EXISTE
 * Avant : la donnée fraîche n'atteignait le site qu'au bout d'une chaîne de quatre
 * maillons (collecte → dépôt local → synchronisation → redéploiement), tous portés
 * par un ordinateur portable. Chaque maillon pouvait publier trop tôt, trop tard, ou
 * pas du tout — et quand la machine dormait, aucun ne bougeait.
 * Après : la collecte écrit dans un dépôt, cette fonction le lit. Une donnée devient
 * publique dès qu'elle est collectée, sans redéployer quoi que ce soit.
 *
 * TROIS GARDE-FOUS
 * 1. Liste blanche stricte sur le nom de fichier. Sans elle, cette route deviendrait
 *    un proxy ouvert vers un dépôt arbitraire.
 * 2. Repli sur la copie DÉPLOYÉE (env.ASSETS) si la source amont ne répond pas :
 *    le pire cas retombe exactement sur le comportement d'avant, jamais sur une page
 *    cassée. Une donnée d'hier vaut mieux qu'un graphe vide.
 * 3. En-tête `x-scf-origin` (github | asset | error) sur chaque réponse : sans lui,
 *    un repli permanent serait indiscernable d'un fonctionnement normal — c'est
 *    exactement le type de panne muette qui a motivé toute cette migration.
 */

// Dépôt de collecte. Volontairement hors du HTML servi : cette valeur ne quitte
// jamais le serveur.
const SOURCE = "https://raw.githubusercontent.com/__OWNER__/scf-data/main/cache/";

// Uniquement des noms de fichiers plats. Pas de `/`, pas de `..`, pas de requête.
const SAFE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(js|json)$/;

// 60 s : au-delà, on republierait de la donnée périmée sans raison ; en deçà, on
// multiplierait les allers-retours pour des fichiers qui changent au mieux toutes
// les 10 minutes.
const EDGE_TTL = 60;

const TYPES = {
  js: "application/javascript; charset=utf-8",
  json: "application/json; charset=utf-8",
};

export async function onRequestGet(context) {
  const { params, env } = context;
  const file = Array.isArray(params.file) ? params.file.join("/") : params.file || "";

  if (!SAFE_NAME.test(file)) {
    return new Response("Nom de fichier refusé", {
      status: 400,
      headers: { "content-type": "text/plain; charset=utf-8", "x-scf-origin": "error" },
    });
  }

  const ext = file.slice(file.lastIndexOf(".") + 1);
  const headers = {
    "content-type": TYPES[ext] || "application/octet-stream",
    "cache-control": `public, max-age=${EDGE_TTL}`,
    "access-control-allow-origin": "*",
  };

  try {
    const upstream = await fetch(SOURCE + file, {
      cf: { cacheTtl: EDGE_TTL, cacheEverything: true },
      headers: { "user-agent": "scf-data-proxy" },
    });

    if (upstream.ok) {
      const body = await upstream.arrayBuffer();
      // Un 200 vide serait pire qu'une erreur : il écraserait une page avec du néant.
      if (body.byteLength > 0) {
        return new Response(body, {
          headers: { ...headers, "x-scf-origin": "github", "x-scf-bytes": String(body.byteLength) },
        });
      }
    }
  } catch (_) {
    // Réseau amont indisponible : on tombe dans le repli ci-dessous.
  }

  // Repli : la copie embarquée dans le déploiement. C'est le comportement d'avant
  // la migration — donc au pire, on n'a rien perdu.
  const asset = await env.ASSETS.fetch(new URL(`/data/${file}`, context.request.url));
  if (asset.ok) {
    return new Response(asset.body, { headers: { ...headers, "x-scf-origin": "asset" } });
  }

  return new Response("Cache introuvable", {
    status: 404,
    headers: { "content-type": "text/plain; charset=utf-8", "x-scf-origin": "error" },
  });
}
