/**
 * auto_refresh.js — Met les données à jour dans la page, sans intervention.
 *
 * LE PROBLÈME QU'IL RÈGLE
 * La collecte tourne désormais toutes les 80 secondes pour les news et la macro. Mais
 * une page charge ses données UNE FOIS, au chargement : sans rafraîchir soi-même, on
 * regarde indéfiniment l'état du moment où on est arrivé. Une donnée fraîche que
 * personne ne voit ne sert à rien.
 *
 * COMMENT IL FONCTIONNE, SANS TOUCHER À UNE SEULE PAGE
 * Les 24 pages qui affichent des données les chargent toutes de la même façon : une
 * balise `<script src="…_cache.js">`. Le script les repère au chargement, surveille
 * leur empreinte côté serveur, et n'agit que si elle change. Aucune page n'a besoin
 * d'être modifiée, et une nouvelle page est couverte d'office.
 *
 * DEUX FAÇONS DE METTRE À JOUR, DE LA PLUS DOUCE À LA PLUS BRUTALE
 *   1. si la page expose `window.__CA_REFRESH__`, on recharge ses données puis on
 *      l'appelle : rien ne bouge à l'écran hormis les chiffres ;
 *   2. sinon on recharge la page, en restituant la position de lecture.
 * La première voie n'existe encore nulle part — c'est délibéré : elle offre un chemin
 * de progression page par page, sans bloquer la mise en service aujourd'hui.
 *
 * CE QU'IL NE FAIT JAMAIS
 * Interrompre quelqu'un. Un rechargement pendant qu'on lit un tableau ou qu'on remplit
 * un champ est plus agaçant qu'une donnée vieille de deux minutes : toute activité
 * récente le reporte au cycle suivant.
 */
(function () {
  "use strict";

  // Les fichiers de données du site portent tous l'une de ces marques.
  const MOTIF = /(_cache|_live|_light|_alert)[\w.-]*\.(js|json)(\?|$)/;

  const INTERVALLE = 60_000;      // fréquence de vérification
  const REPIT = 20_000;           // silence exigé après une action de l'utilisateur
  const CLE_POSITION = "ca_scroll_avant_maj";

  const sources = Array.from(document.scripts)
    .map((s) => s.src)
    .filter((u) => u && MOTIF.test(u));

  if (sources.length === 0) return;   // page sans données : rien à surveiller

  const empreintes = new Map();
  let derniereAction = 0;
  let enCours = false;

  for (const type of ["mousedown", "keydown", "touchstart", "wheel"]) {
    addEventListener(type, () => { derniereAction = Date.now(); }, { passive: true });
  }

  /** Empreinte serveur d'un fichier, SANS le télécharger.
   *
   * En-tête seul, délibérément. Une vérification par simple lecture rapatriait le
   * fichier entier : anodin sur 3 Ko, ruineux sur les 13 Mo de l'historique des
   * actions — 13 Mo par minute et par onglet ouvert. Ici il ne circule que
   * l'empreinte et la taille.
   */
  async function empreinte(url) {
    const r = await fetch(url, { method: "HEAD", cache: "no-store" });
    return r.headers.get("etag")
        || r.headers.get("last-modified")
        || String(r.headers.get("content-length") || "");
  }

  async function verifier() {
    if (enCours || document.hidden) return;
    enCours = true;
    try {
      const changes = [];
      for (const url of sources) {
        let e;
        try {
          e = await empreinte(url);
        } catch (_) {
          continue;                    // réseau capricieux : on retentera au prochain tour
        }
        if (!e) continue;
        if (!empreintes.has(url)) {
          empreintes.set(url, e);      // premier passage : on ne fait que mémoriser
        } else if (empreintes.get(url) !== e) {
          empreintes.set(url, e);
          changes.push(url);
        }
      }
      if (changes.length === 0) return;

      // Ne jamais couper quelqu'un dans son geste. La donnée attendra une minute.
      if (Date.now() - derniereAction < REPIT) return;

      await appliquer(changes);
    } finally {
      enCours = false;
    }
  }

  async function appliquer(changes) {
    if (typeof window.__CA_REFRESH__ === "function") {
      // Voie douce : on recharge les scripts de données, puis la page se redessine.
      await Promise.all(changes.map(rechargerScript));
      try {
        await window.__CA_REFRESH__(changes);
        signaler();
        return;
      } catch (e) {
        // Le redessin a échoué : mieux vaut une page rechargée qu'une page qui ment.
        console.warn("[maj] redessin impossible, rechargement", e);
      }
    }
    try {
      sessionStorage.setItem(CLE_POSITION, String(window.scrollY));
    } catch (_) { /* navigation privée : on perdra la position, sans plus */ }
    location.reload();
  }

  /** Réexécute un script de données pour que ses variables globales soient à jour. */
  function rechargerScript(url) {
    return new Promise((resolve) => {
      const s = document.createElement("script");
      s.src = url.split("#")[0] + (url.includes("?") ? "&" : "?") + "maj=" + Date.now();
      s.onload = s.onerror = () => resolve();
      document.head.appendChild(s);
    });
  }

  /** Signal discret : sans lui, les chiffres changeraient sans explication. */
  function signaler() {
    const d = document.createElement("div");
    d.textContent = "Données mises à jour";
    d.setAttribute("role", "status");
    d.style.cssText = "position:fixed;bottom:18px;right:18px;z-index:99999;"
      + "padding:8px 14px;border-radius:8px;font:500 13px/1.3 system-ui,sans-serif;"
      + "background:rgba(20,22,28,.92);color:#e8eaf0;border:1px solid rgba(255,255,255,.14);"
      + "box-shadow:0 4px 18px rgba(0,0,0,.35);opacity:0;transition:opacity .25s";
    document.body.appendChild(d);
    requestAnimationFrame(() => { d.style.opacity = "1"; });
    setTimeout(() => {
      d.style.opacity = "0";
      setTimeout(() => d.remove(), 400);
    }, 2600);
  }

  // Restitution de la position de lecture après un rechargement automatique.
  try {
    const y = sessionStorage.getItem(CLE_POSITION);
    if (y !== null) {
      sessionStorage.removeItem(CLE_POSITION);
      addEventListener("load", () => window.scrollTo(0, parseInt(y, 10) || 0));
    }
  } catch (_) { /* sans importance */ }

  // Empreintes de départ prises tout de suite, pour que le premier écart réel
  // déclenche une mise à jour dès le cycle suivant plutôt qu'au troisième.
  verifier();
  setInterval(verifier, INTERVALLE);

  // Au retour sur l'onglet, on regarde immédiatement : c'est le moment précis où
  // quelqu'un veut voir des chiffres à jour.
  addEventListener("visibilitychange", () => { if (!document.hidden) verifier(); });
})();
