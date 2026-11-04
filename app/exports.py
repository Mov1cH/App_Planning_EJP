"""Exports PDF et CSV (Excel) du planning mensuel."""
import csv
import io
import re
import unicodedata

from fpdf import FPDF

from .config import BASE_DIR
from .services import MOIS_FR, PlanningMois

LOGO = BASE_DIR / "static" / "logo.png"


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


def planning_pdf(nom_eglise: str, planning: PlanningMois) -> bytes:
    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_auto_page_break(False)
    pdf.set_margins(12, 12, 12)
    largeur_utile = 297 - 24
    col_role = 62
    nb = max(1, len(planning.dimanches))
    col_jour = (largeur_utile - col_role) / nb
    interligne = 6.5

    def entete_page():
        pdf.add_page()
        if LOGO.exists():
            try:
                pdf.image(str(LOGO), x=12, y=10, h=16)
            except Exception:  # noqa: BLE001
                pass
        pdf.set_xy(12, 10)
        pdf.set_font("Arial", "B", 18)
        pdf.set_text_color(23, 22, 27)
        pdf.cell(largeur_utile, 9, _latin1(f"Planning des cultes - {MOIS_FR[planning.mois]} {planning.annee}"), align="R", ln=1)
        pdf.set_font("Arial", "", 10)
        pdf.set_text_color(95, 92, 102)
        pdf.cell(largeur_utile, 6, _latin1(nom_eglise), align="R", ln=1)
        pdf.set_draw_color(194, 65, 12)
        pdf.set_line_width(0.6)
        pdf.line(12, 30, 12 + largeur_utile, 30)
        pdf.set_xy(12, 34)
        # En-tête du tableau
        pdf.set_line_width(0.2)
        pdf.set_draw_color(226, 223, 216)
        pdf.set_fill_color(23, 22, 27)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Arial", "B", 10)
        pdf.cell(col_role, 10, _latin1("Rôle"), border=1, fill=True, align="L")
        for d in planning.dimanches:
            pdf.cell(col_jour, 10, d.strftime("Dim. %d/%m"), border=1, fill=True, align="C")
        pdf.ln()

    entete_page()

    for index, ligne in enumerate(planning.lignes):
        textes = ["\n".join(c.noms) if c.noms else "-" for c in ligne.cellules]
        nb_lignes = max([1] + [t.count("\n") + 1 for t in textes])
        hauteur = max(11, interligne * nb_lignes + 4)

        if pdf.get_y() + hauteur > 198:
            entete_page()

        x0, y0 = pdf.get_x(), pdf.get_y()
        fond = (247, 246, 242) if index % 2 else (255, 255, 255)
        pdf.set_fill_color(*fond)

        pdf.rect(x0, y0, col_role, hauteur, style="DF")
        pdf.set_text_color(23, 22, 27)
        pdf.set_font("Arial", "B", 10)
        pdf.set_xy(x0 + 2, y0 + (hauteur - interligne) / 2)
        pdf.cell(col_role - 4, interligne, _latin1(ligne.role.nom), align="L")

        pdf.set_font("Arial", "", 10)
        for i, (cell, texte) in enumerate(zip(ligne.cellules, textes)):
            x = x0 + col_role + i * col_jour
            pdf.set_fill_color(*fond)
            pdf.rect(x, y0, col_jour, hauteur, style="DF")
            n = texte.count("\n") + 1
            pdf.set_xy(x, y0 + (hauteur - n * interligne) / 2)
            pdf.set_text_color(*((180, 35, 24) if not cell.noms else (23, 22, 27)))
            pdf.multi_cell(col_jour, interligne, _latin1(texte), border=0, align="C")

        pdf.set_xy(x0, y0 + hauteur)

    sortie = pdf.output(dest="S")
    return sortie.encode("latin-1") if isinstance(sortie, str) else bytes(sortie)


def planning_csv(planning: PlanningMois) -> bytes:
    sortie = io.StringIO()
    writer = csv.writer(sortie, delimiter=";")
    writer.writerow(["Rôle"] + [d.strftime("%d/%m/%Y") for d in planning.dimanches])
    for ligne in planning.lignes:
        writer.writerow([ligne.role.nom] + [", ".join(c.noms) if c.noms else "-" for c in ligne.cellules])
    return sortie.getvalue().encode("utf-8-sig")
