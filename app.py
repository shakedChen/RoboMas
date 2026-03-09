from __future__ import annotations

import io
import os
import shutil
import uuid
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from flask import (
    Flask,
    abort,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
    send_file,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from fpdf import FPDF

app = Flask(__name__)
app.secret_key = os.environ.get(
    "SECRET_KEY",
    "robomas-express-il-tax-2024-xk9z",  # dev fallback only; override in production
)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB

# ── Database configuration (SQLAlchemy → parameterized queries, mitigates SQL injection) ──
BASE_DIR = Path(__file__).parent
default_sqlite_path = BASE_DIR / "robomas.db"
database_url = os.environ.get("DATABASE_URL", f"sqlite:///{default_sqlite_path}")
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# ── Upload storage (ephemeral per-session; cleaned aggressively) ──────────────────────────
UPLOAD_FOLDER = BASE_DIR / "uploads"
UPLOAD_FOLDER.mkdir(exist_ok=True)

# ── Auth / stats models ───────────────────────────────────────────────────────


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_login_at = db.Column(db.DateTime)
    last_ip = db.Column(db.String(64))

    visits = db.relationship("Visit", back_populates="user", lazy="dynamic")

    def set_password(self, raw: str) -> None:
        # Use PBKDF2 instead of scrypt to support Python builds without hashlib.scrypt
        self.password_hash = generate_password_hash(raw, method="pbkdf2:sha256")

    def check_password(self, raw: str) -> bool:
        return check_password_hash(self.password_hash, raw)


class Visit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    path = db.Column(db.String(400), nullable=False)
    method = db.Column(db.String(10), nullable=False)
    ip = db.Column(db.String(64))
    user_agent = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    user = db.relationship("User", back_populates="visits")


def _get_client_ip() -> str:
    """Best-effort IP, aware of common proxy headers (Render/NGINX)."""
    # X-Forwarded-For may contain a comma-separated list; take first public entry.
    xff = request.headers.get("X-Forwarded-For", "") or request.headers.get(
        "X-Real-IP", ""
    )
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or ""


def _ensure_initial_admin() -> None:
    """Idempotently ensure the configured seed admin exists."""
    seed_username = "shutzibutzi"
    seed_password = "gsdgsdg#@$@#23dfs!"
    existing = User.query.filter_by(username=seed_username).first()
    if existing:
        if not existing.is_admin:
            existing.is_admin = True
            db.session.commit()
        return
    admin = User(
        username=seed_username,
        is_admin=True,
        created_at=datetime.utcnow(),
    )
    admin.set_password(seed_password)
    db.session.add(admin)
    db.session.commit()


with app.app_context():
    db.create_all()
    _ensure_initial_admin()


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


INCOME_LABELS = {
    # General income
    "salary": "משכורת",
    "equity": "תגמול הוני ממקום עבודה",
    "self_employed": "עצמאי / עוסק",
    "corona_grant": "מענקי קורונה",
    "bituach_leumi": "ביטוח לאומי",
    # Capital / investments
    "capital_israel": "שוק ההון הישראלי",
    "capital_foreign": "ברוקר זר",
    "crypto": "קריפטו",
    "social_loans": "הלוואות חברתיות",
    "rent": "שכירות נדל\"ן",
    "debt_investments": "עסקאות חוב",
    "real_estate_fund": "קרן נדל\"ן (REIT)",
    "private_company": "החזקה בחברה פרטית",
    "real_estate_sale": "מכירת נדל\"ן ללא פטור",
    "family_company": "חברה משפחתית",
    # Pension / funds
    "separation_grant": "פריסת מענק פרישה",
    "severance_taxable": "פיצויים חייבים במס",
    "provident_withdrawal": "משיכת קופת גמל להשקעה",
    "pension_illegal": "משיכת גמולי פנסיה שלא כדין",
    "training_fund": "קרן השתלמות לא פטורה",
    "pension_annuity": "קצבת פנסיה / נכות",
    # Other income
    "directors": "שכר דירקטורים",
    "lottery": "הגרלות ופרסים",
    "foreign_income": "הכנסות מחו\"ל",
    "dividends_private": "דיבידנד מחברה פרטית",
    "electricity_sale": "מכירת חשמל (סולרי)",
    "other_income": "הכנסות אחרות",
}

_INCOME_GROUP_TITLES = {
    "income_general": "הכנסות כלליות",
    "income_capital": "השקעות ושוק ההון",
    "income_pension": "משיכות מקופות וקרנות",
    "income_other": "הכנסות נוספות",
}

DEDUCTION_LABELS = {
    "children": "זיכוי בשל ילדים",
    "single_parent": "הורה יחידני",
    "pension_fund": "הפקדה לקרן השתלמות / פנסיה כעצמאי",
    "work_loss": "ביטוח חיים / אובדן כושר עבודה",
    "donations": "תרומות (סעיף 46)",
    "kibbutz": "חבר/ת קיבוץ",
    "aliya": "עולה חדש/ה או תושב/ת חוזר/ת",
    "abroad_payments": "תשלומים לביטוח לאומי כעצמאי/ת",
    "military": "חייל/ת משוחרר/ת / שירות לאומי",
    "disability": "נכה / עיוור",
    "study": "סיום לימודים אקדמיים / לימודי מקצוע",
    "prev_report": "הגשת דוח שנתי בשנה קודמת",
}

GOAL_LABELS = {
    "annual": "הגשת דו\"ח שנתי מלא",
    "refund": "בקשה להחזר מס בלבד",
}


def _build_report_context() -> dict:
    """Collects the answers into a summary structure for the UI / TXT export."""
    year = session.get("year", "")
    personal = session.get("personal", {}) or {}
    goal = session.get("goal", "")
    family = session.get("family", {}) or {}

    income_summary: list[dict] = []
    for key in ("income_general", "income_capital", "income_pension", "income_other"):
        selected = session.get(key, []) or []
        if not selected:
            continue
        income_summary.append(
            {
                "group_key": key,
                "group_title": _INCOME_GROUP_TITLES.get(key, ""),
                "items": [INCOME_LABELS.get(v, v) for v in selected],
            }
        )

    deductions_keys = session.get("deductions", []) or []
    deductions_labels = [DEDUCTION_LABELS.get(v, v) for v in deductions_keys]

    required_doc_keys = determine_required_docs()
    docs_summary = [
        {"key": k, "label": DOC_TYPES[k]["label"], "code": DOC_TYPES[k]["code"]}
        for k in required_doc_keys
        if k in DOC_TYPES
    ]

    insights: list[str] = []
    if goal in GOAL_LABELS:
        insights.append(GOAL_LABELS[goal])
    if family.get("has_children") == "yes":
        cnt = family.get("children_count") or 0
        if cnt:
            insights.append(f"סימנתם {cnt} ילדים — ייתכנו נקודות זיכוי משמעותיות בגין ילדים.")
        else:
            insights.append("סימנתם שיש ילדים — ייתכנו נקודות זיכוי בגין ילדים.")
    if "capital_foreign" in session.get("income_capital", []):
        insights.append("דיווחתם על השקעות דרך ברוקר זר — לרוב נדרש לצרף טופסי 1042-S ו-867.")
    if "rent" in session.get("income_capital", []):
        insights.append("סימנתם הכנסות משכירות — כדאי לבדוק מסלולי מיסוי אפשריים (פטור, 10%, רגיל).")
    if "donations" in deductions_keys:
        insights.append("סימנתם תרומות לפי סעיף 46 — ייתכן זיכוי מס בגין התרומות.")
    if "military" in deductions_keys:
        insights.append("סימנתם חייל/ת משוחרר/ת או שירות לאומי — ייתכן זיכוי חד-פעמי לשנים הראשונות לאחר השחרור.")

    return {
        "year": year,
        "personal": personal,
        "goal": goal,
        "family": family,
        "income_summary": income_summary,
        "deductions_labels": deductions_labels,
        "docs_summary": docs_summary,
        "insights": insights,
    }

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


def _cleanup_old_uploads(max_age_hours: int = 6) -> None:
    """Best-effort removal of stale upload directories (no long-term storage)."""
    cutoff = datetime.utcnow() - timedelta(hours=max_age_hours)
    try:
        for child in UPLOAD_FOLDER.iterdir():
            if not child.is_dir():
                continue
            try:
                mtime = datetime.utcfromtimestamp(child.stat().st_mtime)
            except OSError:
                continue
            if mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
    except FileNotFoundError:
        return


def _delete_session_uploads() -> None:
    sid = session.get("sid", "")
    if not sid:
        return
    d = UPLOAD_FOLDER / sid
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


def get_session_upload_dir() -> Path:
    if "sid" not in session:
        session["sid"] = str(uuid.uuid4())
    _cleanup_old_uploads()
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


def _is_valid_israeli_id(id_number: str) -> bool:
    """Basic checksum validation for Israeli ID numbers (9 digits, with padding).

    This is not a legal opinion, just a sanity check to catch obvious typos.
    """
    nid = "".join(ch for ch in id_number if ch.isdigit())
    if not nid:
        return False
    # Pad to 9 digits on the left
    nid = nid.zfill(9)
    if len(nid) != 9:
        return False
    total = 0
    for i, ch in enumerate(nid):
        num = int(ch)
        factor = 1 if i % 2 == 0 else 2
        x = num * factor
        if x > 9:
            x -= 9
        total += x
    return total % 10 == 0


@app.before_request
def _load_user_and_log_visit():
    """Attach current user to `g`, protect the funnel, and log basic visit statistics."""
    user = None
    uid = session.get("user_id")
    if uid is not None:
        user = User.query.get(uid)
    g.current_user = user

    # Log only for normal app routes (skip static/assets)
    if request.endpoint and not request.endpoint.startswith("static"):
        try:
            visit = Visit(
                path=request.path[:400],
                method=request.method[:10],
                ip=_get_client_ip()[:64],
                user_agent=(request.headers.get("User-Agent") or "")[:300],
                user=user,
            )
            db.session.add(visit)
            db.session.commit()
        except Exception:
            db.session.rollback()

    # Require login for the main RoboMas funnel (wizard + downloads)
    protected_endpoints = {
        "step_goal",
        "step_year",
        "step_personal",
        "step_family",
        "step_taxfile",
        "taxfile_help",
        "step_income_general",
        "step_income_capital",
        "step_income_pension",
        "step_income_other",
        "step_deductions",
        "step_documents",
        "step_complete",
        "download_zip",
        "download_txt",
        "reset",
    }
    if (
        request.endpoint in protected_endpoints
        and not user
        and request.endpoint not in {"login", "signup"}
    ):
        return redirect(url_for("login", next=request.path))


@app.context_processor
def inject_step_info():
    step, label = _STEP_MAP.get(request.endpoint, (0, ""))
    year = session.get("year", "")
    user = getattr(g, "current_user", None)
    return {
        "current_step": step,
        "total_steps": _TOTAL_STEPS,
        "step_label": label,
        "session_year": year,
        "personal_name": (session.get("personal") or {}).get("first_name", ""),
        "current_user": user,
        "is_admin": bool(user and user.is_admin),
    }


# ── Auth & admin helpers ─────────────────────────────────────────────────────-


def require_admin() -> User:
    user = getattr(g, "current_user", None)
    if not user or not user.is_admin:
        abort(403)
    return user


# ── Auth routes ───────────────────────────────────────────────────────────────


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""
        errors: list[str] = []

        if not username:
            errors.append("שם משתמש הוא שדה חובה.")
        if len(password) < 8:
            errors.append("הסיסמה חייבת להכיל לפחות 8 תווים.")
        if password != confirm:
            errors.append("אישור הסיסמה אינו תואם.")
        if User.query.filter_by(username=username).first():
            errors.append("שם המשתמש כבר קיים במערכת.")

        if errors:
            return render_template("signup.html", errors=errors, username=username)

        user = User(username=username, is_admin=False)
        user.set_password(password)
        user.last_login_at = datetime.utcnow()
        user.last_ip = _get_client_ip()
        db.session.add(user)
        db.session.commit()
        session["user_id"] = user.id
        return redirect(url_for("step_goal"))

    return render_template("signup.html", errors=[])


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        errors: list[str] = []

        user = User.query.filter_by(username=username).first()
        if not user or not user.check_password(password):
            errors.append("שם משתמש או סיסמה שגויים.")
        if errors:
            return render_template("login.html", errors=errors, username=username)

        session["user_id"] = user.id
        user.last_login_at = datetime.utcnow()
        user.last_ip = _get_client_ip()
        db.session.commit()

        next_url = request.args.get("next")
        if next_url and next_url.startswith("/"):
            return redirect(next_url)
        return redirect(url_for("step_goal"))

    return render_template("login.html", errors=[])


@app.route("/logout")
def logout():
    session.pop("user_id", None)
    return redirect(url_for("step_goal"))


# ── Admin routes ──────────────────────────────────────────────────────────────


@app.route("/admin")
def admin_dashboard():
    user = getattr(g, "current_user", None)
    if not user:
        return redirect(url_for("login", next=request.path))
    require_admin()

    stats = {
        "total_users": User.query.count(),
        "admin_count": User.query.filter_by(is_admin=True).count(),
        "total_visits": Visit.query.count(),
        "unique_ips": db.session.scalar(db.select(func.count(func.distinct(Visit.ip)))),
    }
    recent_visits = (
        Visit.query.order_by(Visit.created_at.desc()).limit(50).all()
    )
    users = User.query.order_by(User.created_at.desc()).all()
    return render_template(
        "admin_dashboard.html",
        stats=stats,
        users=users,
        recent_visits=recent_visits,
    )


@app.post("/admin/users/<int:user_id>/make-admin")
def admin_make_admin(user_id: int):
    admin = require_admin()
    target = User.query.get_or_404(user_id)
    if not target.is_admin:
        target.is_admin = True
        db.session.commit()
    return redirect(url_for("admin_dashboard"))


@app.post("/admin/users/<int:user_id>/delete")
def admin_delete_user(user_id: int):
    admin = require_admin()
    target = User.query.get_or_404(user_id)
    if target.id == admin.id:
        # Prevent accidental self-deletion of the only admin
        other_admins = User.query.filter(User.id != admin.id, User.is_admin.is_(True)).count()
        if other_admins == 0:
            abort(400)
    db.session.delete(target)
    db.session.commit()
    return redirect(url_for("admin_dashboard"))


# ── Wizard routes ─────────────────────────────────────────────────────────────
@app.route("/", methods=["GET", "POST"])
def step_goal():
    if request.method == "POST":
        goal = request.form.get("goal", "").strip()
        errors: list[str] = []
        if goal not in ("annual", "refund"):
            errors.append("בחרו מטרה אחת לפני שממשיכים.")
        if errors:
            return render_template("step_goal.html", errors=errors)
        session["goal"] = goal
        return redirect(url_for("step_year"))
    return render_template("step_goal.html", errors=[])


@app.route("/year", methods=["GET", "POST"])
def step_year():
    valid_years = list(range(2016, 2026))
    if request.method == "POST":
        errors: list[str] = []
        raw_year = str(request.form.get("year", "")).strip()
        year_val: int | None = None
        try:
            year_val = int(raw_year)
        except (TypeError, ValueError):
            errors.append("בחרו שנת מס תקפה מתוך הרשימה.")
        else:
            if year_val not in valid_years:
                errors.append("שנת המס שנבחרה אינה נתמכת. בחרו שנה מהרשימה.")

        if errors or year_val is None:
            return render_template(
                "step_year.html",
                years=valid_years,
                selected=session.get("year", 2024),
                errors=errors,
            )

        session["year"] = year_val
        return redirect(url_for("step_personal"))
    return render_template(
        "step_year.html",
        years=valid_years,
        selected=session.get("year", 2024),
        errors=[],
    )


@app.route("/personal", methods=["GET", "POST"])
def step_personal():
    if request.method == "POST":
        errors: list[str] = []
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        dob = request.form.get("dob", "").strip()
        gender = request.form.get("gender", "").strip()
        id_number = request.form.get("id_number", "").strip()
        phone = request.form.get("phone", "").strip()
        email = request.form.get("email", "").strip()

        if not first_name:
            errors.append("שם פרטי הוא שדה חובה.")
        if not last_name:
            errors.append("שם משפחה הוא שדה חובה.")
        if not dob:
            errors.append("תאריך לידה הוא שדה חובה.")
        if gender not in ("male", "female"):
            errors.append("בחרו מין מתוך האפשרויות.")
        if not id_number:
            errors.append("תעודת זהות היא שדה חובה.")
        else:
            if not id_number.isdigit() or len(id_number) not in (8, 9):
                errors.append("תעודת הזהות צריכה להיות מספר בן 8–9 ספרות.")
            elif not _is_valid_israeli_id(id_number):
                errors.append("מספר תעודת הזהות אינו נראה תקין. בדקו שוב.")

        form_data = {
            "first_name": first_name,
            "last_name": last_name,
            "dob": dob,
            "gender": gender,
            "id_number": id_number,
            "phone": phone,
            "email": email,
        }

        if errors:
            year = session.get("year", "")
            return render_template(
                "step_personal.html",
                data=form_data,
                year=year,
                errors=errors,
            )

        session["personal"] = form_data
        return redirect(url_for("step_family"))
    year = session.get("year", "")
    return render_template(
        "step_personal.html",
        data=session.get("personal", {}),
        year=year,
        errors=[],
    )


@app.route("/family", methods=["GET", "POST"])
def step_family():
    if request.method == "POST":
        errors: list[str] = []
        status = request.form.get("status", "").strip()
        foreign_resident = request.form.get("foreign_resident", "no").strip() or "no"
        has_children = request.form.get("has_children", "no").strip() or "no"
        raw_children = request.form.get("children_count", "").strip()

        valid_statuses = {"single", "married", "divorced", "separated", "widowed"}
        if status not in valid_statuses:
            errors.append("בחרו מצב משפחתי מתוך האפשרויות.")

        if has_children not in ("yes", "no"):
            errors.append("ציינו אם יש לכם ילדים או לא.")

        children_count = 0
        if has_children == "yes":
            try:
                children_count = int(raw_children)
            except (TypeError, ValueError):
                errors.append("מספר הילדים חייב להיות מספר שלם.")
            else:
                if children_count < 1 or children_count > 20:
                    errors.append("מספר הילדים צריך להיות בין 1 ל-20.")

        form_data = {
            "status": status,
            "foreign_resident": foreign_resident,
            "has_children": has_children,
            "children_count": children_count,
        }

        if errors:
            year = session.get("year", "")
            return render_template(
                "step_family.html",
                data=form_data,
                year=year,
                errors=errors,
            )

        session["family"] = form_data
        return redirect(url_for("step_taxfile"))
    year = session.get("year", "")
    return render_template(
        "step_family.html",
        data=session.get("family", {}),
        year=year,
        errors=[],
    )


@app.route("/taxfile", methods=["GET", "POST"])
def step_taxfile():
    if request.method == "POST":
        errors: list[str] = []
        has_file = request.form.get("has_file", "").strip()
        open_now = request.form.get("open_now", "no").strip() or "no"
        file_number = request.form.get("file_number", "").strip()
        file_type = request.form.get("file_type", "91").strip() or "91"

        valid_has_file = {"yes", "no", "unknown"}
        if has_file not in valid_has_file:
            errors.append("בחרו אם יש לכם תיק פתוח במס הכנסה.")

        if has_file == "yes" and not file_number:
            errors.append("הזינו את מספר התיק כאשר צוין שיש תיק פתוח.")

        valid_file_types = {code for code, _label in TAXFILE_TYPES}
        if file_type not in valid_file_types:
            errors.append("סוג התיק שנבחר אינו נתמך.")

        form_data = {
            "has_file": has_file,
            "open_now": open_now,
            "file_number": file_number,
            "file_type": file_type,
        }

        if errors:
            year = session.get("year", "")
            personal = session.get("personal", {})
            return render_template(
                "step_taxfile.html",
                data=form_data,
                taxfile_types=TAXFILE_TYPES,
                personal=personal,
                year=year,
                errors=errors,
            )

        session["taxfile"] = form_data
        return redirect(url_for("step_income_general"))
    year = session.get("year", "")
    personal = session.get("personal", {})
    return render_template(
            "step_taxfile.html",
            data=session.get("taxfile", {}),
            taxfile_types=TAXFILE_TYPES,
            personal=personal,
            year=year,
            errors=[],
        )


@app.route("/taxfile/help")
def taxfile_help():
    year = session.get("year", "")
    return render_template("taxfile_help.html", year=year)


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
    ctx = _build_report_context()
    return render_template(
        "step_complete.html",
        year=ctx["year"],
        personal=ctx["personal"],
        goal_label=GOAL_LABELS.get(ctx["goal"], ""),
        income_summary=ctx["income_summary"],
        deductions_labels=ctx["deductions_labels"],
        docs_summary=ctx["docs_summary"],
        insights=ctx["insights"],
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

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
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

    # Reset buffer for sending, then immediately delete all per-session files
    buf.seek(0)
    _delete_session_uploads()

    return send_file(
        buf,
        as_attachment=True,
        download_name="צרופות לטעינה מס הכנסה.zip",
        mimetype="application/zip",
    )


@app.route("/download-txt")
def download_txt():
    """Download a human-readable TXT summary of the answers (for review / advisor)."""
    ctx = _build_report_context()
    year = ctx["year"] or ""
    personal = ctx["personal"]
    user_id = (personal.get("id_number") or "000000000").strip()
    padded_id = user_id.zfill(9)

    lines: list[str] = []
    lines.append("רובומס — קובץ טקסט לסיכום דוח שנתי")
    lines.append("הקובץ נועד לעיון בלבד ואינו מסמך רשמי של רשות המסים.")
    lines.append("-" * 70)
    if year:
        lines.append(f"שנת מס: {year}")
    if user_id:
        lines.append(f"תעודת זהות: {user_id}")
    first = personal.get("first_name", "")
    last = personal.get("last_name", "")
    if first or last:
        lines.append(f"שם: {first} {last}".strip())
    goal = ctx["goal"]
    if goal in GOAL_LABELS:
        lines.append(f"מטרה שנבחרה: {GOAL_LABELS[goal]}")
    lines.append("")

    # Income
    lines.append("מקורות הכנסה שסומנו:")
    if ctx["income_summary"]:
        for group in ctx["income_summary"]:
            lines.append(f"  - {group['group_title']}:")
            for label in group["items"]:
                lines.append(f"      • {label}")
    else:
        lines.append("  (לא סומנו מקורות הכנסה)")
    lines.append("")

    # Deductions
    lines.append("זיכויים וניכויים שסומנו:")
    if ctx["deductions_labels"]:
        for label in ctx["deductions_labels"]:
            lines.append(f"  • {label}")
    else:
        lines.append("  (לא סומנו זיכויים)")
    lines.append("")

    # Documents
    lines.append("סוגי מסמכים שצפויים להידרש (להכוונה בלבד):")
    if ctx["docs_summary"]:
        for d in ctx["docs_summary"]:
            lines.append(f"  • {d['code']} — {d['label']}")
    else:
        lines.append("  (לא זוהו מסמכים חובה בהתאם לבחירות)")
    lines.append("")

    # Insights
    if ctx["insights"]:
        lines.append("תובנות כלליות מהשאלון:")
        for ins in ctx["insights"]:
            lines.append(f"  • {ins}")
        lines.append("")

    lines.append("-" * 70)
    lines.append("שימו לב: אין באמור לעיל משום ייעוץ מס או אחר.")

    content = "\n".join(lines) + "\n"
    buf = io.BytesIO(content.encode("utf-8"))
    filename = f"all_records_{year}_{padded_id}.txt" if year else f"all_records_{padded_id}.txt"
    return send_file(
        buf,
        as_attachment=True,
        download_name=filename,
        mimetype="text/plain; charset=utf-8",
    )


@app.route("/reset")
def reset():
    _delete_session_uploads()
    session.clear()
    return redirect(url_for("step_goal"))


if __name__ == "__main__":
    # Local dev server; in production use gunicorn and a strong SECRET_KEY.
    app.run(debug=False, port=int(os.environ.get("PORT", 5431)))
