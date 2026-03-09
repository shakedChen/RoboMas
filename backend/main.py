from __future__ import annotations

from flask import (
    Flask, render_template, request, session,
    redirect, url_for, send_file, jsonify, g
)
from asgiref.wsgi import WsgiToAsgi
import io
import os
import zipfile
import uuid
import shutil
import hashlib
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from functools import wraps
from werkzeug.utils import secure_filename
from fpdf import FPDF
from supabase import create_client, Client

# Initialize Supabase client
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

supabase: Client | None = None
if SUPABASE_URL and SUPABASE_SERVICE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# Create Flask app
flask_app = Flask(__name__)

# Configure Flask app
flask_app.secret_key = os.environ.get("SECRET_KEY", "robomas-express-il-tax-2024-xk9z")
flask_app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB

# Wrap with ASGI adapter for uvicorn compatibility
app = WsgiToAsgi(flask_app)

BASE_DIR = Path(__file__).parent
# Use /tmp for uploads in serverless environment
UPLOAD_FOLDER = Path("/tmp") / "robomas_uploads" if os.environ.get("VERCEL") else BASE_DIR / "uploads"
UPLOAD_FOLDER.mkdir(exist_ok=True)

# ── Admin credentials (hashed for security) ──────────────────────────────────
# Admin: shutzibutzi / gsdgsdg#@$@#23dfs!
ADMIN_USERNAME = "shutzibutzi"
ADMIN_PASSWORD_HASH = hashlib.sha256("gsdgsdg#@$@#23dfs!".encode()).hexdigest()

# ── Authentication helpers ───────────────────────────────────────────────────
def hash_password(password: str) -> str:
    """Hash password using SHA256 with salt."""
    salt = secrets.token_hex(16)
    hashed = hashlib.sha256((password + salt).encode()).hexdigest()
    return f"{salt}:{hashed}"

def verify_password(password: str, stored_hash: str) -> bool:
    """Verify password against stored hash."""
    if ":" not in stored_hash:
        # Simple hash comparison for admin
        return hashlib.sha256(password.encode()).hexdigest() == stored_hash
    salt, hashed = stored_hash.split(":", 1)
    return hashlib.sha256((password + salt).encode()).hexdigest() == hashed

def get_current_user():
    """Get the current logged-in user from session."""
    user_id = session.get("user_id")
    if not user_id:
        return None
    if user_id == "admin":
        return {"id": "admin", "username": ADMIN_USERNAME, "is_admin": True, "email": "admin@robomas.co.il"}
    if not supabase:
        return None
    try:
        result = supabase.table("profiles").select("*").eq("id", user_id).single().execute()
        if result.data:
            return {**result.data, "is_admin": False}
    except Exception:
        pass
    return None

def login_required(f):
    """Decorator to require login for a route."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("auth_login", next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    """Decorator to require admin access for a route."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_current_user()
        if not user or not user.get("is_admin"):
            return redirect(url_for("auth_login", next=request.url))
        return f(*args, **kwargs)
    return decorated_function

# ── Visitor tracking ─────────────────────────────────────────────────────────
def track_visitor():
    """Track visitor for statistics."""
    if not supabase:
        return
    try:
        visitor_id = session.get("visitor_id")
        if not visitor_id:
            visitor_id = str(uuid.uuid4())
            session["visitor_id"] = visitor_id
        
        ip_address = request.headers.get("X-Forwarded-For", request.remote_addr)
        if ip_address:
            ip_address = ip_address.split(",")[0].strip()
        
        user_agent = request.headers.get("User-Agent", "")[:500]  # Limit length
        page_path = request.path
        
        # Insert visitor record (without storing sensitive data)
        supabase.table("visitors").insert({
            "visitor_id": visitor_id,
            "page_path": page_path,
            "user_agent": user_agent,
            # IP is hashed for privacy
            "ip_hash": hashlib.sha256(ip_address.encode()).hexdigest()[:16] if ip_address else None
        }).execute()
        
        # Update daily stats
        today = datetime.now().strftime("%Y-%m-%d")
        existing = supabase.table("visitor_stats").select("*").eq("date", today).execute()
        
        if existing.data:
            supabase.table("visitor_stats").update({
                "page_views": existing.data[0]["page_views"] + 1,
                "unique_visitors": existing.data[0]["unique_visitors"] + (1 if not session.get("counted_today") else 0)
            }).eq("date", today).execute()
        else:
            supabase.table("visitor_stats").insert({
                "date": today,
                "page_views": 1,
                "unique_visitors": 1
            }).execute()
        
        session["counted_today"] = True
    except Exception as e:
        # Silently fail - don't break the app for analytics
        print(f"Visitor tracking error: {e}")

@flask_app.before_request
def before_request():
    """Track visitors before each request."""
    # Skip tracking for static files and API endpoints
    if request.path.startswith("/static") or request.path.startswith("/api/"):
        return
    track_visitor()
    g.current_user = get_current_user()

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
        insights.append("דיווחת�� על השקעות דרך ברוקר זר — לרוב נדרש לצרף טופסי 1042-S ו-867.")
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
# Try multiple potential font paths for Hebrew support
_HEBREW_FONT_PATHS = [
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",  # macOS
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",       # Linux (Vercel)
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",       # Linux alternative
]
_HEBREW_FONT = None
for font_path in _HEBREW_FONT_PATHS:
    if Path(font_path).exists():
        _HEBREW_FONT = font_path
        break

# ── Summary cover-page PDF ────────────────────────────────────────────────────
def _build_summary_pdf(
    personal: dict,
    year: int | str,
    entries: list[tuple[str, str, str]],
) -> bytes:
    """Return bytes of a Hebrew cover-page PDF listing all uploaded documents."""
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=20)
    
    # Use Hebrew font if available, otherwise fall back to built-in helvetica
    if _HEBREW_FONT:
        pdf.add_font("HEB", "", _HEBREW_FONT)
        pdf.add_font("HEB", "B", _HEBREW_FONT)
        font_name = "HEB"
    else:
        font_name = "helvetica"

    # ── Cover page ───────────���────────────────────────────────────────────────
    pdf.add_page()

    # Deep blue header bar
    pdf.set_fill_color(10, 36, 99)
    pdf.rect(0, 0, 210, 42, "F")

    # Logo text
    pdf.set_font(font_name, "B", 24)
    pdf.set_text_color(255, 255, 255)
    pdf.set_xy(0, 10)
    pdf.cell(210, 10, "RoboMas" if font_name == "helvetica" else "רובומס", align="C")

    pdf.set_font(font_name, "", 12)
    pdf.set_xy(0, 24)
    pdf.cell(210, 8, "Tax Documents Summary" if font_name == "helvetica" else "צרופות לטעינה — מס הכנסה", align="C")

    pdf.set_text_color(30, 30, 30)

    # Year pill
    pdf.set_fill_color(230, 240, 255)
    pdf.set_draw_color(100, 140, 210)
    pdf.set_line_width(0.5)
    pdf.rect(75, 50, 60, 12, "FD")
    pdf.set_font(font_name, "B", 14)
    pdf.set_text_color(10, 36, 99)
    pdf.set_xy(75, 52)
    pdf.cell(60, 8, f"Tax Year {year}" if font_name == "helvetica" else f"שנת מס {year}", align="C")

    pdf.set_text_color(30, 30, 30)
    pdf.set_y(72)

    # Personal info block
    name   = personal.get("name", "").strip()
    id_num = personal.get("id_number", "").strip()
    pdf.set_font(font_name, "B", 11)
    if name:
        pdf.set_x(0)
        pdf.cell(190, 8, name, align="R")
        pdf.ln(7)
    if id_num:
        pdf.set_x(0)
        id_label = "ID:" if font_name == "helvetica" else "ת.ז.:"
        pdf.cell(190, 7, f"{id_label} {id_num}", align="R")
        pdf.ln(7)

    pdf.ln(6)

    # Section title
    pdf.set_fill_color(10, 36, 99)
    pdf.set_text_color(255, 255, 255)
    pdf.set_x(15)
    pdf.set_font(font_name, "B", 11)
    section_title = "Attached Documents" if font_name == "helvetica" else "רשימת המסמכים שצורפו"
    pdf.cell(180, 9, section_title, fill=True, align="R")
    pdf.ln(12)

    pdf.set_text_color(30, 30, 30)

    if entries:
        # Table header
        pdf.set_fill_color(240, 245, 255)
        pdf.set_font(font_name, "B", 9)
        pdf.set_x(15)
        col1 = "File Name in ZIP" if font_name == "helvetica" else "שם קובץ ב-ZIP"
        col2 = "Document Type" if font_name == "helvetica" else "סוג מסמך"
        pdf.cell(90, 8, col1, border=1, fill=True, align="C")
        pdf.cell(90, 8, col2, border=1, fill=True, align="C")
        pdf.ln()

        # Table rows
        pdf.set_font(font_name, "", 9)
        for i, (zip_name, label, _orig) in enumerate(entries):
            fill = i % 2 == 0
            pdf.set_fill_color(250, 252, 255) if fill else pdf.set_fill_color(255, 255, 255)
            pdf.set_x(15)
            pdf.cell(90, 7, zip_name, border=1, fill=fill, align="L")
            pdf.cell(90, 7, label,    border=1, fill=fill, align="R")
            pdf.ln()
    else:
        pdf.set_font(font_name, "", 11)
        pdf.set_x(0)
        no_docs = "No documents uploaded." if font_name == "helvetica" else "לא הועלו מסמכים."
        pdf.cell(190, 10, no_docs, align="C")
        pdf.ln()

    # Footer
    pdf.set_y(-25)
    pdf.set_draw_color(180, 180, 180)
    pdf.set_line_width(0.3)
    pdf.line(15, pdf.get_y(), 195, pdf.get_y())
    pdf.ln(3)
    pdf.set_font(font_name, "", 8)
    pdf.set_text_color(120, 120, 120)
    pdf.set_x(0)
    footer_text = "www.robomas.co.il  |  Auto-generated by RoboMas" if font_name == "helvetica" else "www.robomas.co.il  |  מסמך זה הופק אוטומטית על ידי מערכת רובומס"
    pdf.cell(190, 5, footer_text, align="C")

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


@flask_app.context_processor
def inject_step_info():
    step, label = _STEP_MAP.get(request.endpoint, (0, ""))
    year = session.get("year", "")
    user = getattr(g, 'current_user', None)
    return {
        "current_step":  step,
        "total_steps":   _TOTAL_STEPS,
        "step_label":    label,
        "session_year":  year,
        "personal_name": (session.get("personal") or {}).get("first_name", ""),
        "current_user":  user,
        "is_logged_in":  user is not None,
        "is_admin":      user.get("is_admin", False) if user else False,
    }


# ── Routes ────────────────────────────────────────────────────────────────────
@flask_app.route("/", methods=["GET", "POST"])
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


@flask_app.route("/year", methods=["GET", "POST"])
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


@flask_app.route("/personal", methods=["GET", "POST"])
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


@flask_app.route("/family", methods=["GET", "POST"])
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


@flask_app.route("/taxfile", methods=["GET", "POST"])
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


@flask_app.route("/income/general", methods=["GET", "POST"])
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


@flask_app.route("/income/capital", methods=["GET", "POST"])
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


@flask_app.route("/income/pension", methods=["GET", "POST"])
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


@flask_app.route("/income/other", methods=["GET", "POST"])
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


@flask_app.route("/deductions", methods=["GET", "POST"])
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


@flask_app.route("/documents", methods=["GET", "POST"])
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


@flask_app.route("/complete")
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


@flask_app.route("/download")
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


@flask_app.route("/download-txt")
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


@flask_app.route("/reset")
def reset():
    sid = session.get("sid", "")
    if sid:
        d = UPLOAD_FOLDER / sid
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    # Keep user session, only clear form data
    user_id = session.get("user_id")
    visitor_id = session.get("visitor_id")
    session.clear()
    if user_id:
        session["user_id"] = user_id
    if visitor_id:
        session["visitor_id"] = visitor_id
    return redirect(url_for("step_goal"))


# ── Authentication Routes ────────────────────────────────────────────────────
@flask_app.route("/login", methods=["GET", "POST"])
def auth_login():
    errors = []
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        
        if not username or not password:
            errors.append("נא למלא שם משתמש וסיסמה.")
        else:
            # Check if admin
            if username == ADMIN_USERNAME:
                if hashlib.sha256(password.encode()).hexdigest() == ADMIN_PASSWORD_HASH:
                    session["user_id"] = "admin"
                    session["is_admin"] = True
                    next_url = request.args.get("next", url_for("admin_dashboard"))
                    return redirect(next_url)
                else:
                    errors.append("שם משתמש או סיסמה שגויים.")
            elif supabase:
                # Check regular user in database
                try:
                    result = supabase.table("profiles").select("*").eq("username", username).execute()
                    if result.data:
                        user = result.data[0]
                        if verify_password(password, user.get("password_hash", "")):
                            session["user_id"] = user["id"]
                            session["is_admin"] = False
                            next_url = request.args.get("next", url_for("step_goal"))
                            return redirect(next_url)
                        else:
                            errors.append("שם משתמש או סיסמה שגויים.")
                    else:
                        errors.append("שם משתמש או סיסמה שגויים.")
                except Exception as e:
                    errors.append("שגיאה בהתחברות. נסו שוב.")
            else:
                errors.append("שם משתמש או סיסמה שגויים.")
    
    return render_template("auth_login.html", errors=errors)


@flask_app.route("/signup", methods=["GET", "POST"])
def auth_signup():
    errors = []
    form_data = {}
    
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "").strip()
        confirm_password = request.form.get("confirm_password", "").strip()
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        
        form_data = {
            "username": username,
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
        }
        
        # Validation
        if not username or len(username) < 3:
            errors.append("שם משתמש חייב להכיל לפחות 3 תווים.")
        if not email or "@" not in email:
            errors.append("נא להזין כתובת אימייל תקינה.")
        if not password or len(password) < 8:
            errors.append("הסיסמה חייבת להכיל לפחות 8 תווים.")
        if password != confirm_password:
            errors.append("הסיסמאות אינן תואמות.")
        
        # Check reserved username
        if username.lower() == ADMIN_USERNAME.lower():
            errors.append("שם משתמש זה אינו זמין.")
        
        if not errors and supabase:
            try:
                # Check if username exists
                existing = supabase.table("profiles").select("id").eq("username", username).execute()
                if existing.data:
                    errors.append("שם המשתמש כבר קיים במערכת.")
                else:
                    # Check if email exists
                    existing_email = supabase.table("profiles").select("id").eq("email", email).execute()
                    if existing_email.data:
                        errors.append("כתובת האימייל כבר רשומה במערכת.")
                    else:
                        # Create user
                        user_id = str(uuid.uuid4())
                        password_hash = hash_password(password)
                        
                        supabase.table("profiles").insert({
                            "id": user_id,
                            "username": username,
                            "email": email,
                            "password_hash": password_hash,
                            "first_name": first_name,
                            "last_name": last_name,
                        }).execute()
                        
                        # Log them in
                        session["user_id"] = user_id
                        session["is_admin"] = False
                        return redirect(url_for("step_goal"))
            except Exception as e:
                errors.append(f"שגיאה בהרשמה. נסו שוב.")
        elif not supabase:
            errors.append("מערכת ההרשמה אינה זמינה כרגע.")
    
    return render_template("auth_signup.html", errors=errors, data=form_data)


@flask_app.route("/logout")
def auth_logout():
    session.pop("user_id", None)
    session.pop("is_admin", None)
    return redirect(url_for("step_goal"))


# ── Admin Dashboard ──────────────────────────────────────────────────────────
@flask_app.route("/admin")
@admin_required
def admin_dashboard():
    stats = {"total_visitors": 0, "total_page_views": 0, "today_visitors": 0, "today_page_views": 0}
    daily_stats = []
    users = []
    
    if supabase:
        try:
            # Get aggregate stats
            all_stats = supabase.table("visitor_stats").select("*").order("date", desc=True).limit(30).execute()
            if all_stats.data:
                daily_stats = all_stats.data
                stats["total_visitors"] = sum(s["unique_visitors"] for s in all_stats.data)
                stats["total_page_views"] = sum(s["page_views"] for s in all_stats.data)
                
                today = datetime.now().strftime("%Y-%m-%d")
                today_stat = next((s for s in all_stats.data if s["date"] == today), None)
                if today_stat:
                    stats["today_visitors"] = today_stat["unique_visitors"]
                    stats["today_page_views"] = today_stat["page_views"]
            
            # Get users
            users_result = supabase.table("profiles").select("id, username, email, first_name, last_name, created_at").order("created_at", desc=True).execute()
            users = users_result.data or []
        except Exception as e:
            print(f"Admin dashboard error: {e}")
    
    return render_template("admin_dashboard.html", stats=stats, daily_stats=daily_stats, users=users)


@flask_app.route("/api/admin/stats")
@admin_required
def api_admin_stats():
    """API endpoint for admin statistics."""
    if not supabase:
        return jsonify({"error": "Database not connected"}), 500
    
    try:
        # Get last 30 days stats
        stats = supabase.table("visitor_stats").select("*").order("date", desc=True).limit(30).execute()
        return jsonify({"stats": stats.data or []})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5431))
    flask_app.run(debug=True, host="0.0.0.0", port=port)
