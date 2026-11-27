"""Exports PDF et CSV (Excel) du planning mensuel."""
import csv
import io
import re
import unicodedata


from .config import BASE_DIR
from .services import MOIS_FR, PlanningMois

ASSETS = BASE_DIR / "app" / "pdf_assets"
ASSETS_FONTS = BASE_DIR / "app" / "fonts"


def _latin1(texte: str) -> str:
    """Les polices PDF intégrées ne gèrent que le latin-1 : on remplace le reste proprement."""
    remplacements = {"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", "…": "...", "œ": "oe", "Œ": "OE"}
    for a, b in remplacements.items():
        texte = texte.replace(a, b)
    try:
        texte.encode("latin-1")
        return texte
    except UnicodeEncodeError:
        normalise = unicodedata.normalize("NFKD", texte)
        return normalise.encode("latin-1", "ignore").decode("latin-1")


def nom_fichier(nom_eglise: str, planning: PlanningMois, extension: str) -> str:
    base = unicodedata.normalize("NFKD", nom_eglise).encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-zA-Z0-9]+", "_", base).strip("_").lower() or "eglise"
    return f"planning_{base}_{planning.mois:02d}_{planning.annee}.{extension}"


def _hex(c: str):
    from reportlab.lib.colors import HexColor
    return HexColor(c)


_FONTS_OK = False
_FILIGRANES = {}


def _filigrane(fichier: str, couleur_hex: str, force: float = 2):
    """Logo en filigrane teinté de la couleur de l'église (mis en cache)."""
    cle = (fichier, couleur_hex, force)
    if cle not in _FILIGRANES:
        from PIL import Image
        from reportlab.lib.utils import ImageReader
        base = Image.open(ASSETS / fichier).convert("RGBA")
        r, g, b = (int(couleur_hex.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        alpha = base.getchannel("A").point(lambda a: min(255, int(a * force)))
        teinte = Image.new("RGBA", base.size, (r, g, b, 0))
        teinte.putalpha(alpha)
        _FILIGRANES[cle] = ImageReader(teinte)
    return _FILIGRANES[cle]


def _polices() -> None:
    global _FONTS_OK
    if _FONTS_OK:
        return
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for nom in ("InterDisplay-ExtraBold", "Inter-Regular", "Inter-SemiBold", "Inter-Bold"):
        pdfmetrics.registerFont(TTFont(nom, str(ASSETS_FONTS / f"{nom}.ttf")))
    _FONTS_OK = True


def _lignes(texte: str, police: str, taille: float, largeur: float):
    """Découpe un texte en lignes tenant dans la largeur donnée."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    mots, lignes, courante = texte.split(), [], ""
    for mot in mots:
        essai = f"{courante} {mot}".strip()
        if stringWidth(essai, police, taille) <= largeur or not courante:
            courante = essai
        else:
            lignes.append(courante)
            courante = mot
    if courante:
        lignes.append(courante)
    return lignes or [""]


def planning_pdf(nom_eglise: str, planning: PlanningMois, couleur: dict, prochain=None, organisation: str = "ejp") -> bytes:
    """PDF A4 paysage dans le style de l'application : une colonne par dimanche."""
    import io as _io
    from datetime import date as _date
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas

    _polices()
    W, H = landscape(A4)
    M = 34
    buf = _io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H))
    c.setTitle(f"Planning {MOIS_FR[planning.mois]} {planning.annee} - {nom_eglise}")
    c.setAuthor(nom_eglise)

    noir, gris, filet, papier = _hex("#0A0A0A"), _hex("#6B6D73"), _hex("#DCDDD9"), _hex("#F1F1EE")
    acc, acc_clair = _hex(couleur["acc"]), _hex(couleur["light"])
    acc_doux, acc_fonce = _hex(couleur["soft"]), _hex(couleur["ink"])

    # Logo en filigrane, en bas à droite, teinté de la couleur de l'église
    try:
        if organisation == "icc":
            _m = _filigrane("icc-mark-dark.png", couleur["acc"]); _w, _h = _m.getSize()
            c.drawImage(_m, W - 430, -150, width=480, height=480 * _h / _w, mask="auto")
        else:
            _m = _filigrane("mark-dark.png", couleur["acc"])
            c.drawImage(_m, W - 400, -170, width=470, height=470 * 409 / 281, mask="auto")
    except Exception:  # noqa: BLE001
        pass

    # En-tête
    try:
        from reportlab.lib.utils import ImageReader
        logo = ImageReader(str(ASSETS / ("icc-logo.png" if organisation == "icc" else "logo.png")))
        lw, lh = logo.getSize()
        c.drawImage(logo, M, H - M - 40, height=40, width=40 * lw / lh, mask="auto")
    except Exception:  # noqa: BLE001
        pass
    c.setFillColor(gris); c.setFont("Inter-SemiBold", 9.5)
    c.drawRightString(W - M, H - M - 12, nom_eglise)
    c.drawRightString(W - M, H - M - 26, "Planning des cultes")

    titre_y = H - M - 98
    c.setFillColor(noir); c.setFont("InterDisplay-ExtraBold", 56)
    mois = MOIS_FR[planning.mois]
    c.drawString(M - 2, titre_y, mois)
    c.setFillColor(acc)
    c.drawString(M - 2 + stringWidth(mois + " ", "InterDisplay-ExtraBold", 56), titre_y, str(planning.annee))

    total, manquantes = planning.places_totales, planning.places_manquantes
    resume = f"{len(planning.dimanches)} dimanches, {total - manquantes} places pourvues sur {total}"
    if manquantes:
        resume += f", {manquantes} à pourvoir"

    # Colonnes
    n = max(1, len(planning.dimanches))
    gap = 10
    top = titre_y - 24
    bottom = M + 22
    col_w = (W - 2 * M - gap * (n - 1)) / n
    pad = 14
    inner = col_w - 2 * pad
    roles = planning.lignes
    par_jour = planning.par_dimanche()

    # Hauteur de chaque rôle = la plus grande parmi les dimanches (lignes alignées)
    def mesure(echelle):
        haut = []
        for i, ligne in enumerate(roles):
            h_max = 0
            for cell in ligne.cellules:
                nb = sum(len(_lignes(nom, "Inter-Bold", 11.5 * echelle, inner)) for nom in cell.noms) or 1
                h_role = len(_lignes(ligne.role.nom, "Inter-Regular", 8 * echelle, inner))
                h_max = max(h_max, (h_role * 10 + nb * 14.5) * echelle + 14 * echelle)
            haut.append(h_max)
        return haut

    tete = 72
    echelle = 1.45
    hauteurs = mesure(echelle)
    while tete + sum(hauteurs) + 8 > top - bottom and echelle > 0.6:
        echelle -= 0.05
        hauteurs = mesure(echelle)
    col_h = tete + sum(hauteurs) + 8

    for j, d in enumerate(planning.dimanches):
        x = M + j * (col_w + gap)
        y_haut = top
        actif = prochain is not None and d == prochain
        c.saveState()
        if actif:
            c.setFillColor(acc)
        else:
            c.setFillColor(acc_doux); c.setFillAlpha(0.92)
        c.roundRect(x, y_haut - col_h, col_w, col_h, 14, stroke=0, fill=1)
        c.restoreState()

        texte, secondaire, ligne_sep = (_hex("#FFFFFF"), acc_clair, _hex("#FFFFFF")) if actif else (noir, gris, acc_clair)
        c.setFillColor(_hex("#FFFFFF") if actif else acc); c.setFont("InterDisplay-ExtraBold", 40)
        c.drawString(x + pad - 1, y_haut - 46, f"{d.day:02d}")
        c.setFillColor(secondaire); c.setFont("Inter-SemiBold", 8.5)
        c.drawString(x + pad, y_haut - 61, "Prochain culte" if actif else "Dimanche")

        y = y_haut - tete
        for i, ligne in enumerate(roles):
            cell = ligne.cellules[j]
            c.saveState()
            c.setStrokeColor(ligne_sep); c.setLineWidth(0.6)
            if actif:
                c.setStrokeAlpha(0.3)
            c.line(x + pad, y, x + col_w - pad, y)
            c.restoreState()
            yy = y - 13 * echelle
            c.setFillColor(secondaire); c.setFont("Inter-Regular", 8 * echelle)
            for l in _lignes(ligne.role.nom, "Inter-Regular", 8 * echelle, inner):
                c.drawString(x + pad, yy, l); yy -= 10 * echelle
            yy -= 4 * echelle
            if cell.noms:
                c.setFillColor(texte); c.setFont("Inter-Bold", 11.5 * echelle)
                for nom in cell.noms:
                    for l in _lignes(nom, "Inter-Bold", 11.5 * echelle, inner):
                        c.drawString(x + pad, yy, l); yy -= 14.5 * echelle
            else:
                label = "À pourvoir"
                lw = stringWidth(label, "Inter-Bold", 8.5) + 14
                c.setFillColor(_hex("#FFFFFF") if actif else acc)
                c.roundRect(x + pad, yy - 4, lw, 14, 7, stroke=0, fill=1)
                c.setFillColor(acc if actif else _hex("#FFFFFF")); c.setFont("Inter-Bold", 8.5)
                c.drawString(x + pad + 7, yy, label)
            y -= hauteurs[i]

    c.setFillColor(gris); c.setFont("Inter-Regular", 8)
    aujourd_hui = _date.today()
    c.drawRightString(W - M, M - 4, resume)
    c.drawString(M, M - 4, f"Édité le {aujourd_hui.day} {MOIS_FR[aujourd_hui.month].lower()} {aujourd_hui.year} avec Planning des cultes")
    c.showPage()
    c.save()
    return buf.getvalue()


def planning_csv(planning: PlanningMois) -> bytes:
    sortie = io.StringIO()
    writer = csv.writer(sortie, delimiter=";")
    writer.writerow(["Rôle"] + [d.strftime("%d/%m/%Y") for d in planning.dimanches])
    for ligne in planning.lignes:
        writer.writerow([ligne.role.nom] + [", ".join(c.noms) if c.noms else "-" for c in ligne.cellules])
    return sortie.getvalue().encode("utf-8-sig")


def stats_pdf(nom_eglise: str, b, couleur: dict, organisation: str = "ejp") -> bytes:
    """Bilan du suivi des cultes, A4 portrait, organisé comme le « Tableau de Bord Annuel » du Google Sheet :
    bilan (6 chiffres clés), Top 10 des STAR et des services manqués, services prévus et effectués par mois,
    puis une section par mois (chiffres clés, services effectués et manqués), et le détail par culte."""
    import io as _io
    from datetime import date as _date
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas

    _polices()
    W, H = A4
    M = 36
    buf = _io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H))
    c.setTitle(f"Tableau de bord {b.periode.libelle} - {nom_eglise}")
    c.setAuthor(nom_eglise)
    # Couleurs du Sheet
    sapin, sapin_fonce, clair = _hex("#2E5D50"), _hex("#23473D"), _hex("#A9C7B8")
    vert, terre, label = _hex("#5B8B76"), _hex("#AF7C61"), _hex("#E6EEE9")
    fond, noir, gris, blanc, piste = _hex("#F6F9F7"), _hex("#0A0A0A"), _hex("#6B6D73"), _hex("#FFFFFF"), _hex("#EEF2EF")
    page = [1]
    LARG = W - 2 * M
    periode_nom = {"annee": "annuel", "semestre": "du semestre", "trimestre": "du trimestre"}.get(b.periode.type)

    def fond_page():
        c.setFillColor(fond); c.rect(0, 0, W, H, stroke=0, fill=1)

    def pied():
        c.setFillColor(gris); c.setFont("Inter-Regular", 8)
        j = _date.today()
        c.drawString(M, 20, f"Édité le {j.day} {MOIS_FR[j.month].lower()} {j.year} avec Planning des cultes")
        c.drawRightString(W - M, 20, f"{nom_eglise} · {b.periode.libelle} · page {page[0]}")

    def nouvelle_page():
        pied(); c.showPage(); page[0] += 1; fond_page()
        return H - M

    def place(y, besoin):
        return nouvelle_page() if y - besoin < 40 else y

    def bande(y, texte, besoin=230):
        y = place(y, besoin)  # la bande, ses chiffres clés et le début de ses graphiques restent sur la même page
        c.setFillColor(sapin); c.roundRect(M, y - 24, LARG, 24, 6, stroke=0, fill=1)
        c.setFillColor(blanc); c.setFont("Inter-Bold", 10.5)
        c.drawString(M + 12, y - 16, texte.upper())
        return y - 34

    def tuiles(y, cp, dernier_lib, dernier_val, effectif_total):
        valeurs = [("Services prévus", str(cp.prevus), False), ("Services assurés", str(cp.presents), False),
                   ("Services manqués", str(cp.absences), bool(cp.absences)),
                   ("Taux de présence", f"{cp.taux} %" if cp.taux is not None else "–", False),
                   ("Effectif total", str(effectif_total), False),
                   (dernier_lib, "–" if dernier_val is None else str(dernier_val), False)]
        gap = 6
        tw = (LARG - 5 * gap) / 6
        for i, (lib, val, alerte) in enumerate(valeurs):
            x = M + i * (tw + gap)
            c.setFillColor(blanc); c.roundRect(x, y - 54, tw, 54, 7, stroke=0, fill=1)
            c.setFillColor(label); c.roundRect(x, y - 18, tw, 18, 7, stroke=0, fill=1)
            c.rect(x, y - 18, tw, 8, stroke=0, fill=1)
            c.setFillColor(sapin); c.setFont("Inter-Bold", 7.2)
            c.drawString(x + 7, y - 12.5, lib)
            c.setFillColor(terre if alerte else sapin_fonce); c.setFont("InterDisplay-ExtraBold", 19)
            c.drawString(x + 7, y - 44, val)
        return y - 66

    def nom_coupe(nom, largeur, police="Inter-SemiBold", taille=8.5):
        n = nom.strip()
        if stringWidth(n, police, taille) <= largeur:
            return n
        while len(n) > 3 and stringWidth(n + "…", police, taille) > largeur:
            n = n[:-1]
        return n.rstrip() + "…"

    def carte_barres(x, y, largeur, titre, sous_titre, items, coul, vide):
        """Carte blanche avec barres horizontales. Retourne la hauteur utilisée."""
        h = 40 + max(1, len(items)) * 15 + 8
        c.setFillColor(blanc); c.roundRect(x, y - h, largeur, h, 10, stroke=0, fill=1)
        c.setFillColor(noir); c.setFont("InterDisplay-ExtraBold", 11)
        c.drawString(x + 12, y - 17, titre)
        c.setFillColor(gris); c.setFont("Inter-Regular", 7)
        c.drawString(x + 12, y - 28, nom_coupe(sous_titre, largeur - 24, "Inter-Regular", 7))
        yy = y - 46
        if not items:
            c.setFont("Inter-Regular", 8.5); c.drawString(x + 12, yy, vide)
            return h
        vmax = max(v for _, v in items) or 1
        nom_w = min(110, largeur * 0.36)
        bx, bw = x + 12 + nom_w, largeur - 24 - nom_w - 22
        for nom, v in items:
            c.setFillColor(noir); c.setFont("Inter-SemiBold", 8.5)
            c.drawString(x + 12, yy, nom_coupe(nom, nom_w - 6))
            c.setFillColor(piste); c.roundRect(bx, yy - 1.5, bw, 8, 2, stroke=0, fill=1)
            c.setFillColor(coul); c.roundRect(bx, yy - 1.5, max(3, bw * v / vmax), 8, 2, stroke=0, fill=1)
            c.setFillColor(noir); c.setFont("Inter-Bold", 8.5)
            c.drawRightString(x + largeur - 12, yy, str(v))
            yy -= 15
        return h

    def deux_cartes(y, gauche, droite):
        """gauche/droite : (titre, sous-titre, items, couleur, texte si vide). Coupe en pages si besoin."""
        col = (LARG - 10) / 2
        g_items, d_items = list(gauche[2]), list(droite[2])
        premier = True
        while premier or g_items or d_items:
            dispo = int((y - 40 - 56) // 15)
            if dispo < 3:
                y = nouvelle_page(); continue
            g, d = g_items[:dispo], d_items[:dispo]
            g_items, d_items = g_items[dispo:], d_items[dispo:]
            suite = "" if premier else " (suite)"
            hg = carte_barres(M, y, col, gauche[0] + suite, gauche[1], g, gauche[3], gauche[4]) if (g or premier) else 0
            hd = carte_barres(M + col + 10, y, col, droite[0] + suite, droite[1], d, droite[3], droite[4]) if (d or premier) else 0
            y -= max(hg, hd) + 10
            premier = False
            if g_items or d_items:
                y = nouvelle_page()
        return y

    def colonnes_mois(y):
        h = 190
        y = place(y, h + 10)
        c.setFillColor(blanc); c.roundRect(M, y - h, LARG, h, 10, stroke=0, fill=1)
        c.setFillColor(noir); c.setFont("InterDisplay-ExtraBold", 11)
        c.drawString(M + 12, y - 17, "Services prévus et effectués par mois")
        c.setFillColor(gris); c.setFont("Inter-Regular", 7)
        c.drawString(M + 12, y - 28, "Clair : services planifiés. Foncé : services réellement assurés. Un mois vide n'est pas encore rempli.")
        mois = b.par_mois
        haut = max([max(lm.compteurs.prevus, lm.compteurs.services) for lm in mois] or [1]) or 1
        zone_b, zone_h = y - h + 26, h - 26 - 52
        pas = (LARG - 24) / max(1, len(mois))
        bw = min(16, pas * 0.3)
        c.setStrokeColor(label); c.setLineWidth(0.8); c.line(M + 12, zone_b, W - M - 12, zone_b)
        noms = ["", "Janv.", "Févr.", "Mars", "Avr.", "Mai", "Juin", "Juil.", "Août", "Sept.", "Oct.", "Nov.", "Déc."]
        for i, lm in enumerate(mois):
            cx = M + 12 + pas * (i + 0.5)
            for k, (v, coul) in enumerate(((lm.compteurs.prevus, clair), (lm.compteurs.services, sapin))):
                bh = zone_h * v / haut
                x = cx - bw - 1 if k == 0 else cx + 1
                if v:
                    c.setFillColor(coul); c.rect(x, zone_b, bw, bh, stroke=0, fill=1)
                    c.setFillColor(gris); c.setFont("Inter-Bold", 6.5); c.drawCentredString(x + bw / 2, zone_b + bh + 3, str(v))
            c.setFillColor(gris); c.setFont("Inter-SemiBold", 7.5)
            c.drawCentredString(cx, zone_b - 11, noms[lm.mois])
        return y - h - 12

    # ----- Page 1 -----
    fond_page()
    try:
        logo = ImageReader(str(ASSETS / ("icc-logo.png" if organisation == "icc" else "logo.png")))
        lw, lh = logo.getSize()
        c.drawImage(logo, W - M - 30 * lw / lh, H - M - 30, height=30, width=30 * lw / lh, mask="auto")
    except Exception:  # noqa: BLE001
        pass
    y = H - M - 22
    c.setFillColor(sapin); c.setFont("InterDisplay-ExtraBold", 22)
    titre = f"TABLEAU DE BORD {periode_nom.upper()} {b.periode.annee}" if periode_nom else f"TABLEAU DE BORD · {b.periode.libelle.upper()}"
    c.drawString(M, y, titre)
    c.setFillColor(gris); c.setFont("Inter-SemiBold", 9)
    c.drawString(M, y - 16, f"{nom_eglise} · suivi des cultes")
    y -= 34

    t = b.total
    if periode_nom:
        y = bande(y, f"Bilan {periode_nom}")
        y = tuiles(y, t, "STAR actives", b.stars_actives, b.effectif_total)
        y = deux_cartes(y, ("Top 10 des STAR", "Les 10 personnes qui ont servi le plus souvent sur la période.",
                            b.effectues[:10], sapin, "Aucun service sur la période."),
                        ("Top 10 des services manqués", "Personnes prévues qui ont le plus souvent manqué leur service.",
                         b.manques[:10], terre, "Aucun service manqué sur la période."))
        y = colonnes_mois(y)

    for lm in b.par_mois:
        if not lm.cultes and periode_nom:
            continue
        y = bande(y, f"{MOIS_FR[lm.mois]} {lm.annee}")
        y = tuiles(y, lm.compteurs, "Moyenne par culte", lm.effectif_moyen, lm.effectif_total)
        y = deux_cartes(y, (f"{MOIS_FR[lm.mois]} : services effectués", "Nombre de fois où chaque STAR a servi ce mois-ci.",
                            lm.effectues, vert, "Aucun service pointé ce mois-ci."),
                        (f"{MOIS_FR[lm.mois]} : services manqués", "Nombre de fois où chaque personne prévue n'a pas servi.",
                         lm.manques, terre, "Aucun service manqué ce mois-ci."))

    # ----- Détail par culte (effectifs comme dans le Sheet) -----
    y = bande(y, "Détail par culte", besoin=40 + 17 * min(len(b.cultes), 6))
    colonnes = [("Date", M + 8)] + [(lib, M + 108 + 46 * i) for i, lib in enumerate(
        ("Hommes", "Femmes", "Ados", "Enfants", "Total", "Nouveaux", "Services", "Présence", "Manqués"))]
    c.setFillColor(sapin); c.setFont("Inter-Bold", 8)
    for lib, x in colonnes:
        c.drawString(x, y, lib)
    y -= 6
    for lc in b.cultes:
        y = place(y, 18)
        cp = lc.compteurs
        c.setFillColor(blanc); c.rect(M, y - 13, LARG, 15, stroke=0, fill=1)
        vals = [f"{lc.date.day} {MOIS_FR[lc.date.month].lower()}",
                "–" if lc.hommes is None else str(lc.hommes), "–" if lc.femmes is None else str(lc.femmes),
                "–" if lc.ados is None else str(lc.ados), "–" if lc.enfants is None else str(lc.enfants),
                "–" if lc.effectif is None else str(lc.effectif), "–" if not lc.nouveaux else str(lc.nouveaux),
                str(cp.services), "–" if cp.taux is None else f"{cp.taux} %", str(cp.absences)]
        for (lib, x), v in zip(colonnes, vals):
            c.setFillColor(terre if lib == "Manqués" and cp.absences else noir)
            c.setFont("Inter-Bold" if lib in ("Date", "Total") else "Inter-Regular", 8.5)
            c.drawString(x, y - 9, v)
        y -= 17

    # ----- Nouveaux venus -----
    avec_noms = [lc for lc in b.cultes if lc.nouveaux or lc.nouveaux_noms]
    if avec_noms:
        y -= 10
        y = bande(y, f"Nouveaux venus · {b.nouveaux_total} sur la période", besoin=40 + 16 * min(len(avec_noms), 6))
        for lc in avec_noms:
            noms = _lignes(lc.nouveaux_noms or "Prénoms non renseignés", "Inter-Regular", 8.5, LARG - 150)
            y = place(y, 6 + 12 * len(noms))
            c.setFillColor(noir); c.setFont("Inter-Bold", 8.5)
            c.drawString(M + 8, y - 9, f"{lc.date.day} {MOIS_FR[lc.date.month].lower()}")
            c.setFillColor(sapin); c.drawString(M + 90, y - 9, str(lc.nouveaux or "–"))
            c.setFillColor(noir if lc.nouveaux_noms else gris); c.setFont("Inter-Regular", 8.5)
            for k, l in enumerate(noms):
                c.drawString(M + 120, y - 9 - 12 * k, l)
            y -= 8 + 12 * len(noms)

    pied()
    c.showPage()
    c.save()
    return buf.getvalue()
