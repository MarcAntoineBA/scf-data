/**
 * reveil — déclenche les cadences de collecte, à la place du planning de GitHub.
 *
 * ⚠ NON DÉPLOYÉ — GARDÉ EN RÉSERVE. À lire avant de s'en servir.
 *
 * Écrit parce que le planning de GitHub Actions n'avait démarré AUCUNE exécution en
 * plus de trois heures. La vraie cause était ailleurs : chaque poussée sur le dépôt
 * réenregistre les plannings, et j'en avais fait une vingtaine d'affilée pendant la
 * migration. Une heure après la dernière, les sept cadences sont parties toutes seules.
 *
 * La leçon vaut d'être gardée : sur un dépôt en cours de modification permanente, le
 * planning ne démarre pas — ce n'est ni une panne ni une limite de la plateforme, c'est
 * une conséquence du va-et-vient. Ne pas conclure à un planning cassé avant d'avoir
 * laissé le dépôt tranquille une heure.
 *
 * Ce Worker reste prêt si le planning venait à s'arrêter pour de bon : il déclenche les
 * cadences depuis une infrastructure déjà utilisée pour le site, sans compte
 * supplémentaire ni machine allumée quelque part. `reveil/installer.sh` le déploie.
 *
 * UN SEUL DÉCLENCHEUR, TOUTES LES CADENCES
 * Il s'exécute toutes les 10 minutes et décide lui-même de ce qui est dû. Sept
 * déclencheurs distincts seraient plus lisibles, mais la logique vivrait dans la
 * configuration au lieu du code — donc invérifiable autrement qu'en attendant.
 *
 * CE QU'IL NE FAIT PAS : empêcher deux exécutions de se chevaucher. C'est déjà garanti
 * côté workflow par son verrou d'exécution ; le refaire ici serait un second endroit
 * où se tromper.
 */

const DEPOT = "MarcAntoineBA/scf-data";

// (fichier du workflow, est-elle due maintenant ?)
// Les heures sont celles d'UTC, comme les expressions qu'elles remplacent.
const CADENCES = [
  ["collect-10min.yml", () => true],
  ["collect-30min.yml", (h, m) => m === 0 || m === 30],
  ["collect-2h.yml", (h, m) => m === 0 && h % 2 === 0],
  ["collect-6h.yml", (h, m) => m === 0 && [1, 7, 13, 19].includes(h)],
  ["collect-12h.yml", (h, m) => m === 0 && [3, 15].includes(h)],
  ["collect-daily.yml", (h, m) => m === 0 && h === 4],
  // Décalée de 10 minutes : sinon elle partirait dans la même minute que la
  // quotidienne, et les deux se disputeraient les mêmes fichiers de sortie.
  ["collect-weekly.yml", (h, m, j) => m === 10 && h === 4 && j === 1],
];

async function declencher(fichier, jeton) {
  const url = `https://api.github.com/repos/${DEPOT}/actions/workflows/${fichier}/dispatches`;
  const r = await fetch(url, {
    method: "POST",
    headers: {
      authorization: `Bearer ${jeton}`,
      accept: "application/vnd.github+json",
      "x-github-api-version": "2022-11-28",
      "content-type": "application/json",
      "user-agent": "reveil-collecte",
    },
    body: JSON.stringify({ ref: "main" }),
  });
  // 204 = accepté. Tout le reste mérite d'être lu : un 404 signifie presque toujours
  // une portée manquante sur le jeton, pas un workflow absent — l'API refuse de
  // révéler ce à quoi on n'a pas droit.
  return { fichier, code: r.status, detail: r.ok ? "" : (await r.text()).slice(0, 200) };
}

export default {
  async scheduled(event, env, ctx) {
    if (!env.GITHUB_TOKEN) {
      console.error("GITHUB_TOKEN absent : aucun déclenchement possible");
      return;
    }
    const maintenant = new Date(event.scheduledTime);
    const h = maintenant.getUTCHours();
    const m = maintenant.getUTCMinutes();
    const j = maintenant.getUTCDay();

    const dues = CADENCES.filter(([, due]) => due(h, m, j)).map(([f]) => f);
    if (dues.length === 0) return;

    const resultats = await Promise.all(dues.map((f) => declencher(f, env.GITHUB_TOKEN)));
    for (const r of resultats) {
      if (r.code === 204) console.log(`déclenché ${r.fichier}`);
      else console.error(`ÉCHEC ${r.fichier} : HTTP ${r.code} ${r.detail}`);
    }
  },

  // Permet de vérifier à la main que le jeton et les droits sont bons, sans attendre
  // une fenêtre. Ne déclenche rien : il se contente de lire.
  async fetch(request, env) {
    if (!env.GITHUB_TOKEN) {
      return new Response("GITHUB_TOKEN absent", { status: 500 });
    }
    const r = await fetch(`https://api.github.com/repos/${DEPOT}/actions/workflows`, {
      headers: {
        authorization: `Bearer ${env.GITHUB_TOKEN}`,
        accept: "application/vnd.github+json",
        "user-agent": "reveil-collecte",
      },
    });
    const d = await r.json();
    const noms = (d.workflows || []).map((w) => `${w.name} — ${w.state}`);
    return new Response(
      `accès au dépôt : HTTP ${r.status}\n${noms.join("\n") || JSON.stringify(d).slice(0, 300)}\n`,
      { headers: { "content-type": "text/plain; charset=utf-8" } },
    );
  },
};
