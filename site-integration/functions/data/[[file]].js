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

async function tryOrigin(url, siPasModifie) {
  try {
    const entetes = { "user-agent": "scf-data-proxy" };
    // On RELAIE la question du navigateur à l'origine. Sans ça, surveiller un fichier
    // de 13 Mo toutes les minutes le retéléchargerait intégralement à chaque fois —
    // pour apprendre qu'il n'a pas bougé. Avec, la réponse est un en-tête vide.
    if (siPasModifie) entetes["if-none-match"] = siPasModifie;

    const r = await fetch(url, {
      cf: { cacheTtl: EDGE_TTL, cacheEverything: true },
      headers: entetes,
      redirect: "follow",
    });

    if (r.status === 304) return { inchange: true, etag: siPasModifie };
    if (!r.ok) return null;

    const body = await r.arrayBuffer();
    // Un 200 vide serait pire qu'une erreur : il écraserait une page avec du néant.
    if (body.byteLength === 0) return null;
    return { body, etag: r.headers.get("etag") || "" };
  } catch (_) {
    return null;   // origine injoignable : on passe à la suivante
  }
}

/**
 * Interrogation SANS corps, pour la surveillance continue des pages.
 *
 * INDISPENSABLE, ET PAS UN CONFORT : la page vérifie ses données toutes les minutes.
 * Sans cette route, chaque vérification retéléchargerait le fichier entier — soit
 * 13 Mo par minute et par onglet ouvert pour l'historique des actions. On demande
 * donc à l'origine la même chose, en en-tête seul : elle répond l'empreinte et la
 * taille, et rien d'autre ne circule.
 */
export async function onRequestHead(context) {
  const { params } = context;
  const file = Array.isArray(params.file) ? params.file.join("/") : params.file || "";
  if (!SAFE_NAME.test(file)) return new Response(null, { status: 400 });

  const ext = file.slice(file.lastIndexOf(".") + 1);
  for (const [origine, base] of [["branche", BRANCH_SOURCE], ["piece-jointe", RELEASE_SOURCE]]) {
    try {
      const r = await fetch(base + file, {
        method: "HEAD",
        cf: { cacheTtl: EDGE_TTL, cacheEverything: true },
        headers: { "user-agent": "scf-data-proxy" },
        redirect: "follow",
      });
      if (!r.ok) continue;
      return new Response(null, {
        headers: {
          "content-type": TYPES[ext] || "application/octet-stream",
          "cache-control": `public, max-age=${EDGE_TTL}`,
          "access-control-allow-origin": "*",
          etag: r.headers.get("etag") || "",
          "content-length": r.headers.get("content-length") || "",
          "last-modified": r.headers.get("last-modified") || "",
          "x-scf-origin": origine,
        },
      });
    } catch (_) { /* origine muette : on essaie la suivante */ }
  }
  return new Response(null, { status: 404, headers: { "x-scf-origin": "erreur" } });
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
    const r = await tryOrigin(base + file, request.headers.get("if-none-match") || "");
    if (!r) continue;

    // Rien n'a changé : on le dit en trois en-têtes, sans corps. C'est ce qui rend la
    // surveillance continue des pages praticable, y compris sur les gros historiques —
    // le navigateur garde sa copie et n'apprend qu'une chose : inutile de retélécharger.
    if (r.inchange) {
      return new Response(null, {
        status: 304,
        headers: {
          etag: r.etag,
          "cache-control": headers["cache-control"],
          "x-scf-origin": origin,
        },
      });
    }

    return new Response(r.body, {
      headers: {
        ...headers,
        ...(r.etag ? { etag: r.etag } : {}),
        "x-scf-origin": origin,
        "x-scf-bytes": String(r.body.byteLength),
      },
    });
  }

  // Repli : la copie embarquée dans le déploiement. C'est le comportement d'avant
  // la migration — donc au pire, on n'a rien perdu.
  //
  // PIÈGE : un chemin absent ne renvoie PAS 404. Cloudflare sert la page d'accueil
  // avec un code 200. Sans le contrôle ci-dessous, on renverrait du HTML étiqueté
  // « application/javascript » : la page ne planterait pas franchement, elle
  // afficherait un graphe vide sans la moindre erreur. Mesuré en local : 14 octets
  // de HTML servis à la place d'un cache de 13 Mo.
  const asset = await env.ASSETS.fetch(new URL(`/data/${file}`, request.url));
  const typeAsset = asset.headers.get("content-type") || "";
  if (asset.ok && !typeAsset.includes("text/html")) {
    return new Response(asset.body, { headers: { ...headers, "x-scf-origin": "deploiement" } });
  }

  // Aucune origine n'a la donnée. On le dit franchement : un 404 se voit dans la
  // console du navigateur, un contenu vide déguisé en succès ne se voit nulle part.

  return new Response("Cache introuvable", {
    status: 404,
    headers: { "content-type": "text/plain; charset=utf-8", "x-scf-origin": "erreur" },
  });
}
