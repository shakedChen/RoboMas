from __future__ import annotations

from flask import (
    Flask, render_template, request, session,
    redirect, url_for, send_file
)
import io
import os
import zipfile
import uuid
import shutil
from pathlib import Path
from werkzeug.utils import secure_filename
from fpdf import FPDF

app = Flask(__name__)
app.secret_key = "robomas-express-il-tax-2024-xk9z"
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB

BASE_DIR = Path(__file__).parent
UPLOAD_FOLDER = BASE_DIR / "uploads"
UPLOAD_FOLDER.mkdir(exist_ok=True)

# ── Document type registry ────────────────────────────────────────────────────
DOC_TYPES = {
    "form_106": {
        "code": "AK005000", "part": "01",
        "label": "טופס 106 — תעודת עובד שכיר",
        "desc": "ניתן לקבל ממעסיק. מפרט הכנסות ומסים שנוכו במקור בשנת המס.",
        "multiple": True, "accept": ".pdf,.jpg,.jpeg,.png",
        "icon": "🏢",
    },
    "form_867": {
        "code": "AK014800", "part": "01",
        "label": "טופס 867 — דוח שנתי מבנק / ברוקר",
        "desc": "מפרט את כל פעולות ההשקעה, הריבית, הדיבידנד והמסים שנוכו.",
        "multiple": True, "accept": ".pdf,.jpg,.jpeg,.png",
        "icon": "📊",
    },
    "form_867_annex": {
        "code": "AK014800", "part": "03",
        "label": "נספח 867 — פירוט פעולות (אם קיים)",
        "desc": "נספח מפורט של טופס 867 שהברוקר / הבנק מספקים בנפרד.",
        "multiple": True, "accept": ".pdf,.jpg,.jpeg,.png",
        "icon": "📋",
    },
    "bank_account": {
        "code": "AK008400", "part": "01",
        "label": "אישור ניהול חשבון",
        "desc": "מסמך מהבנק המאשר שם בעל החשבון, מספר חשבון וסניף.",
        "multiple": False, "accept": ".pdf,.jpg,.jpeg,.png",
        "icon": "🏦",
    },
    "form_1042": {
        "code": "AF134400", "part": "03",
        "label": "טופס 1042-S — ניכוי מס במקור זר",
        "desc": "ניתן מברוקר זר (Interactive Brokers וכד׳). מפרט מס שנוכה בחו\"ל.",
        "multiple": True, "accept": ".pdf",
        "icon": "🌐",
    },
    "broker_trades": {
        "code": "AK016000", "part": "01",
        "label": "דוח פעולות ברוקר",
        "desc": "דוח CSV / PDF של כל הפעולות שבוצעו בשנת המס.",
        "multiple": True, "accept": ".pdf,.csv",
        "icon": "📈",
    },
    "annual_prev": {
        "code": "AK000900", "part": "01",
        "label": "דוח שנתי קודם / אישור הגשה",
        "desc": "הדוח השנתי שהוגש בשנה הקודמת, או אישור הגשתו.",
        "multiple": True, "accept": ".pdf",
        "icon": "🗂️",
    },
    "bituach_leumi": {
        "code": "AK010200", "part": "01",
        "label": "אישור תשלומים — ביטוח לאומי",
        "desc": "ניתן דרך אתר ביטוח לאומי. מפרט קצבאות ותשלומים שהתקבלו.",
        "multiple": False, "accept": ".pdf",
        "icon": "🛡️",
    },
    "military_cert": {
        "code": "AK013500", "part": "01",
        "label": "תעודת שחרור / אישור שירות צבאי",
        "desc": "תעודת שחרור מצה\"ל או אישור שירות מילואים.",
        "multiple": False, "accept": ".pdf,.jpg,.jpeg,.png",
        "icon": "🎖️",
    },
    "pension_withdrawal": {
        "code": "AK011000", "part": "01",
        "label": "אישור משיכת קופה / פנסיה",
        "desc": "מסמך מקרן הפנסיה או קופת הגמל על משיכת כספים.",
        "multiple": True, "accept": ".pdf",
        "icon": "💰",
    },
    "rent_income": {
        "code": "AK012000", "part": "01",
        "label": "אסמכתאות הכנסות שכירות",
        "desc": "חוזה שכירות, קבלות דמי שכירות, או דיווח על הכנסות שכירות.",
        "multiple": True, "accept": ".pdf,.jpg,.jpeg,.png",
        "icon": "🏠",
    },
}

# ── Income → required documents ───────────────────────────────────────────────
INCOME_DOC_MAP = {
    "salary":             ["form_106", "bank_account"],
    "equity":             ["form_867", "bank_account"],
    "self_employed":      ["bank_account"],
    "capital_israel":     ["form_867", "bank_account"],
    "capital_foreign":    ["form_867", "form_1042", "broker_trades"],
    "crypto":             ["form_867", "bank_account"],
    "bituach_leumi":      ["bituach_leumi"],
    "corona_grant":       [],
    "rent":               ["rent_income", "bank_account"],
    "pension_withdrawal": ["pension_withdrawal"],
    "directors":          ["bank_account"],
    "foreign_income":     ["form_867", "bank_account"],
    "dividends_private":  ["bank_account"],
    "real_estate_sale":   ["bank_account"],
    "social_loans":       ["form_867", "bank_account"],
    "private_company":    ["bank_account"],
    "family_company":     ["bank_account"],
    "real_estate_fund":   ["bank_account"],
    "debt_investments":   ["form_867", "bank_account"],
    "lottery":            ["bank_account"],
    "electricity_sale":   ["bank_account"],
    "other_income":       ["bank_account"],
    "pension_annuity":    ["form_106", "bank_account"],
    "separation_grant":   ["pension_withdrawal"],
    "provident_withdrawal": ["pension_withdrawal"],
    "training_fund":      ["pension_withdrawal"],
    "pension_illegal":    ["pension_withdrawal"],
    "severance_taxable":  ["pension_withdrawal"],
}

DEDUCTION_DOC_MAP = {
    "military":         ["military_cert"],
    "prev_report":      ["annual_prev"],
}

TAXFILE_TYPES = [
    ("91", "סוג תיק 91 — החזר מס — לא חייב בהגשה"),
    ("92", "סוג תיק 92 — בעל הכנסה ממשכורת / קצבה שחייב בהגשה"),
    ("93", "סוג תיק 93 — נישום עם מספר מקורות הכנסה שחייב בהגשה"),
    ("40", "סוג תיק 40 — עוסק יחיד / זעיר"),
    ("41", "סוג תיק 41 — חייב חד צידית פשוטה"),
    ("42", "סוג תיק 42 — חייב חד צידית מורכבת - מצטבר"),
    ("43", "סוג תיק 43 — חקלאי ללא כפולה"),
    ("52", "סוג תיק 52 — חד צידית מורכבת - בסיס מזומן"),
    ("53", "סוג תיק 53 — חייב כפולה או מנהל כפולה"),
    ("94", "סוג תיק 94 — שכיר עם הכנסות רכוש / ריבית / בעל מניות"),
    ("95", "סוג תיק 95 — שכיר עם הכנסות משכ\"ד"),
    ("96", "סוג תיק 96 — תיק מיועד לסגירה עם יתרות"),
    ("97", "סוג תיק 97 — תיק שכיר במקרים מיוחדים"),
    ("5",  "סוג תיק 5  — עיסקת אקראי"),
    ("20", "סוג תיק 20 — תיק ניהול נישום מחזור מזערי"),
    ("30", "סוג תיק 30 — תיק בעל מניות / בעל שליטה"),
]


# ── MIME → extension fallback ─────────────────────────────────────────────────
_MIME_TO_EXT = {
    "application/pdf":  ".pdf",
    "image/jpeg":       ".jpg",
    "image/png":        ".png",
    "image/gif":        ".gif",
    "image/tiff":       ".tiff",
    "text/csv":         ".csv",
    "application/octet-stream": ".pdf",  # treat unknown binary as PDF
}

# ── Hebrew font path ───────────────────────────────────────────────────────────
_HEBREW_FONT = "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"

# ── Summary cover-page PDF ────────────────────────────────────────────────────
def _build_summary_pdf(
    personal: dict,
    year: int | str,
    entries: list[tuple[str, str, str]],
) -> bytes:
    """Return bytes of a Hebrew cover-page PDF listing all uploaded documents."""
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_font("HEB", "", _HEBREW_FONT)
    pdf.add_font("HEB", "B", _HEBREW_FONT)

    # ── Cover page ────────────────────────────────────────────────────────────
    pdf.add_page()

    # Deep blue header bar
    pdf.set_fill_color(10, 36, 99)
    pdf.rect(0, 0, 210, 42, "F")

    # Logo text
    pdf.set_font("HEB", "B", 24)
    pdf.set_text_color(255, 255, 255)
    pdf.set_xy(0, 10)
    pdf.cell(210, 10, "רובומס", align="C")

    pdf.set_font("HEB", "", 12)
    pdf.set_xy(0, 24)
    pdf.cell(210, 8, "צרופות לטעינה — מס הכנסה", align="C")

    pdf.set_text_color(30, 30, 30)

    # Year pill
    pdf.set_fill_color(230, 240, 255)
    pdf.set_draw_color(100, 140, 210)
    pdf.set_line_width(0.5)
    pdf.rect(75, 50, 60, 12, "FD")
    pdf.set_font("HEB", "B", 14)
    pdf.set_text_color(10, 36, 99)
    pdf.set_xy(75, 52)
    pdf.cell(60, 8, f"שנת מס {year}", align="C")

    pdf.set_text_color(30, 30, 30)
    pdf.set_y(72)

    # Personal info block
    name   = personal.get("name", "").strip()
    id_num = personal.get("id_number", "").strip()
    pdf.set_font("HEB", "B", 11)
    if name:
        pdf.set_x(0)
        pdf.cell(190, 8, name, align="R")
        pdf.ln(7)
    if id_num:
        pdf.set_x(0)
        pdf.cell(190, 7, f"ת.ז.: {id_num}", align="R")
        pdf.ln(7)

    pdf.ln(6)

    # Section title
    pdf.set_fill_color(10, 36, 99)
    pdf.set_text_color(255, 255, 255)
    pdf.set_x(15)
    pdf.set_font("HEB", "B", 11)
    pdf.cell(180, 9, "רשימת המסמכים שצורפו", fill=True, align="R")
    pdf.ln(12)

    pdf.set_text_color(30, 30, 30)

    if entries:
        # Table header
        pdf.set_fill_color(240, 245, 255)
        pdf.set_font("HEB", "B", 9)
        pdf.set_x(15)
        pdf.cell(90, 8, "שם קובץ ב-ZIP", border=1, fill=True, align="C")
        pdf.cell(90, 8, "סוג מסמך", border=1, fill=True, align="C")
        pdf.ln()

        # Table rows
        pdf.set_font("HEB", "", 9)
        for i, (zip_name, label, _orig) in enumerate(entries):
            fill = i % 2 == 0
            pdf.set_fill_color(250, 252, 255) if fill else pdf.set_fill_color(255, 255, 255)
            pdf.set_x(15)
            pdf.cell(90, 7, zip_name, border=1, fill=fill, align="L")
            pdf.cell(90, 7, label,    border=1, fill=fill, align="R")
            pdf.ln()
    else:
        pdf.set_font("HEB", "", 11)
        pdf.set_x(0)
        pdf.cell(190, 10, "לא הועלו מסמכים.", align="C")
        pdf.ln()

    # Footer
    pdf.set_y(-25)
    pdf.set_draw_color(180, 180, 180)
    pdf.set_line_width(0.3)
    pdf.line(15, pdf.get_y(), 195, pdf.get_y())
    pdf.ln(3)
    pdf.set_font("HEB", "", 8)
    pdf.set_text_color(120, 120, 120)
    pdf.set_x(0)
    pdf.cell(190, 5, "www.robomas.co.il  |  מסמך זה הופק אוטומטית על ידי מערכת רובומס", align="C")

    return bytes(pdf.output())


# ── Helpers ───────────────────────────────────────────────────────────────────
def get_session_upload_dir() -> Path:
    if "sid" not in session:
        session["sid"] = str(uuid.uuid4())
    path = UPLOAD_FOLDER / session["sid"]
    path.mkdir(exist_ok=True)
    return path


def determine_required_docs() -> list[str]:
    required: set[str] = set()
    all_incomes = (
        session.get("income_general", [])
        + session.get("income_capital", [])
        + session.get("income_pension", [])
        + session.get("income_other", [])
    )
    for income in all_incomes:
        required.update(INCOME_DOC_MAP.get(income, []))
    for deduction in session.get("deductions", []):
        required.update(DEDUCTION_DOC_MAP.get(deduction, []))
    # Always need bank account for refund goal
    if session.get("goal") in ("refund", "annual"):
        required.add("bank_account")
    # Return in DOC_TYPES order for consistent display
    return [k for k in DOC_TYPES if k in required]


_STEP_MAP = {
    "step_goal":           (1,  "מטרה"),
    "step_year":           (2,  "בחירת שנה"),
    "step_personal":       (3,  "פרטים אישיים"),
    "step_family":         (4,  "מצב משפחתי"),
    "step_taxfile":        (5,  "תיק מס הכנסה"),
    "step_income_general": (6,  "הכנסות כלליות"),
    "step_income_capital": (7,  "השקעות והון"),
    "step_income_pension": (8,  "קופות וקרנות"),
    "step_income_other":   (9,  "הכנסות נוספות"),
    "step_deductions":     (10, "זיכויים"),
    "step_documents":      (11, "מסמכים"),
    "step_complete":       (12, "סיום"),
}
_TOTAL_STEPS = 11  # goal page is pre-funnel; real funnel is steps 2-12


@app.context_processor
def inject_step_info():
    step, label = _STEP_MAP.get(request.endpoint, (0, ""))
    year = session.get("year", "")
    return {
        "current_step":  step,
        "total_steps":   _TOTAL_STEPS,
        "step_label":    label,
        "session_year":  year,
        "personal_name": (session.get("personal") or {}).get("first_name", ""),
    }


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET", "POST"])
def step_goal():
    if request.method == "POST":
        session["goal"] = request.form["goal"]
        return redirect(url_for("step_year"))
    return render_template("step_goal.html")


@app.route("/year", methods=["GET", "POST"])
def step_year():
    if request.method == "POST":
        session["year"] = int(request.form["year"])
        return redirect(url_for("step_personal"))
    return render_template(
        "step_year.html",
        years=list(range(2016, 2026)),
        selected=session.get("year", 2024),
    )


@app.route("/personal", methods=["GET", "POST"])
def step_personal():
    if request.method == "POST":
        session["personal"] = {
            "first_name": request.form["first_name"],
            "last_name":  request.form["last_name"],
            "dob":        request.form["dob"],
            "gender":     request.form["gender"],
            "id_number":  request.form["id_number"],
            "phone":      request.form.get("phone", ""),
            "email":      request.form.get("email", ""),
        }
        return redirect(url_for("step_family"))
    year = session.get("year", "")
    return render_template(
        "step_personal.html",
        data=session.get("personal", {}),
        year=year,
    )


@app.route("/family", methods=["GET", "POST"])
def step_family():
    if request.method == "POST":
        has_children = request.form.get("has_children", "no")
        session["family"] = {
            "status":           request.form["status"],
            "foreign_resident": request.form.get("foreign_resident", "no"),
            "has_children":     has_children,
            "children_count":   int(request.form.get("children_count", 0)) if has_children == "yes" else 0,
        }
        return redirect(url_for("step_taxfile"))
    year = session.get("year", "")
    return render_template(
        "step_family.html",
        data=session.get("family", {}),
        year=year,
    )


@app.route("/taxfile", methods=["GET", "POST"])
def step_taxfile():
    if request.method == "POST":
        session["taxfile"] = {
            "has_file":      request.form.get("has_file", "no"),
            "open_now":      request.form.get("open_now", "no"),
            "file_number":   request.form.get("file_number", ""),
            "file_type":     request.form.get("file_type", "91"),
        }
        return redirect(url_for("step_income_general"))
    year = session.get("year", "")
    personal = session.get("personal", {})
    return render_template(
        "step_taxfile.html",
        data=session.get("taxfile", {}),
        taxfile_types=TAXFILE_TYPES,
        personal=personal,
        year=year,
    )


@app.route("/income/general", methods=["GET", "POST"])
def step_income_general():
    if request.method == "POST":
        session["income_general"] = request.form.getlist("items")
        return redirect(url_for("step_income_capital"))
    year = session.get("year", "")
    return render_template(
        "step_income_general.html",
        selected=session.get("income_general", []),
        year=year,
    )


@app.route("/income/capital", methods=["GET", "POST"])
def step_income_capital():
    if request.method == "POST":
        session["income_capital"] = request.form.getlist("items")
        return redirect(url_for("step_income_pension"))
    year = session.get("year", "")
    return render_template(
        "step_income_capital.html",
        selected=session.get("income_capital", []),
        year=year,
    )


@app.route("/income/pension", methods=["GET", "POST"])
def step_income_pension():
    if request.method == "POST":
        session["income_pension"] = request.form.getlist("items")
        return redirect(url_for("step_income_other"))
    year = session.get("year", "")
    return render_template(
        "step_income_pension.html",
        selected=session.get("income_pension", []),
        year=year,
    )


@app.route("/income/other", methods=["GET", "POST"])
def step_income_other():
    if request.method == "POST":
        session["income_other"] = request.form.getlist("items")
        return redirect(url_for("step_deductions"))
    year = session.get("year", "")
    return render_template(
        "step_income_other.html",
        selected=session.get("income_other", []),
        year=year,
    )


@app.route("/deductions", methods=["GET", "POST"])
def step_deductions():
    if request.method == "POST":
        session["deductions"] = request.form.getlist("items")
        return redirect(url_for("step_documents"))
    year = session.get("year", "")
    family = session.get("family", {})
    # Auto-seed deductions from family answers (only on first visit)
    selected = session.get("deductions", None)
    if selected is None:
        auto = []
        if family.get("has_children") == "yes":
            auto.append("single_parent" if family.get("status") in ("single", "divorced", "widowed") else "children")
        selected = auto
    return render_template(
        "step_deductions.html",
        selected=selected,
        year=year,
        family=family,
    )


@app.route("/documents", methods=["GET", "POST"])
def step_documents():
    required_docs = determine_required_docs()
    upload_dir = get_session_upload_dir()

    if request.method == "POST":
        for doc_key in required_docs:
            files = request.files.getlist(f"doc_{doc_key}")
            doc_dir = upload_dir / doc_key
            doc_dir.mkdir(exist_ok=True)
            seq = 1
            for f in files:
                if f and f.filename:
                    ext = Path(secure_filename(f.filename)).suffix.lower()
                    # Fall back to extension derived from content-type if none found
                    if not ext:
                        ct = (f.content_type or "").split(";")[0].strip()
                        ext = _MIME_TO_EXT.get(ct, ".pdf")
                    f.save(doc_dir / f"{seq:03d}{ext}")
                    seq += 1
        return redirect(url_for("step_complete"))

    docs_info = [(k, DOC_TYPES[k]) for k in required_docs if k in DOC_TYPES]
    year = session.get("year", "")
    return render_template(
        "step_documents.html",
        docs_info=docs_info,
        year=year,
    )


@app.route("/complete")
def step_complete():
    year = session.get("year", "")
    personal = session.get("personal", {})
    return render_template(
        "step_complete.html",
        year=year,
        personal=personal,
    )


@app.route("/download")
def download_zip():
    upload_dir = get_session_upload_dir()
    personal = session.get("personal", {})
    year = session.get("year", 2024)

    user_id = personal.get("id_number", "000000000").strip()
    padded_id = user_id.zfill(10)

    # Collect uploaded entries (for summary manifest)
    entries: list[tuple[str, str, str]] = []   # (zip_name, label, original_name)

    zip_path = upload_dir / "output.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for doc_key, doc_info in DOC_TYPES.items():
            doc_dir = upload_dir / doc_key
            if not doc_dir.exists():
                continue
            code = doc_info["code"]
            part = doc_info["part"]
            files = sorted(f for f in doc_dir.iterdir() if f.is_file())
            for seq, fpath in enumerate(files, 1):
                ext = fpath.suffix.lower() or ".pdf"
                new_name = f"{code}_{part}_{seq:03d}_{padded_id}{year}{ext}"
                zf.write(str(fpath), new_name)
                entries.append((new_name, doc_info["label"], fpath.name))

        # Generate and add the summary cover PDF
        summary_pdf = _build_summary_pdf(personal, year, entries)
        summary_name = f"קובץ_סיכום_01_001_{padded_id}{year}.pdf"
        zf.writestr(summary_name, summary_pdf)

    return send_file(
        str(zip_path),
        as_attachment=True,
        download_name="צרופות לטעינה מס הכנסה.zip",
        mimetype="application/zip",
    )


@app.route("/reset")
def reset():
    sid = session.get("sid", "")
    if sid:
        d = UPLOAD_FOLDER / sid
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    session.clear()
    return redirect(url_for("step_goal"))


if __name__ == "__main__":
    app.run(debug=True, port=5431)
