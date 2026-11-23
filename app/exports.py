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
    """Bilan du suivi des cultes (mois, trimestre, semestre ou année), A4 portrait."""
    import io as _io
    from datetime import date as _date
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas

    _polices()
    W, H = A4
    M = 40
    buf = _io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H))
    c.setTitle(f"Statistiques {b.periode.libelle} - {nom_eglise}")
    c.setAuthor(nom_eglise)
    noir, gris, blanc = _hex("#0A0A0A"), _hex("#6B6D73"), _hex("#FFFFFF")
    acc, clair, doux = _hex(couleur["acc"]), _hex(couleur["light"]), _hex(couleur["soft"])
    alerte, alerte_doux = _hex("#C2341B"), _hex("#FBE9E4")
    page = [1]

    def filigrane():
        try:
            if organisation == "icc":
                m = _filigrane("icc-mark-dark.png", couleur["acc"], 1.1); w, h = m.getSize()
                c.drawImage(m, W - 330, -120, width=380, height=380 * h / w, mask="auto")
            else:
                m = _filigrane("mark-dark.png", couleur["acc"], 1.1)
                c.drawImage(m, W - 300, -150, width=340, height=340 * 409 / 281, mask="auto")
        except Exception:  # noqa: BLE001
            pass

    def pied():
        c.setFillColor(gris); c.setFont("Inter-Regular", 8)
        j = _date.today()
        c.drawString(M, 24, f"Édité le {j.day} {MOIS_FR[j.month].lower()} {j.year} avec Planning des cultes")
        c.drawRightString(W - M, 24, f"{nom_eglise} · {b.periode.libelle} · page {page[0]}")

    def nouvelle_page():
        pied(); c.showPage(); page[0] += 1; filigrane()
        c.setFillColor(gris); c.setFont("Inter-SemiBold", 9)
        c.drawString(M, H - M, f"Statistiques · {b.periode.libelle}")
        return H - M - 28

    def place(y, besoin):
        return nouvelle_page() if y - besoin < 48 else y

    def titre_section(y, texte):
        y = place(y, 60)
        c.setFillColor(noir); c.setFont("InterDisplay-ExtraBold", 17)
        c.drawString(M, y, texte)
        return y - 20

    def barres(y, x, largeur, items, valeur_max, suffixe="", nom_w=None):
        """items : (nom, valeur, note). Retourne y final."""
        nom_w = nom_w or largeur * 0.38
        for nom, v, note in items:
            y = place(y, 18)
            c.setFillColor(noir); c.setFont("Inter-SemiBold", 9)
            n = nom
            if stringWidth(n, "Inter-SemiBold", 9) > nom_w - 6:
                while len(n) > 3 and stringWidth(n + "…", "Inter-SemiBold", 9) > nom_w - 6:
                    n = n[:-1]
                n = n.rstrip() + "…"
            c.drawString(x, y, n)
            bx, bw = x + nom_w, largeur - nom_w - 34
            c.setFillColor(doux); c.roundRect(bx, y - 1, bw, 7, 3.5, stroke=0, fill=1)
            if v:
                c.setFillColor(acc); c.roundRect(bx, y - 1, max(7, bw * v / (valeur_max or 1)), 7, 3.5, stroke=0, fill=1)
            c.setFillColor(noir); c.setFont("Inter-Bold", 9.5)
            c.drawRightString(x + largeur, y, f"{v}{suffixe}")
            if note:
                y -= 11
                c.setFillColor(gris); c.setFont("Inter-Regular", 7.5)
                c.drawString(bx, y, note)
            y -= 16
        return y

    filigrane()
    # En-tête
    try:
        logo = ImageReader(str(ASSETS / ("icc-logo.png" if organisation == "icc" else "logo.png")))
        lw, lh = logo.getSize()
        c.drawImage(logo, M, H - M - 34, height=34, width=34 * lw / lh, mask="auto")
    except Exception:  # noqa: BLE001
        pass
    c.setFillColor(gris); c.setFont("Inter-SemiBold", 9)
    c.drawRightString(W - M, H - M - 10, nom_eglise)
    c.drawRightString(W - M, H - M - 23, "Suivi des cultes")

    y = H - M - 86
    c.setFillColor(noir); c.setFont("InterDisplay-ExtraBold", 40)
    c.drawString(M - 1, y, "Bilan")
    c.setFillColor(acc)
    c.drawString(M - 1 + stringWidth("Bilan ", "InterDisplay-ExtraBold", 40), y, b.periode.libelle.lower() if b.periode.type != "mois" else b.periode.libelle)
    y -= 30

    # Chiffres clés
    t = b.total
    cartes = [
        (str(len(b.cultes)), f"culte{'s' if len(b.cultes) > 1 else ''} pointé{'s' if len(b.cultes) > 1 else ''}", False),
        (f"{t.taux if t.taux is not None else '–'} %", f"de présence ({t.presents} sur {t.prevus})", True),
        (str(t.absences), "absences ou remplacements", False),
        (str(b.effectif_moyen if b.effectif_moyen is not None else "–"),
         "personnes en moyenne" + (f" ({b.hommes_moyen} H, {b.femmes_moyen} F"
                                    + (f", {b.ados_moyen} Ados" if b.ados_moyen is not None else "")
                                    + (f", {b.enfants_moyen} Enfants" if b.enfants_moyen is not None else "")
                                    + ")" if b.hommes_moyen is not None else ""), False),
    ]
    gap = 8
    cw = (W - 2 * M - 3 * gap) / 4
    ch = 70
    for i, (val, lib, mis) in enumerate(cartes):
        x = M + i * (cw + gap)
        c.setFillColor(acc if mis else doux)
        c.roundRect(x, y - ch, cw, ch, 12, stroke=0, fill=1)
        c.setFillColor(blanc if mis else (alerte if i == 2 and t.absences else acc)); c.setFont("InterDisplay-ExtraBold", 28)
        c.drawString(x + 12, y - 34, val)
        c.setFillColor(clair if mis else gris); c.setFont("Inter-Regular", 7.5)
        for k, l in enumerate(_lignes(lib, "Inter-Regular", 7.5, cw - 24)[:2]):
            c.drawString(x + 12, y - 50 - k * 9.5, l)
    y -= ch + 30

    # Mois par mois
    if b.periode.type != "mois":
        y = titre_section(y, "Mois par mois")
        mx = max([lm.compteurs.services for lm in b.par_mois] or [1])
        items = []
        for lm in b.par_mois:
            note = (f"{lm.cultes} culte{'s' if lm.cultes > 1 else ''}"
                    + (f" · présence {lm.compteurs.taux} %" if lm.compteurs.taux is not None else "")
                    + (f" · {lm.effectif_moyen} pers. en moyenne" if lm.effectif_moyen else "")) if lm.cultes else "aucun culte pointé"
            items.append((MOIS_FR[lm.mois], lm.compteurs.services, note))
        y = barres(y, M, W - 2 * M, items, mx, " serv.", nom_w=110) - 14

    # Départements et personnes, en deux colonnes
    col = (W - 2 * M - 24) / 2
    y = place(y, 80)
    c.setFillColor(noir); c.setFont("InterDisplay-ExtraBold", 17)
    c.drawString(M, y, "Par département")
    c.drawString(M + col + 24, y, "Services effectués")
    y -= 20
    deps = [(n, v, "") for n, v in b.par_departement]
    pers = [(n, v, "") for n, v in b.effectues]
    md = max([v for _, v, _ in deps] or [1]); mp = max([v for _, v, _ in pers] or [1])
    i = 0
    while i < max(len(deps), len(pers)):
        y = place(y, 18)
        if i < len(deps):
            barres(y, M, col, [deps[i]], md)
        if i < len(pers):
            barres(y, M + col + 24, col, [pers[i]], mp)
        y -= 13.5
        i += 1
    y -= 14

    # Services manqués
    y = titre_section(y, "Services manqués")
    if not b.manques:
        c.setFillColor(gris); c.setFont("Inter-Regular", 9.5)
        c.drawString(M, y, "Aucune absence : toutes les personnes prévues ont servi.")
        y -= 24
    else:
        from .stats import cle_nom
        for nom, n in b.manques:
            details = [f"{d.day} {MOIS_FR[d.month][:4].lower()}. {dep}" + (f" (remplacé par {s})" if s else "")
                       for d, dep, p, s in b.absences_detail if cle_nom(p) == cle_nom(nom)]
            lignes = _lignes(" · ".join(details), "Inter-Regular", 8, W - 2 * M - 150)
            y = place(y, 14 + 10 * len(lignes))
            c.setFillColor(alerte_doux); c.roundRect(M, y - 4, 22, 14, 7, stroke=0, fill=1)
            c.setFillColor(alerte); c.setFont("Inter-Bold", 8.5); c.drawCentredString(M + 11, y, str(n))
            c.setFillColor(noir); c.setFont("Inter-Bold", 9.5); c.drawString(M + 32, y, nom)
            c.setFillColor(gris); c.setFont("Inter-Regular", 8)
            for k, l in enumerate(lignes):
                c.drawString(M + 150, y - k * 10, l)
            y -= 8 + 10 * len(lignes)
        y -= 10

    # Détail par culte
    y = titre_section(y, "Détail par culte")
    colonnes = [("Date", M), ("Effectif", M + 95), ("Hommes", M + 150), ("Femmes", M + 195),
                ("Ados", M + 240), ("Enfants", M + 280), ("Services", M + 335), ("Présence", M + 400),
                ("Absences", M + 465)]
    y = place(y, 30)
    c.setFillColor(gris); c.setFont("Inter-SemiBold", 8)
    for lib, x in colonnes:
        c.drawString(x, y, lib)
    y -= 8
    c.setStrokeColor(clair); c.setLineWidth(0.6); c.line(M, y, W - M, y)
    y -= 13
    for lc in b.cultes:
        y = place(y, 16)
        cp = lc.compteurs
        vals = [f"{lc.date.day} {MOIS_FR[lc.date.month].lower()}",
                "–" if lc.effectif is None else str(lc.effectif),
                "–" if lc.hommes is None else str(lc.hommes), "–" if lc.femmes is None else str(lc.femmes),
                "–" if lc.ados is None else str(lc.ados), "–" if lc.enfants is None else str(lc.enfants),
                str(cp.services), "–" if cp.taux is None else f"{cp.taux} %", str(cp.absences)]
        for (lib, x), v in zip(colonnes, vals):
            c.setFillColor(alerte if lib == "Absences" and cp.absences else noir)
            c.setFont("Inter-Bold" if lib in ("Date", "Effectif") else "Inter-Regular", 9)
            c.drawString(x, y, v)
        y -= 6
        c.setStrokeColor(doux); c.line(M, y, W - M, y)
        y -= 12

    pied()
    c.showPage()
    c.save()
    return buf.getvalue()
