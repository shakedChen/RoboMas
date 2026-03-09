# 🤖 רובומס — RoboMas Tax Refunds

A Hebrew-language Flask web application that guides Israeli taxpayers through the annual tax report (דוח שנתי) and refund process — step by step.

## Features

- **Multi-step funnel** — goal → year → personal info → family → tax file → income (4 categories) → deductions → documents → download
- **Smart conditional logic** — yes/no answers trigger contextual information and auto-select relevant deductions
- **Document upload** — drag & drop or file picker per document type, with real-time progress
- **ZIP generation** — produces a ready-to-upload `צרופות לטעינה מס הכנסה.zip` with IRS-standard filenames (`CODE_PART_SEQ_IDYEAR.pdf`) plus a Hebrew cover-page PDF (`קובץ_סיכום`)
- **RTL Hebrew UI** — Israeli-flag-inspired color palette, fully right-to-left layout
- **No authentication required**

## Quick Start

```bash
# Install dependencies
pip install flask fpdf2

# Run
python app.py
```

Then open [http://127.0.0.1:5431](http://127.0.0.1:5431)

## Project Structure

```
robomas/
├── app.py                  # Flask app — routes, session logic, ZIP builder
├── templates/
│   ├── base.html           # Layout, header, progress bar, nav buttons
│   ├── step_goal.html      # Step 1 — choose goal
│   ├── step_year.html      # Step 2 — select tax year
│   ├── step_personal.html  # Step 3 — personal details
│   ├── step_family.html    # Step 4 — family status & children
│   ├── step_taxfile.html   # Step 5 — tax file details
│   ├── step_income_*.html  # Steps 6–9 — income categories
│   ├── step_deductions.html# Step 10 — deductions & credits
│   ├── step_documents.html # Step 11 — document upload
│   └── step_complete.html  # Step 12 — download ZIP
├── static/
│   ├── css/style.css
│   └── js/main.js
└── uploads/                # Temporary upload storage (gitignored)
```

## ZIP Output Format

Each uploaded file is renamed to the IRS standard:

```
{CODE}_{PART}_{SEQ}_{ID10}{YEAR}.pdf
```

For example: `AK005000_01_001_03190572532024.pdf` (Form 106)

A cover page `קובץ_סיכום_01_001_{ID}{YEAR}.pdf` is auto-generated and included.

## Requirements

- Python 3.8+
- Flask
- fpdf2
