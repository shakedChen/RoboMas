# 🤖 רובומס — RoboMas Tax Refunds

RoboMas is a Hebrew-language Flask web application that walks Israeli taxpayers through the annual tax report (דוח שנתי) and refund process, step by step.

> ⚠️ **Disclaimer**: RoboMas does **not** replace professional tax advice. Always verify the final forms with a qualified professional or the Israel Tax Authority.

## Features

- **Simple step-by-step wizard**: answer a short series of clear questions (יעד, שנה, פרטים אישיים, משפחה, קובץ ניכויים, הכנסות, זכאויות, מסמכים) instead of filling long forms by yourself.
- **Helps you find refunds**: based on your answers, RoboMas highlights relevant credits, deductions, and situations where you might be entitled to a tax refund.
- **Explains what and why in Hebrew**: each step includes short explanations in Hebrew so you understand what information is needed and how it affects your refund.
- **Easy document upload**: upload all required documents with drag & drop, organized by type, with a clear list of what you already added and what is missing.
- **Ready-for-upload ZIP file**: at the end you get a single ZIP file that is already named and formatted the way the Israel Tax Authority expects, including a summary file (קובץ סיכום).
- **Designed for Israelis**: right-to-left interface, Hebrew labels, and a visual style that feels familiar.
- **No registration**: use RoboMas immediately without creating an account or remembering a password.

## Requirements

- Python 3.8+
- `pip`

All Python dependencies are listed in `requirements.txt`.

## Installation & Running Locally

From the project root (`Robomas/`):

```bash
# (Optional but recommended) create a virtual environment
python3 -m venv .venv
source .venv/bin/activate  # on Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run the development server
python app.py
```

Then open [http://127.0.0.1:5431](http://127.0.0.1:5431) in your browser.

## Project Structure

```text
robomas/
├── app.py                   # Flask app — routes, session logic, ZIP builder
├── templates/
│   ├── base.html            # Layout, header, progress bar, nav buttons
│   ├── step_goal.html       # Step 1 — choose goal
│   ├── step_year.html       # Step 2 — select tax year
│   ├── step_personal.html   # Step 3 — personal details
│   ├── step_family.html     # Step 4 — family status & children
│   ├── step_taxfile.html    # Step 5 — tax file details
│   ├── step_income_*.html   # Steps 6–9 — income categories
│   ├── step_deductions.html # Step 10 — deductions & credits
│   ├── step_documents.html  # Step 11 — document upload
│   └── step_complete.html   # Step 12 — download ZIP
├── static/
│   ├── css/style.css
│   └── js/main.js
└── uploads/                 # Temporary upload storage (gitignored)
```

## ZIP Output Format

Each uploaded file is renamed to the Israel Tax Authority standard:

```text
{CODE}_{PART}_{SEQ}_{ID10}{YEAR}.pdf
```

For example: `AK005000_01_001_03190572532024.pdf` (Form 106).

In addition, a cover page is auto-generated and included:

```text
קובץ_סיכום_01_001_{ID}{YEAR}.pdf
```

Both the summary file and all renamed attachments are bundled into a single ZIP ready for upload.
