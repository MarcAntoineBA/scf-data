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
 * DEUX ORIGINES, PARCE QUE LES DONNÉES SONT RANGÉES EN DEUX ENDROITS
 * Douze fichiers pèsent 93 des 114 Mo du parc. Les versionner à chaque collecte ferait
 * enfler le dépôt sans fin : ils sont publiés en pièces jointes d'une release, remplacées
 * sur place. Les petits fichiers, eux, restent versionnés — leur historique dit quelle
 * valeur a changé et quand. La fonction essaie donc la branche, puis les pièces jointes.
 *
 * TROIS GARDE-FOUS
 * 1. Liste blanche stricte sur le nom de fichier. Sans elle, cette route deviendrait
 *    un proxy ouvert vers un dépôt arbitraire.
 * 2. Repli sur la copie DÉPLOYÉE (env.ASSETS) si les deux origines se taisent : le pire
 *    cas retombe exactement sur le comportement d'avant, jamais sur une page cassée.
 *    Une donnée d'hier vaut mieux qu'un graphe vide.
 * 3. En-tête `x-scf-origin` (branche | piece-jointe | deploiement | erreur) sur chaque
 *    réponse : sans lui, un repli permanent serait indiscernable d'un fonctionnement
 *    normal — exactement le type de panne muette qui a motivé cette migration.
 */

const OWNER = "MarcAntoineBA";
const REPO = "scf-data";

// Petits fichiers : versionnés sur la branche principale.
const BRANCH_SOURCE = `https://raw.githubusercontent.com/${OWNER}/${REPO}/main/cache/`;
// Gros fichiers : pièces jointes de la release « data », remplacées sur place.
const RELEASE_SOURCE = `https://github.com/${OWNER}/${REPO}/releases/download/data/`;

// Uniquement des noms de fichiers plats. Pas de `/`, pas de `..`, pas de requête.
const SAFE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(js|json)$/;

// 60 s : au-delà, on servirait de la donnée périmée sans raison ; en deçà, on
// multiplierait les allers-retours pour des fichiers qui changent au mieux toutes
// les 10 minutes.
const EDGE_TTL = 60;

const TYPES = {
  js: "application/javascript; charset=utf-8",
  json: "application/json; charset=utf-8",
};

async function tryOrigin(url) {
  try {
    const r = await fetch(url, {
      cf: { cacheTtl: EDGE_TTL, cacheEverything: true },
      headers: { "user-agent": "scf-data-proxy" },
      redirect: "follow",
    });
    if (!r.ok) return null;
    const body = await r.arrayBuffer();
    // Un 200 vide serait pire qu'une erreur : il écraserait une page avec du néant.
    return body.byteLength > 0 ? body : null;
  } catch (_) {
    return null;   // origine injoignable : on passe à la suivante
  }
}

export async function onRequestGet(context) {
  const { params, env, request } = context;
  const file = Array.isArray(params.file) ? params.file.join("/") : params.file || "";

  if (!SAFE_NAME.test(file)) {
    return new Response("Nom de fichier refusé", {
      status: 400,
      headers: { "content-type": "text/plain; charset=utf-8", "x-scf-origin": "erreur" },
    });
  }

  const ext = file.slice(file.lastIndexOf(".") + 1);
  const headers = {
    "content-type": TYPES[ext] || "application/octet-stream",
    "cache-control": `public, max-age=${EDGE_TTL}`,
    "access-control-allow-origin": "*",
  };

  for (const [origin, base] of [["branche", BRANCH_SOURCE], ["piece-jointe", RELEASE_SOURCE]]) {
    const body = await tryOrigin(base + file);
    if (body) {
      return new Response(body, {
        headers: { ...headers, "x-scf-origin": origin, "x-scf-bytes": String(body.byteLength) },
      });
    }
  }

  // Repli : la copie embarquée dans le déploiement. C'est le comportement d'avant
  // la migration — donc au pire, on n'a rien perdu.
  const asset = await env.ASSETS.fetch(new URL(`/data/${file}`, request.url));
  if (asset.ok) {
    return new Response(asset.body, { headers: { ...headers, "x-scf-origin": "deploiement" } });
  }

  return new Response("Cache introuvable", {
    status: 404,
    headers: { "content-type": "text/plain; charset=utf-8", "x-scf-origin": "erreur" },
  });
}
