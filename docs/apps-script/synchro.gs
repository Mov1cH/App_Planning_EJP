/**
 * Synchro planning — prévient le site Planning EJP quand un onglet mensuel du Sheet est modifié.
 *
 * Installation : voir INSTALLATION.md (même dossier).
 * Le secret n'est jamais écrit ici : il est lu dans Paramètres du projet → Propriétés du script
 * (clé SHEET_WEBHOOK_SECRET). L'adresse du site est dans la propriété SITE_URL.
 *
 * Fonction à relier à un déclencheur INSTALLABLE « Lors de la modification » (un simple onEdit ne peut pas
 * appeler d'adresse internet).
 */

var MOIS = ['Janvier', 'Février', 'Mars', 'Avril', 'Mai', 'Juin', 'Juillet', 'Août', 'Septembre', 'Octobre', 'Novembre', 'Décembre'];

function estOngletMensuel_(nom) {
  var m = /^(\S+) (\d{4})$/.exec(nom);
  return !!m && MOIS.indexOf(m[1]) >= 0;
}

function surModification(e) {
  if (!e || !e.range) return;
  var feuille = e.range.getSheet();
  var onglet = feuille.getName();
  if (!estOngletMensuel_(onglet)) return;

  var ligne = e.range.getRow();
  var colonne = e.range.getColumn();
  var derniereLigne = e.range.getLastRow();
  var derniereColonne = e.range.getLastColumn();
  // Seules les zones synchronisées : services (A à D) et effectifs (O à R, lignes 3 à 7).
  var touchePointage = colonne <= 4;
  var toucheEffectifs = derniereColonne >= 15 && colonne <= 18 && derniereLigne >= 3 && ligne <= 7;
  if (!touchePointage && !toucheEffectifs) return;

  var props = PropertiesService.getScriptProperties();
  var secret = props.getProperty('SHEET_WEBHOOK_SECRET');
  var site = props.getProperty('SITE_URL') || 'https://planning-ejp.vercel.app';
  if (!secret) return;

  // Une modification peut couvrir plusieurs lignes (collage) : on prévient pour chaque ligne distincte,
  // en évitant d'envoyer plus d'un appel par ligne toutes les 5 secondes.
  var cache = CacheService.getScriptCache();
  var fin = Math.min(derniereLigne, ligne + 60);
  for (var l = ligne; l <= fin; l++) {
    var cle = onglet + '|' + l;
    if (cache.get(cle)) continue;
    cache.put(cle, '1', 5);
    try {
      UrlFetchApp.fetch(site + '/api/sheet/modification', {
        method: 'post',
        contentType: 'application/json',
        headers: { 'X-Sheet-Secret': secret },
        payload: JSON.stringify({ onglet: onglet, ligne: l, colonne: colonne }),
        muteHttpExceptions: true
      });
    } catch (err) {
      console.warn('Synchro planning : ' + err);
    }
  }
}

/** À lancer une fois à la main pour vérifier que le site répond (renvoie le code HTTP dans les journaux). */
function testerConnexion() {
  var props = PropertiesService.getScriptProperties();
  var site = props.getProperty('SITE_URL') || 'https://planning-ejp.vercel.app';
  var r = UrlFetchApp.fetch(site + '/api/sheet/modification', {
    method: 'post', contentType: 'application/json',
    headers: { 'X-Sheet-Secret': props.getProperty('SHEET_WEBHOOK_SECRET') || '' },
    payload: JSON.stringify({ onglet: 'Novembre 2026', ligne: 53, colonne: 2 }),
    muteHttpExceptions: true
  });
  console.log('Réponse du site : ' + r.getResponseCode() + ' ' + r.getContentText());
}
