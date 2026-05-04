import subprocess, sys
subprocess.check_call([sys.executable, "-m", "pip", "install",
    "requests", "beautifulsoup4", "pdfplumber", "gspread", "google-auth", "-q"])

import requests, re, io, time, urllib3, socket
from datetime import datetime, timedelta
from urllib.parse import urlparse
from bs4 import BeautifulSoup
import gspread
from google.auth import default
from google.oauth2.service_account import Credentials as SACredentials
import os, json
import argparse

# ════════════════════════════════════════════════════════════
# CLIENT CONFIG — MAPlanning (inlined — no external JSON file needed)
# Edit the values below to change keywords, scoring, or target sheet.
# ════════════════════════════════════════════════════════════
CLIENT_CONFIG = {
    "client_id":            "maplanning",
    "client_name":          "MAPlanning",
    "sheet_id":             "172bpv-b2_nK5ENE1XPk5rWeokvnr1sjHvLBfVzHWh6c",
    "email_to_secret_name": "GMAIL_TO_MAPLANNING",
    "client_type":          "retail",
    "min_lead_score":       60,  # Mark's threshold from maplanning.json
    "preferred_documents":  ["Decision Notice"],

    # ── Mark's search keywords (from maplanning.json) + "to X" variants ────
    # These go into the Idox description search field.
    # "to X" variants catch change-of-use applications by their TARGET use.
    # Pure policy terms (sequential, vitality) are NOT here — they're in
    # pdf_triggers. Application descriptions don't use policy language.
    "search_keywords": [
        # Primary Class E (Mark's exact terms from JSON)
        "Class E", "change of use", "use class e",
        # Retail
        "shop", "to shop",
        "retail", "to retail",
        "supermarket", "to supermarket",
        "convenience", "to convenience",
        "food store", "to food store",
        "discount store",
        "comparison retail", "to comparison",
        # Food & Drink
        "café", "cafe", "to café", "to cafe",
        "restaurant", "to restaurant",
        "hot food", "to hot food",
        "takeaway", "to takeaway",
        "coffee shop",
        "food and drink",
        "drive-through", "drive through",
        # Leisure / Health / Personal Services
        "gym", "to gym",
        "fitness",
        "hair", "beauty", "nail", "barber",
        "health centre",
        "clinic", "to clinic",
        "pharmacy", "to pharmacy",
        "optician",
        # Other Class E
        "office", "to office",
        "workspace",
        # Sui Generis (Mark's JSON includes these)
        "sui generis",
        "betting",
        "amusement",
        "car wash",
        "mixed use",
    ],

    

    "pdf_triggers": [
        # Sequential test failures
        "out of centre", "out-of-centre", "outside the town centre",
        "outside a defined centre", "outside any defined centre",
        "edge of centre", "edge-of-centre", "edge of the town centre",
        "sequential", "sequential test", "sequential approach",
        "sequential assessment", "sequential preference", "sequential search",
        "no sequential", "fail the sequential", "fails the sequential", "failed the sequential",
        "sequentially preferable", "sequential step",
        # Evidence / assessment failures (strongest appeal grounds)
        "lack of evidence", "insufficient evidence", "no evidence",
        "lack of information", "insufficient information",
        "failure to demonstrate", "failed to demonstrate",
        "fails to demonstrate", "not demonstrated", "has not demonstrated",
        "cannot demonstrate", "unable to demonstrate",
        "no information provided", "no assessment",
        "has not been submitted", "not been submitted",
        "not been provided", "has not been provided",
        "was not submitted", "absence of", "in the absence of",
        # Retail impact assessment
        "retail impact assessment", "retail impact study", "retail impact",
        "impact assessment", "quantitative need assessment",
        # Vitality / viability
        "harm to the vitality and viability", "harm to the vitality",
        "adverse impact on the vitality", "undermine the vitality",
        "prejudice the vitality", "vitality and viability",
        "health of the town centre",
        # Need
        "no identified need", "no quantitative need", "no qualitative need",
        "need has not been", "need has not been demonstrated",
        "no overriding need", "need not been established",
        "unmet need", "no need has been",
        # Justification
        "insufficient justification", "failed to justify",
        "fails to justify", "not justified", "unjustified",
        # NPPF chapter 7 specific
        "nppf", "national planning policy framework",
        "paragraph 91", "paragraph 88", "paragraph 89", "paragraph 90",
        "main town centre use", "primary shopping area",
        "defined town centre", "primary frontage", "secondary frontage",
        # Council policy
        "retail policy", "town centre policy", "out of centre policy",
        "local plan policy", "development plan policy",
    ],

    "exclude_words": [
        "discharge of condition", "discharge of planning condition",
        "reserved matters", "approval of details", "approval of reserved",
        "details reserved by condition", "condition discharge",
        "certificate of lawful", "advertisement consent",
        "listed building consent", "hedgerow removal",
        "non-material amendment", "minor material amendment",
        "section 73", "s73", "screening opinion", "scoping opinion",
        "environmental impact assessment screening", "prior notification",
        "class ma", "part 6", "part 7", "notification under",
        "prior notification under", "telecommunications", "street works",
        "temporary structure", "HMO", "House in Multiple Occupation",
        "Hostel", "C4 use", "Sui Generis HMO", "prior approval",
        "lawful development", "tree preservation", "single storey extension",
        "loft conversion", "porch", "garage alteration",
    ],
}

# ── Flat constants derived from config ──────────────────────────────────────
SHEET_ID        = CLIENT_CONFIG["sheet_id"]
RETAIL_KEYWORDS = CLIENT_CONFIG["search_keywords"]
PDF_TRIGGERS    = CLIENT_CONFIG["pdf_triggers"]
EXCLUDE_WORDS   = CLIENT_CONFIG["exclude_words"]
MIN_LEAD_SCORE  = CLIENT_CONFIG["min_lead_score"]
CLIENT_EMAIL_VAR= CLIENT_CONFIG["email_to_secret_name"]
CLIENT_TYPE     = CLIENT_CONFIG.get("client_type", "retail")

# ── CLI: --weeks = how far back; --mode = what to look for ──────────────────
# Usage examples:
#   python engine_ma.py --weeks 2              (default: find refusals, 2-week window)
#   python engine_ma.py --weeks 4 --mode both  (refusals + competitor alerts)
#   python engine_ma.py --mode applications    (only competitor alerts)
parser = argparse.ArgumentParser(description="MAPlanning Retail Lead Engine v26")
parser.add_argument("--weeks", type=int, default=2,
                    help="Weeks of applications to scan (default 2)")
parser.add_argument("--mode",  type=str, default="decisions",
                    choices=["decisions", "applications", "both"],
                    help="decisions=refused apps | applications=competitor alerts | both")
args, _unknown = parser.parse_known_args()
WEEKS_TO_SCRAPE = args.weeks
RUN_MODE        = args.mode

import email_digest
import pdfplumber

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ════════════════════════════════════════════════════════════
# CONFIG
# First run:  WEEKS_TO_SCRAPE = 12  (3 month backfill)
# Weekly run: WEEKS_TO_SCRAPE = 2
# ════════════════════════════════════════════════════════════
# ── Verified Idox portals only ────────────────────────────
# Each URL is tested at startup — dead ones are skipped automatically.
# All use the standard Idox /search.do?action=advanced endpoint.
#
# ── v18 URL audit notes ──────────────────────────────────
# Many councils migrated Idox subdomains. All URLs below are verified
# against live search results (March 2026).
# Non-Idox portals (Northgate, Ocella, Fastweb, Angular SPAs) removed —
# they use different form structures and cannot be scraped by this tool.
# Councils that time-out from GitHub US IPs are kept in the dict —
# they work fine when run from Colab (UK IP routing).
COUNCILS = {
    # ══ West Yorkshire ══════════════════════════════════════════
    "Leeds":             "https://publicaccess.leeds.gov.uk/online-applications",
    "Wakefield":         "https://planning.wakefield.gov.uk/online-applications",
    "Bradford":          "https://planning.bradford.gov.uk/online-applications",
    "Calderdale":        "https://portal.calderdale.gov.uk/online-applications",
    "Kirklees":          "https://www.kirklees.gov.uk/beta/planning-and-building-control/online-applications",

    # ══ Greater Manchester ══════════════════════════════════════
    # All confirmed Idox — ALL require UK IP (blocked from GitHub US).
    # Run from Colab for these. Tameside = where Mark's confirmed leads came from.
    "Tameside":          "https://publicaccess.tameside.gov.uk/online-applications",
    "Manchester":        "https://pa.manchester.gov.uk/online-applications",
    "Salford":           "https://publicaccess.salford.gov.uk/online-applications",
    "Trafford":          "https://pa.trafford.gov.uk/online-applications",
    "Bolton":            "https://www.planningpa.bolton.gov.uk/online-applications-17",
    "Oldham":            "https://planningpa.oldham.gov.uk/online-applications/",
    "Bury":              "https://planning.bury.gov.uk/online-applications",
    "Rochdale":          "https://planning.rochdale.gov.uk/online-applications",
    "Wigan":             "https://planning.wigan.gov.uk/online-applications",
    "Stockport":         "https://planning.stockport.gov.uk/PlanningData-live/",

    # ══ South Yorkshire ═════════════════════════════════════════
    "Sheffield":         "https://planningapps.sheffield.gov.uk/online-applications",
    "Barnsley":          "https://www.barnsley.gov.uk/online-applications",
    "Doncaster":         "https://planning.doncaster.gov.uk/online-applications",
    # Rotherham: Fastweb portal — not Idox, excluded

    # ══ North/East Yorkshire ════════════════════════════════════
    "York":              "https://planningaccess.york.gov.uk/online-applications/",
    "East Riding":       "https://www.eastriding.gov.uk/online-applications",
    "Hull":              "https://www.hullcc.gov.uk/padcbc/publicaccess-live/",
    "North Yorkshire":   "https://planning.northyorks.gov.uk/online-applications",

    # ══ North East ══════════════════════════════════════════════
    "Sunderland":        "https://online-applications.sunderland.gov.uk/online-applications",
    "Durham":            "https://publicaccess.durham.gov.uk/online-applications",
    "North Tyneside":    "https://idoxpublicaccess.northtyneside.gov.uk/online-applications",
    "South Tyneside":    "https://www.southtyneside.gov.uk/online-applications",
    "Northumberland":    "https://publicaccess.northumberland.gov.uk/online-applications/",
    "Middlesbrough":     "https://planning.middlesbrough.gov.uk/online-applications",
    "Darlington":        "https://planning.darlington.gov.uk/online-applications",
    "Stockton":          "https://www.stockton.gov.uk/online-applications",
    "Hartlepool":        "https://eha.hartlepool.gov.uk/online-applications",
    "Redcar":            "https://planning.redcar-cleveland.gov.uk/online-applications",
    "Newcastle":         "https://publicaccess.newcastle.gov.uk/online-applications/",
    "Gateshead":         "https://public.gateshead.gov.uk/online-applications/",

    # ══ North West ══════════════════════════════════════════════
    "Knowsley":          "https://publicaccess.knowsley.gov.uk/online-applications",
    "Wirral":            "https://planning.wirral.gov.uk/online-applications",
    "Lancaster":         "https://planning.lancaster.gov.uk/online-applications",
    "Blackpool":         "https://idoxpa.blackpool.gov.uk/online-applications",
    "Cheshire West":     "https://pa.cheshirewestandchester.gov.uk/online-applications",
    "Sefton":            "https://pa.sefton.gov.uk/online-applications",
    "St Helens":         "https://publicaccess.sthelens.gov.uk/online-applications/",
    "Halton":            "https://pa.halton.gov.uk/online-applications",
    "Burnley":           "https://planning.burnley.gov.uk/online-applications",
    "Pendle":            "https://planning.pendle.gov.uk/online-applications",
    "Chorley":           "https://planning.chorley.gov.uk/online-applications/search.do?action=simple&searchType=Application",
    "West Lancashire":   "https://pa.westlancs.gov.uk/online-applications/search.do?action=simple&searchType=Application",
    "South Ribble":      "https://publicaccess.southribble.gov.uk/online-applications/",
    # Preston: ASP.NET portal — not Idox
    # Warrington: migrated off Idox — not Idox
    # Cheshire East: AdvancedSearch.aspx — not Idox

    # ══ East Midlands ═══════════════════════════════════════════
    "Lincoln":           "https://planning.lincoln.gov.uk/online-applications",
    "Nottingham":        "https://publicaccess.nottinghamcity.gov.uk/online-applications",
    "Derby":             "https://eplanning.derby.gov.uk/online-applications",
    # Northampton BC was ABOLISHED April 2021 — replaced by West Northamptonshire
    "West Northants":    "https://www.westnorthants.gov.uk/online-applications",
    "North Northants":   "https://www.northnorthants.gov.uk/online-applications",
    "Leicester":         "https://planning.leicester.gov.uk/online-applications",
    "Chesterfield":      "https://publicaccess.chesterfield.gov.uk/online-applications/",
    "Peterborough":      "https://planning.peterborough.gov.uk/online-applications",

    # ══ West Midlands ═══════════════════════════════════════════
    "Wolverhampton":     "https://planningonline.wolverhampton.gov.uk/online-applications",
    "Solihull":          "https://publicaccess.solihull.gov.uk/online-applications",
    "Birmingham":        "https://eplanning.birmingham.gov.uk/online-applications",
    "Coventry":          "https://planningapps.coventry.gov.uk/online-applications",
    "Walsall":           "https://planningonline.walsall.gov.uk/online-applications",
    "Dudley":            "https://www.dudley.gov.uk/online-applications",


    # ══ South East (additional) ════════════════════════════════
    "Milton Keynes":     "https://publicaccess.milton-keynes.gov.uk/online-applications",
    "Slough":            "https://digital.slough.gov.uk/online-applications",
    # ══ South West ══════════════════════════════════════════════
    "Bristol":           "https://planningonline.bristol.gov.uk/online-applications",
    "Plymouth":          "https://planning.plymouth.gov.uk/online-applications",
    "Exeter":            "https://publicaccess.exeter.gov.uk/online-applications",
    "Cornwall":          "https://planning.cornwall.gov.uk/online-applications",
    "Cheltenham":        "https://publicaccess.cheltenham.gov.uk/online-applications",
    "Gloucester":        "https://publicaccess.gloucester.gov.uk/online-applications",
    "Swindon":           "https://pa.swindon.gov.uk/publicaccess/",
    "Torbay":            "https://publicaccess.torbay.gov.uk/view/",
    "Bath":              "https://app.bathnes.gov.uk/webforms/planning/search.html",

    # ══ South East ══════════════════════════════════════════════
    "Portsmouth":        "https://publicaccess.portsmouth.gov.uk/online-applications",
    "Southampton":       "https://planningpublicaccess.southampton.gov.uk/online-applications",
    "Reading":           "https://planning.reading.gov.uk/online-applications",
    "Oxford":            "https://public.oxford.gov.uk/online-applications",
    "Canterbury":        "https://pa.canterbury.gov.uk/online-applications",
    "Maidstone":         "https://pa.midkent.gov.uk/online-applications/",
    "Thanet":            "https://planning.thanet.gov.uk/online-applications",
    "Guildford":         "https://publicaccess.guildford.gov.uk/online-applications",
    "Eastbourne":        "https://planning.eastbourne.gov.uk/online-applications",
    "Worthing":          "https://planning.adur-worthing.gov.uk/online-applications/",
    "Brighton":          "https://publicaccess.brighton-hove.gov.uk/online-applications/",
    "Hastings":          "https://publicaccess.hastings.gov.uk/online-applications/",
    "Chichester":        "https://publicaccess.chichester.gov.uk/online-applications",
    "Arun":              "https://www.arun.gov.uk/online-applications",
    "Reigate":           "https://planning.reigate-banstead.gov.uk/online-applications/",
    "Medway":            "https://publicaccess1.medway.gov.uk/online-applications/",
    "Swale":             "https://pa.midkent.gov.uk/online-applications/",
    "Tunbridge Wells":   "https://pa.tunbridgewells.gov.uk/online-applications",
    "Sevenoaks":         "https://pa.sevenoaks.gov.uk/online-applications",
    "Ashford":           "https://www.ashford.gov.uk/online-applications",
    "Dover":             "https://publicaccess.dover.gov.uk/online-applications/search.do?action=simple&searchType=Application",
    "Folkestone":        "https://planningpa.folkestone.gov.uk/online-applications",
    "Tonbridge":         "https://publicaccess.tmbc.gov.uk/online-applications/",
    "Fareham":           "https://planning.fareham.gov.uk/online-applications",
    "Winchester":        "https://planningapps.winchester.gov.uk/online-applications/",
    "Eastleigh":         "https://planning.eastleigh.gov.uk/online-applications",
    "New Forest":        "https://planning.newforest.gov.uk/online-applications/",
    "Test Valley":       "https://view-applications.testvalley.gov.uk/online-applications/",
    "Basingstoke":       "https://publicaccess.basingstoke.gov.uk/online-applications/",
    "Hart":              "https://publicaccess.hart.gov.uk/online-applications/",
    "West Berkshire":    "https://publicaccess.westberks.gov.uk/online-applications",
    "South & Vale":      "https://www.southandvale.gov.uk/online-applications",  # merged South Oxfordshire + Vale of White Horse
    # South Oxfordshire (southoxon.gov.uk) and Vale of White Horse (whitehorsedc.gov.uk) both
    # redirect to southandvale.gov.uk since 2020 merger — old URLs return Oops pages
    "Cherwell":          "https://pa.cherwell.gov.uk/online-applications",
    "West Oxfordshire":  "https://publicaccess.westoxon.gov.uk/online-applications/",
    "Woking":            "https://caps.woking.gov.uk/online-applications/",
    "Elmbridge":         "https://www.elmbridge.gov.uk/online-applications",
    "Crawley":           "https://planning.crawley.gov.uk/online-applications",
    "Horsham":           "https://public-access.horsham.gov.uk/public-access/",
    "Mid Sussex":        "https://pa.midsussex.gov.uk/online-applications/",
    "Wealden":           "https://www.wealden.gov.uk/online-applications",
    "Rother":            "https://www.rother.gov.uk/online-applications",
    "Runnymede":         "https://idoxpa.runnymede.gov.uk/online-applications",

    # ══ East of England ═════════════════════════════════════════
    "North Norfolk":     "https://idoxpa.north-norfolk.gov.uk/online-applications/",
    "Norwich":           "https://planning.norwich.gov.uk/online-applications/",
    "Cambridge":         "https://applications.greatercambridgeplanning.org/online-applications",
    "Chelmsford":        "https://publicaccess.chelmsford.gov.uk/online-applications",
    "Luton":             "https://planning.luton.gov.uk/online-applications/",
    "Braintree":         "https://publicaccess.braintree.gov.uk/online-applications/",
    "Basildon":          "https://planning.basildon.gov.uk/online-applications/",
    "Tendring":          "https://idox.tendringdc.gov.uk/online-applications",
    "Ipswich":           "https://ppc.ipswich.gov.uk/online-applications",
    "Colchester":        "https://www.colchester.gov.uk/online-applications",
    "East Suffolk":      "https://www.eastsuffolk.gov.uk/online-applications",
    "West Suffolk":      "https://planning.westsuffolk.gov.uk/online-applications/",
    "Great Yarmouth":    "https://planning.great-yarmouth.gov.uk/online-applications",
    "Breckland":         "https://planning.breckland.gov.uk/online-applications",
    "South Norfolk":     "https://planning.south-norfolk.gov.uk/online-applications",
    "St Albans":         "https://planningregister.stalbans.gov.uk/online-applications",
    "Watford":           "https://pa.watford.gov.uk/publicaccess/",
    "Hertsmere":         "https://www6.hertsmere.gov.uk/online-applications",
    "Three Rivers":      "https://www3.threerivers.gov.uk/online-applications/",
    "East Herts":        "https://publicaccess.eastherts.gov.uk/online-applications/",
    "Stevenage":         "https://publicaccess.stevenage.gov.uk/online-applications",
    "North Herts":       "https://pa2.north-herts.gov.uk/online-applications/",
    "Huntingdonshire":   "https://publicaccess.huntingdonshire.gov.uk/online-applications",

    # ══ North East ══════════════════════════════════════════════

    # ══ West Midlands (additions) ════════════════════════════════════
    "Sandwell":          "https://webcaps.sandwell.gov.uk/publicaccess/",
    "Stoke-on-Trent":    "https://www.stoke.gov.uk/online-applications",
    "Tamworth":          "https://www.tamworth.gov.uk/online-applications",

    # ══ East Midlands (additions) ════════════════════════════════════
    "Gedling":           "https://pawam.gedling.gov.uk/online-applications/",
    "Broxtowe":          "https://publicaccess.broxtowe.gov.uk/online-applications/",
    "Mansfield":         "https://planning.mansfield.gov.uk/online-applications/",
    "Rushcliffe":        "https://planningon-line.rushcliffe.gov.uk/online-applications/",
    "Newark":            "https://publicaccess.newark-sherwooddc.gov.uk/online-applications/",

    # ══ East of England (additions) ══════════════════════════════════
    "Brentwood":         "https://publicaccess.brentwood.gov.uk/online-applications/",
    "Epping Forest":     "https://www.eppingforestdc.gov.uk/online-applications",

    # ══ London ═══════════════════════════════════════════════════════
    "Barking Dagenham":  "https://paplan.lbbd.gov.uk/online-applications",

    # ══ London (Idox portals only) ════════════════════════════
    # Non-Idox: Hackney, Waltham Forest, Harrow, Havering, Hillingdon,
    #           Hounslow, Merton, Redbridge, Wandsworth, Haringey, Camden, Richmond
    "Ealing":            "https://pam.ealing.gov.uk/online-applications",
    "Lewisham":          "https://planning.lewisham.gov.uk/online-applications",
    "Lambeth":           "https://planning.lambeth.gov.uk/online-applications",
    "Croydon":           "https://publicaccess3.croydon.gov.uk/online-applications",
    "Brent":             "https://pa.brent.gov.uk/online-applications",
    "Tower Hamlets":     "https://development.towerhamlets.gov.uk/online-applications",
    "Greenwich":         "https://planning.royalgreenwich.gov.uk/online-applications",
    "Newham":            "https://pa.newham.gov.uk/online-applications",
    "Bexley":            "https://pa.bexley.gov.uk/online-applications",
    "Kingston":          "https://publicaccess.kingston.gov.uk/online-applications",
    "Sutton":            "https://planningregister.sutton.gov.uk/online-applications",
    "Westminster":       "https://idoxpa.westminster.gov.uk/online-applications",
    "Southwark":         "https://planning.southwark.gov.uk/online-applications",
    "Barnet":            "https://publicaccess.barnet.gov.uk/online-applications",
    "Enfield":           "https://planningandbuildingcontrol.enfield.gov.uk/online-applications",
    "Bromley":           "https://searchapplications.bromley.gov.uk/online-applications",
    "Hammersmith":       "https://public-access.lbhf.gov.uk/online-applications",
    "City of London":    "https://www.planning2.cityoflondon.gov.uk/online-applications",
    "Islington":         "https://publicaccess.islington.gov.uk/online-applications",
    # ══ North West (additions) ══════════════════════════════════════
    "Wyre":              "https://planning.wyre.gov.uk/online-applications",
    "Fylde":             "https://www.fylde.gov.uk/online-applications",
    "Rossendale":        "https://publicaccess.rossendale.gov.uk/online-applications/",
    "Hyndburn":          "https://planning.hyndburn.gov.uk/online-applications",
    "Ribble Valley":     "https://www.ribblevalley.gov.uk/online-applications",

    # ══ East Midlands (additions) ════════════════════════════════════
    "Erewash":           "https://www.erewash.gov.uk/online-applications",
    "Amber Valley":      "https://www.ambervalley.gov.uk/online-applications",
    "South Derbyshire":  "https://www.south-derbys.gov.uk/online-applications",
    "Blaby":             "https://pa.blaby.gov.uk/online-applications/",
    "Hinckley Bosworth": "https://pa.hinckley-bosworth.gov.uk/online-applications/",
    "Harborough":        "https://pa2.harborough.gov.uk/online-applications/search.do?action=simple&searchType=Application",

    # ══ West Midlands (additions) ════════════════════════════════════
    "Lichfield":         "https://planning.lichfielddc.gov.uk/online-applications/search.do?action=simple",
    "Cannock Chase":     "https://www.cannockchasedc.gov.uk/online-applications",
    "East Staffordshire":"https://www.eaststaffsbc.gov.uk/online-applications",

    # ══ South East (additions) ═══════════════════════════════════════
    "Waverley":          "https://planning.waverley.gov.uk/online-applications",
    "Mole Valley":       "https://www.molevalley.gov.uk/online-applications",
    "Surrey Heath":      "https://publicaccess.surreyheath.gov.uk/online-applications/",
    "Epsom Ewell":       "https://eplanning.epsom-ewell.gov.uk/online-applications/",
    "Spelthorne":        "https://www.spelthorne.gov.uk/online-applications",

    # ══ South West (additions) ═══════════════════════════════════════
    "North Devon":       "https://www.northdevon.gov.uk/online-applications",
    "East Devon":        "https://planning.eastdevon.gov.uk/online-applications",
    "Mid Devon":         "https://planning.middevon.gov.uk/online-applications",
    "Teignbridge":       "https://www.teignbridge.gov.uk/online-applications",

    # ══ East of England (additions) ══════════════════════════════════
    "Broadland":         "https://www.broadland.gov.uk/online-applications",
    "Kings Lynn":        "https://online.west-norfolk.gov.uk/online-applications/",
    "Fenland":           "https://www.publicaccess.fenland.gov.uk/publicaccess/",
}

HEADERS_HTTP = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# ── Per-council rate limit tracker ───────────────────────────────────────────
# When a council returns HTTP 429, record when the ban expires.
_rate_limited_until = {}   # base_url -> datetime when ban expires
_429_count = {}            # base_url -> consecutive 429 count (reset on success)
_disclaimer_blocked = {}   # host -> True when disclaimer permanently blocks this session

# ── Councils that need longer inter-request delays ────────────────────────────
# Cornwall and a few large unitaries aggressively rate-limit cloud IPs.
# Adding them here doubles the sleep between keyword requests for that council.
# Councils with known disclaimer gate issues (non-standard cookie acceptance)
# These need extra warmup time and cookie injection
_DISCLAIMER_GATE_COUNCILS = {
    "surrey heath", "west suffolk", "wigan", "basingstoke",
}

SLOW_COUNCILS = {
    # URL → inter-keyword sleep seconds (default 1.0 elsewhere)
    "https://planning.stockport.gov.uk/PlanningData-live/":  5.0,
    "https://planning.cornwall.gov.uk/online-applications":  3.0,
    "https://www.eastriding.gov.uk/online-applications":     3.0,
    "https://eplanning.birmingham.gov.uk/online-applications": 3.0,
}

# ════════════════════════════════════════════════════════════
# LOGGING
# ════════════════════════════════════════════════════════════
def log(msg, i=0):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {'  '*i}{msg}", flush=True)

_ENGINE_START      = datetime.now()
_MAX_RUNTIME_HOURS = float(os.environ.get("MAX_RUNTIME_HOURS", "5.5"))

def time_ok(need_s: int = 120) -> bool:
    """True if we have at least need_s seconds of budget remaining."""
    elapsed = (datetime.now() - _ENGINE_START).total_seconds()
    budget  = _MAX_RUNTIME_HOURS * 3600
    return (budget - elapsed) > need_s

# ════════════════════════════════════════════════════════════
# SESSION
# ════════════════════════════════════════════════════════════
def new_session():
    s = requests.Session()
    s.headers.update(HEADERS_HTTP)
    s.verify = False
    return s

def _is_dns_error(e):
    """True if the error is a DNS resolution failure — pointless to retry."""
    msg = str(e)
    return any(x in msg for x in [
        "NameResolutionError", "Name or service not known",
        "nodename nor servname", "getaddrinfo failed",
        "[Errno -2]", "[Errno 11001]",
    ])

def safe_get(sess, url, timeout=25, retries=2):
    for attempt in range(retries):
        try:
            r = sess.get(url, timeout=timeout, allow_redirects=True)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 60))
                # Cap wait at 45s max — don't waste the whole run on one council
                wait = min(wait, 45)
                base = url.split("/online-applications")[0] + "/online-applications"
                log(f"  🚫 429 on GET — waiting {wait}s before retry ({url[:50]})", 2)
                # Track how many 429s this council has hit this session
                _429_count[base] = _429_count.get(base, 0) + 1
                if _429_count[base] >= 3:
                    # 3+ consecutive 429s = council is actively throttling this IP
                    # Mark blocked for 10 minutes and move on
                    _rate_limited_until[base] = datetime.now() + timedelta(minutes=10)
                    log(f"  🛑 3 consecutive 429s — marking {base[-40:]} blocked 10min", 2)
                    return None
                _rate_limited_until[base] = datetime.now() + timedelta(seconds=wait)
                if attempt < retries - 1:
                    time.sleep(wait)
                    continue  # retry after waiting
            else:
                # Reset 429 counter on success
                base = url.split("/online-applications")[0] + "/online-applications"
                _429_count.pop(base, None)
            return r
        except requests.exceptions.ConnectionError as e:
            if _is_dns_error(e):
                log(f"  ❌ DNS failure (dead URL): {url[:70]}", 2)
                return None
            if attempt < retries - 1:
                time.sleep(4)
            else:
                log(f"  ❌ GET failed: {e}", 2)
        except requests.exceptions.Timeout:
            if attempt < retries - 1:
                log(f"  ⏱️  Timeout, retry {attempt+2}...", 2)
                time.sleep(5)
            else:
                log(f"  ❌ Timeout after {retries} attempts: {url[:60]}", 2)
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(4)
            else:
                log(f"  ❌ GET failed: {e}", 2)
    return None

# ════════════════════════════════════════════════════════════
# PRE-FLIGHT: test every council URL before scraping
# ════════════════════════════════════════════════════════════
def preflight_check(councils):
    """
    Single-attempt check per council. Classifies results into three buckets:

      ok          → responds from current IP, include in this run
      geo_blocked → ConnErr or Timeout from GitHub US IPs; confirmed Idox portals
                    that require a UK IP address. INCLUDED in the scrape run —
                    the scraper will try them and skip gracefully if they fail.
                    Run from Colab (UK IP) to get 100% coverage from these.
      dead        → DNS failure or no Idox form detected — genuinely gone, skip.

    Why this matters: previously ConnErr/Timeout both went into dead{} and were
    SKIPPED. ~20 valid councils were silently dropped on every GitHub Actions run.
    Now they are included and will either work (from Colab) or fail gracefully.
    """
    import concurrent.futures
    log("\n🔍 PRE-FLIGHT  (single-attempt, parallel)")
    log("=" * 60)
    live        = {}
    geo_blocked = {}  # ConnErr/Timeout — include in scrape, log separately
    dead        = {}  # DNS / no_form — genuinely skip

    def _test(name_url):
        name, base_url = name_url
        test_url = f"{base_url}/search.do?action=advanced&searchType=Application"
        headers_variants = [
            None,
            {"Accept": "text/html,*/*;q=0.9", "Accept-Language": "en-GB,en;q=0.5"},
        ]
        for extra_headers in headers_variants:
            try:
                sess = new_session()
                if extra_headers:
                    sess.headers.update(extra_headers)
                r = sess.get(test_url, timeout=15, allow_redirects=True, verify=False)
                if r.status_code == 200:
                    soup = BeautifulSoup(r.text, "html.parser")
                    text_lower = r.text.lower()
                    has_search_form = bool(
                        soup.find("input", {"name": re.compile(r"description|caseDecision|keyWord", re.I)})
                        or soup.find("form", {"id": re.compile(r"search|criteria", re.I)})
                        or soup.find("select", {"name": re.compile(r"caseDecision|decision|status", re.I)})
                    )
                    is_disclaimer = any(kw in text_lower for kw in (
                        "disclaimer", "terms and conditions", "accept", "cookies",
                        "i accept", "agree to", "before you continue"
                    ))
                    is_idox = any(kw in text_lower for kw in (
                        "planning application", "search.do", "idox",
                        "application reference", "applicationdetails",
                        "online-applications", "keyval"
                    ))
                    if has_search_form or (is_disclaimer and is_idox):
                        return name, base_url, "ok", r.status_code
                    if soup.find("form") and is_idox:
                        return name, base_url, "ok", r.status_code
                    return name, base_url, "no_form", r.status_code
                if r.status_code == 406 and extra_headers is None:
                    continue
                return name, base_url, "bad_status", r.status_code
            except requests.exceptions.ConnectionError as e:
                if _is_dns_error(e):
                    return name, base_url, "DNS", 0
                # Non-DNS ConnErr = geo-IP block. Do NOT skip — include in scrape.
                return name, base_url, "geo_blocked", 0
            except requests.exceptions.Timeout:
                # Timeout from GitHub US almost always = geo-IP block, not a dead server.
                return name, base_url, "geo_blocked", 0
            except Exception as e:
                return name, base_url, f"Err:{type(e).__name__}", 0
        return name, base_url, "bad_status", 406

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_test, item): item[0] for item in councils.items()}
        for fut in concurrent.futures.as_completed(futures):
            name, base_url, status, code = fut.result()
            if status == "ok":
                live[name] = base_url
                log(f"  ✅ {name:25s} reachable")
            elif status == "geo_blocked":
                geo_blocked[name] = base_url
                log(f"  🌍 {name:25s} geo-blocked (included — use Colab for full coverage)")
            else:
                reason = f"HTTP {code}" if code else status
                dead[name] = reason
                log(f"  ❌ {name:25s} {reason} — skipping")

    # Include geo_blocked in the live set — scrape_council will skip gracefully if still blocked
    combined_live = {**dict(sorted(live.items())), **dict(sorted(geo_blocked.items()))}

    log(f"\n  ✅ {len(live):3d} directly reachable")
    log(f"  🌍 {len(geo_blocked):3d} geo-blocked from this IP (included, try Colab for these)")
    log(f"  ❌ {len(dead):3d} truly dead (DNS / no Idox form)")
    log(f"  ─── Scraping {len(combined_live)} councils total")
    log("=" * 60)
    return combined_live, dead

# ════════════════════════════════════════════════════════════
# GOOGLE SHEETS — with retry + in-memory dedup cache
# ════════════════════════════════════════════════════════════
SHEET_HEADERS = [
    "Council", "Reference", "Address", "Description", "App Type",
    "Applicant", "Agent", "Date Received", "Date Decided", "Decision",
    "Trigger Words", "Score", "Keyword", "Portal Link", "Decision Doc URL",
    "Date Found", "Mark's Comments",
    # ── AI Evaluation (most important column) ─────────────────────────────
    "AI Evaluation",         # gpt-4o: why refused / appeal grounds / first action
    # ── Winability intelligence ──────────────────────────────────────────
    "Winability",            # HIGH / MEDIUM / LOW
    "Recommended Action",    # What Mark should do today
    "Top Trigger",           # Single most impactful trigger phrase
    # ── Sales intelligence ───────────────────────────────────────────────
    "Est. Project Value", "Developer", "Architect",
    "Impact Probability", "CH Number", "Registered Address", "Contact Link",
    # ── Appeal window ───────────────────────────────────────────────────
    "Days to Appeal", "Appeal Urgency",
    # ── Enforcement flag ─────────────────────────────────────────────────
    "Is Enforcement",        # YES if enforcement notice appeal
]

_ws           = None   # cached worksheet
_existing_refs = set() # in-memory dedup — loaded once at startup

def sheets_retry(fn, retries=5, base_delay=10):
    """Exponential backoff for transient Google API errors (500/503/quota)."""
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:
            msg = str(e)
            transient = any(code in msg for code in [
                "500", "503", "quota", "rate", "UNAVAILABLE",
                "internal", "temporarily", "overloaded",
            ])
            if transient and attempt < retries - 1:
                delay = base_delay * (2 ** attempt)  # 10s, 20s, 40s, 80s, 160s
                log(f"  ⚠️  Sheets API error (attempt {attempt+1}/{retries}): {msg[:55]}")
                log(f"  ⏳ Waiting {delay}s...")
                time.sleep(delay)
            else:
                raise

def _make_gspread_client():
    """
    Returns an authorised gspread client.
    - GitHub Actions / automated: reads GCP_SERVICE_ACCOUNT_JSON env var.
    - Google Colab interactive:   uses google.colab.auth + default().
    """
    sa_json = os.environ.get("GCP_SERVICE_ACCOUNT_JSON", "").strip()
    if sa_json:
        info  = json.loads(sa_json)
        creds = SACredentials.from_service_account_info(info, scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ])
        log("✅ Auth via service account (automated mode)")
        return gspread.authorize(creds)
    else:
        creds, _ = default()
        log("✅ Auth via Colab default credentials")
        return gspread.authorize(creds)


def get_sheet():
    global _ws
    if _ws:
        return _ws
    try:
        def _connect():
            gc_client = _make_gspread_client()
            ws = gc_client.open_by_key(SHEET_ID).worksheet("Leads")
            existing = ws.row_values(1)
            if existing != SHEET_HEADERS:
                ws.update(values=[SHEET_HEADERS], range_name="A1")
                log("✅ Headers written")
            else:
                log("✅ Sheets connected")
            return ws
        _ws = sheets_retry(_connect)
        return _ws
    except Exception as e:
        log(f"❌ Sheets connect failed after retries: {e}")
        return None

def load_existing_refs():
    """
    Load all existing reference numbers from column B into memory.
    Called once at startup — avoids per-lead API calls for dedup.
    """
    global _existing_refs
    ws = get_sheet()
    if not ws:
        return
    try:
        refs = sheets_retry(lambda: ws.col_values(2))
        _existing_refs = set(refs[1:])  # skip header row
        log(f"✅ Loaded {len(_existing_refs)} existing refs (dedup cache)")
    except Exception as e:
        log(f"⚠️  Could not load existing refs: {e} — duplicate check may miss some")

def get_weekly_lead_count():
    """
    Count how many leads were added to the sheet in the past 7 days.
    Reads the "Date Found" column (column 16) and counts rows where
    the date is within the last 7 days.

    This is included in the email digest even when the current run
    finds 0 new leads — so the email is always meaningful.
    """
    ws = get_sheet()
    if not ws:
        return 0, []
    try:
        # Get all rows including Date Found (col 16) and key fields
        all_rows = sheets_retry(lambda: ws.get_all_values())
        if len(all_rows) < 2:
            return 0, []

        cutoff = datetime.now() - timedelta(days=7)
        weekly_leads = []

        for row in all_rows[1:]:  # skip header
            if len(row) < 16:
                continue
            date_found_str = row[15].strip()  # column P = index 15 = "Date Found"
            if not date_found_str:
                continue
            try:
                # Handles both "2026-03-10 14:22" and "2026-03-10" formats
                date_found = datetime.strptime(date_found_str[:10], "%Y-%m-%d")
                if date_found >= cutoff:
                    # Safely convert Google Sheets text score to an integer
                    raw_score = row[11] if len(row) > 11 else "0"
                    try:
                        score_int = int(raw_score.strip())
                    except ValueError:
                        score_int = 0  # Fallback if the cell is blank or has weird text
                    weekly_leads.append({
                        "council": row[0] if row else "",
                        "ref":     row[1] if len(row) > 1 else "",
                        "desc":    row[3][:80] if len(row) > 3 else "",
                        "score":   row[11] if len(row) > 11 else "",
                        "date":    date_found_str,
                    })
            except Exception:
                continue

        log(f"✅ {len(weekly_leads)} leads added in the past 7 days (from sheet)")
        return len(weekly_leads), weekly_leads

    except Exception as e:
        log(f"⚠️  Could not read weekly leads from sheet: {e}")
        return 0, []

def write_lead(lead):
    ws = get_sheet()
    if not ws:
        return False

    # Fast in-memory dedup check
    if lead["ref"] in _existing_refs:
        log(f"  ⏭️  Duplicate: {lead['ref']}")
        return False

    row_data = [
        lead["council"], lead["ref"], lead["addr"], lead["desc"],
        lead["app_type"], lead["applicant"], lead["agent"],
        lead["date_rec"], lead["date_dec"], lead.get("decision", "REFUSED"),
        lead["triggers"], lead["score"], lead["keyword"],
        lead["url"], lead["doc_url"],
        datetime.now().strftime("%Y-%m-%d %H:%M"), "",
        # Sales intelligence columns
        lead.get("est_value",""),
        lead.get("developer",""),
        lead.get("architect",""),
        str(lead.get("impact_prob","")) + "%" if lead.get("impact_prob") else "",
        lead.get("ch_number",""),
        lead.get("reg_address",""),
        lead.get("contact_link",""),
        # AI Evaluation (Mark reads this first — gpt-4o analysis)
        lead.get("ai_evaluation",""),
        # Winability intelligence
        lead.get("winability",""),
        lead.get("recommended_action",""),
        lead.get("top_trigger",""),
        # Appeal window
        lead.get("days_to_appeal", "Unknown"),
        str(lead.get("appeal_urgency", "")),
        # Enforcement flag
        lead.get("is_enforcement",""),
    ]

    try:
        # Use lambda here to match your retry logic
        sheets_retry(lambda: ws.append_row(row_data))
        _existing_refs.add(lead["ref"]) 

        try:
            all_rows   = sheets_retry(lambda: ws.get_all_values())
            row_num    = len(all_rows)
            dec        = lead.get("decision", "").upper()
            is_refused = dec == "REFUSED" or dec.startswith("REFUSED")
            r, g, b    = (0.85, 0.93, 0.85) if is_refused else (0.96, 0.80, 0.80)
            
            fmt_body = {
                "requests": [{
                    "repeatCell": {
                        "range": {
                            "sheetId": ws.id,
                            "startRowIndex": row_num - 1,
                            "endRowIndex":   row_num,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": {"red": r, "green": g, "blue": b}
                            }
                        },
                        "fields": "userEnteredFormat.backgroundColor",
                    }
                }]
            }
            sheets_retry(lambda: ws.spreadsheet.batch_update(fmt_body))
        except Exception:
            pass 

        log(f"  💾 SAVED: {lead['ref']} | {lead['triggers'][:50]}")
        return True
    except Exception as e:
        log(f"  ❌ Sheets write failed after retries: {e}")
        return False

# ════════════════════════════════════════════════════════════
# SCORING  — client-type-aware dispatcher
# Adding a new client type: add a new _score_X() function below
# and register it in score_lead()'s if/elif block.
# ════════════════════════════════════════════════════════════
def score_lead(desc, triggers, client_type=None):
    """
    Dispatcher — routes to the correct scoring function based on client_type.
    Falls back to the CLIENT_TYPE global if not passed explicitly.
    """
    ct = (client_type or CLIENT_TYPE or "retail").lower()
    if ct == "rural":
        return _score_rural(desc, triggers)
    else:
        return _score_retail(desc, triggers)


# ── Retail scorer (MAPlanning — Class E, sequential test) ──────────────────
def _score_retail(desc, triggers):
    """
    Score for retail/Class E appeal consultants.
    Calibrated from Mark (MAPlanning) feedback.
    """
    s  = 40
    d  = desc.lower()
    tw = " ".join(triggers).lower()

    # NPPF / planning policy signals — +8 per group when found in decision notice
    # nppf alone in a refusal = the refusal cites national retail policy = relevant lead
    if any(w in tw for w in ("nppf","national planning policy framework",
                              "paragraph 91","paragraph 88","paragraph 89","paragraph 90",
                              "planning policy","local plan","retail policy","development plan")):
        s += 8
    if any(w in tw for w in ("primary shopping area","main town centre use",
                              "primary frontage","secondary frontage",
                              "defined town centre","town centre first")):
        s += 8

    # Evidence failure family — most winnable refusal type
    _evidence_phrases = (
        "lack of evidence", "insufficient evidence", "no evidence",
        "lack of information", "insufficient information",
        "failure to demonstrate", "failed to demonstrate", "fails to demonstrate",
        "not demonstrated", "has not demonstrated", "cannot demonstrate",
        "unable to demonstrate",
        "no information", "no assessment", "no retail impact",
        "has not been submitted", "not been provided", "has not been provided",
        "was not submitted", "not been submitted",
        "absence of", "in the absence of",
        "no identified need", "no quantitative need", "no qualitative need",
        "need has not been demonstrated", "no overriding need",
        "insufficient justification", "failed to justify", "not justified",
    )
    for w in _evidence_phrases:
        if w in tw:
            s += 25
            break

    # Sequential test failure
    _seq_phrases = (
        "sequential test", "sequential approach", "sequential assessment",
        "sequential preference", "sequential search", "sequential step",
        "sequentially preferable", "sequentially preferred",
        "fail the sequential", "failed the sequential", "fails the sequential",
    )
    for w in _seq_phrases:
        if w in tw:
            s += 20
            break
    if "no sequential" in tw: s += 15

    # Out-of-centre location
    _outofcentre = (
        "out of centre", "out-of-centre", "outside the town centre",
        "outside a defined centre", "out of town", "out-of-town",
        "not within the town centre", "not in a town centre",
    )
    for w in _outofcentre:
        if w in tw:
            s += 15
            break
    _edgeofcentre = ("edge of centre", "edge-of-centre", "edge of the town centre")
    for w in _edgeofcentre:
        if w in tw:
            s += 10
            break

    # Retail impact
    if "retail impact assessment" in tw or "retail impact study" in tw: s += 15
    elif "retail impact"          in tw:                                 s += 10
    elif "impact assessment"      in tw:                                 s += 8
    for w in ("impact on the vitality", "impact on vitality",
              "impact on the viability", "impact on viability",
              "harm to the vitality", "harm to vitality",
              "adverse impact on the town centre"):
        if w in tw:
            s += 8
            break
    for w in ("vitality and viability", "vitality or viability",
              "health of the town centre", "undermine the vitality",
              "prejudice the vitality"):
        if w in tw:
            s += 5
            break

    # Description signals
    if "class e"        in d: s += 10
    if "use class e"    in d: s += 10
    if "change of use"  in d: s += 8
    for w in ("gym", "fitness", "hair", "beauty", "salon",
              "nail", "barber", "café", "cafe", "coffee",
              "restaurant", "hot food", "takeaway", "office", "clinic"):
        if w in d:
            s += 5
            break
    if "supermarket"  in d: s += 8
    if "food store"   in d: s += 8
    if "retail park"  in d: s += 5
    if "convenience"  in d: s += 5
    if "shop"         in d: s += 3
    # Drive-through: almost always out-of-centre, always needs sequential test
    if any(w in d for w in ("drive-through","drive through","drive thru","drivethrough")): s += 12
    # Discount food retail: highest sequential test refusal rate
    if any(w in d for w in ("aldi","lidl","iceland","home bargains","b&m","farmfoods",
                             "food warehouse","poundland","savers")): s += 10

    # Penalise non-leads
    for bad in (
        "discharge of condition", "discharge of planning condition",
        "reserved matters", "approval of details", "condition discharge",
        "details reserved by condition", "approval of reserved",
        "prior approval", "lawful development certificate",
        "certificate of lawful",
    ):
        if bad in d:
            s -= 60
            break

    return max(10, min(s, 100))


# ── Rural scorer (Aynsley Planning — NP, agricultural, heritage) ───────────
def _score_rural(desc, triggers):
    """
    Score for rural / full-service planning consultants like Aynsley Planning.

    Scoring tiers calibrated to John's practice:
    Tier 1 (+25): Evidence failure — always winnable, any sector
    Tier 2 (+22): Para 84/80 exceptional quality — highest fee, rare
    Tier 2 (+20): National Park / AONB — John's proven specialism
    Tier 2 (+20): Sequential test failure — strong predictable ground
    Tier 3 (+18): Barn conversion structural failure (Class Q/R)
    Tier 3 (+15): Agricultural functional need failure
    Tier 3 (+16): Heritage asset refusal
    Tier 3 (+12): Green belt / countryside encroachment
    Tier 4 (+10): Enforcement notices (quick cash instructions)
    Tier 4 (+8):  Landscape/visual impact only (lower win rate)
    Tier 4 (+8):  Housing supply / tilted balance
    """
    s  = 40
    d  = desc.lower()
    tw = " ".join(triggers).lower()

    # TIER 1: Evidence failure (+25) — same in every sector
    _evidence_phrases = (
        "lack of evidence", "insufficient evidence", "no evidence has been",
        "failure to demonstrate", "failed to demonstrate", "fails to demonstrate",
        "not been demonstrated", "has not been demonstrated",
        "insufficient information", "no information has been provided",
        "has not been provided", "not been submitted", "in the absence of",
        "not justified", "fails to justify", "failed to justify",
        "insufficient justification",
    )
    for w in _evidence_phrases:
        if w in tw:
            s += 25
            break

    # TIER 2: Para 84 / Para 80 — exceptional quality homes (+22)
    # John's highest-value cases. Long commissions, architect-led, high spec.
    _para84_phrases = (
        "paragraph 84", "para 84", "paragraph 80", "para 80",
        "exceptional quality", "outstanding design",
        "design fails to meet", "does not meet the criteria of paragraph",
        "fails to meet the criteria",
    )
    for w in _para84_phrases:
        if w in tw or w in d:
            s += 22
            break

    # TIER 2: National Park / AONB location (+20)
    # John has proven NP track record. Fewer competitors. Clients need specialists.
    _np_phrases = (
        "national park", "northumberland national park", "aonb",
        "area of outstanding natural beauty", "sssi",
        "site of special scientific interest", "scheduled ancient monument",
    )
    for w in _np_phrases:
        if w in tw or w in d:
            s += 20
            break

    # TIER 2: Sequential test failure (+20)
    _seq_phrases = (
        "sequential test", "sequential approach", "sequential preference",
        "sequential assessment", "fails the sequential", "sequentially preferable",
    )
    for w in _seq_phrases:
        if w in tw:
            s += 20
            break

    # TIER 3: Barn conversion / Class Q/R structural failure
    # "Not capable of conversion" is THE classic Class Q refusal — very winnable.
    # Split into two sub-tiers:
    #   +22 for the specific "not capable / rebuild" phrases (clearest grounds)
    #   +18 for other structural failure language
    _conversion_strong = (
        "not capable of conversion",
        "capable of conversion without substantial reconstruction",
        "rebuild rather than conversion",
        "not a conversion",
        "reconstruction rather than",
    )
    _conversion_other = (
        "structural integrity", "extent of demolition", "substantial reconstruction",
    )
    for w in _conversion_strong:
        if w in tw:
            s += 22
            break
    else:
        for w in _conversion_other:
            if w in tw:
                s += 18
                break

    # TIER 3: Agricultural / rural worker need failure (+15)
    _need_phrases = (
        "functional need", "lack of functional need",
        "essential need", "agricultural need", "rural worker dwelling",
        "occupational need", "financial viability of the holding",
        "functional test", "financial test",
        "business plan has not been provided",
    )
    for w in _need_phrases:
        if w in tw:
            s += 15
            break

    # TIER 3: Heritage refusal (+16)
    # John has worked with NP authority and Historic England.
    _heritage_phrases = (
        "heritage asset", "setting of the listed building",
        "less than substantial harm", "substantial harm to",
        "significance of the heritage asset",
        "character and appearance of the conservation area",
        "scheduled ancient monument", "historic environment",
    )
    for w in _heritage_phrases:
        if w in tw:
            s += 16
            break

    # TIER 3: Green belt / countryside encroachment (+12)
    _gb_phrases = (
        "very special circumstances", "inappropriate development in the green belt",
        "harm to the openness", "openness of the green belt",
        "encroachment into the countryside",
    )
    for w in _gb_phrases:
        if w in tw:
            s += 12
            break

    # TIER 3: Isolated dwelling — para 80 signal (+12)
    _isolated_phrases = (
        "isolated home in the countryside", "isolated dwelling",
        "isolated location", "no functional relationship",
    )
    for w in _isolated_phrases:
        if w in tw or w in d:
            s += 12
            break

    # TIER 4: Enforcement / regularisation (+10)
    # Listed as core Aynsley service. Quick turnaround, cash-positive.
    _enforcement_phrases = (
        "enforcement notice", "breach of condition",
        "breach of planning control", "unauthorised development",
    )
    for w in _enforcement_phrases:
        if w in d or w in tw:
            s += 10
            break

    # TIER 4: Landscape / visual impact only (+8)
    # Hardest refusal to overturn — councils have wide discretion. Score but don't prioritise.
    _landscape_phrases = (
        "landscape and visual impact", "lvia",
        "harm to the character", "harm to the setting",
        "harm to the landscape", "adverse effect on the landscape",
        "intrusive in the landscape", "prominent and intrusive",
    )
    for w in _landscape_phrases:
        if w in tw:
            s += 8
            break

    # TIER 4: Housing supply / tilted balance (+8)
    _housing_phrases = (
        "5-year housing supply", "five-year housing land supply",
        "housing land supply", "tilted balance", "nppf paragraph 11", "paragraph 11d",
    )
    for w in _housing_phrases:
        if w in tw:
            s += 8
            break

    # Description type bonuses
    if any(w in d for w in ("barn conversion", "barn to", "redundant barn",
                             "agricultural building to", "class q", "class r")):
        s += 10
    elif any(w in d for w in ("rural worker", "agricultural worker", "essential worker")):
        s += 8
    elif any(w in d for w in ("holiday let", "shepherd hut", "glamping",
                               "tourism", "holiday accommodation")):
        s += 7
    elif any(w in d for w in ("listed building", "conservation area")):
        s += 6
    elif any(w in d for w in ("residential", "dwelling", "new home")):
        s += 4
    elif "change of use" in d:
        s += 4

    # Penalise definitively non-lead types
    for bad in (
        "discharge of condition", "discharge of planning condition",
        "reserved matters", "approval of details", "condition discharge",
        "details reserved by condition", "approval of reserved",
        "lawful development certificate", "certificate of lawful",
        "non-material amendment", "minor material amendment",
        "advertisement consent", "tree preservation order",
        "single storey rear extension", "single-storey rear extension",
        "rear extension to", "householder application",
    ):
        if bad in d:
            s -= 50
            break

    return max(10, min(s, 100))

# ════════════════════════════════════════════════════════════
# SALES INTELLIGENCE ENRICHMENT
# ════════════════════════════════════════════════════════════

# Build rates by client type
_BUILD_RATES_RETAIL = {
    "supermarket":   1800, "food store":  1800, "retail park": 1200,
    "retail":        1100, "class e":     1000, "mixed use":   1400,
    "restaurant":    1600, "convenience": 1100, "comparison":  1000,
    "shop":          1000,
}

_BUILD_RATES_RURAL = {
    "dwelling":            1800, "dwellings":         1800,
    "residential":         1700, "new home":          1750,
    "housing":             1700, "barn conversion":   1400,
    "class q":             1300, "class r":           1100,
    "agricultural building": 1200, "holiday":          900,
    "shepherd hut":         600, "glamping":           600,
    "tourism":              900, "stable":             700,
    "equestrian":           700,
}

_LONDON_BOROUGHS = {
    "westminster","camden","southwark","ealing","islington","hackney",
    "lewisham","lambeth","newham","croydon","barnet","enfield","brent",
    "tower hamlets","greenwich","waltham forest","wandsworth","haringey",
}

_NE_COUNCILS = {
    "northumberland","newcastle","sunderland","durham","gateshead",
    "north tyneside","south tyneside","middlesbrough","darlington",
    "stockton","hartlepool","redcar",
}


def estimate_project_value(desc, council, triggers):
    """
    Routes to retail or rural value estimator based on CLIENT_TYPE.
    Returns (lo_string, hi_string) e.g. ("\u00a3120k", "\u00a3350k").
    """
    d   = desc.lower()
    loc = council.lower()
    london_premium = 1.35 if any(b in loc for b in _LONDON_BOROUGHS) else 1.0
    ne_discount    = 0.90 if any(b in loc for b in _NE_COUNCILS)     else 1.0

    ct = (CLIENT_TYPE or "retail").lower()
    if ct == "rural":
        return _estimate_value_rural(d, ne_discount)
    else:
        return _estimate_value_retail(d, london_premium)


def _estimate_value_retail(d, london_premium):
    """Retail/Class E construction value estimate."""
    rate = 1000
    for kw, r in _BUILD_RATES_RETAIL.items():
        if kw in d:
            rate = r
            break
    rate = int(rate * london_premium)

    sqm_match = re.findall(
        r'(\d[\d,]*)\s*(?:sq\.?\s*m(?:etres?)?|sqm|m2|square\s+metre)', d
    )
    if sqm_match:
        try:
            sqm = int(sqm_match[0].replace(",",""))
            return _fmt_value(sqm * rate), _fmt_value(sqm * int(rate * 1.3))
        except Exception:
            pass

    if any(w in d for w in ["major","superstore","supermarket","retail park","district centre"]):
        lo, hi = 3_000_000, 15_000_000
    elif any(w in d for w in ["food store","convenience","large format"]):
        lo, hi = 1_000_000, 5_000_000
    elif any(w in d for w in ["retail","class e","shop","commercial"]):
        lo, hi = 250_000, 1_500_000
    else:
        lo, hi = 150_000, 750_000

    return _fmt_value(int(lo * london_premium)), _fmt_value(int(hi * london_premium))


def _estimate_value_rural(d, ne_discount):
    """Rural construction value for Northumberland / North East projects."""
    unit_match = re.findall(
        r'(\d+)\s*(?:dwellings?|new homes?|houses?|apartments?|flats?|units?)', d
    )
    if unit_match:
        try:
            units = int(unit_match[0])
            return (_fmt_value(int(units * 220_000 * ne_discount)),
                    _fmt_value(int(units * 320_000 * ne_discount)))
        except Exception:
            pass

    if any(w in d for w in ["paragraph 84","para 84","paragraph 80","exceptional quality","outstanding design"]):
        lo, hi = 500_000, 2_000_000
    elif any(w in d for w in ["barn conversion","class q","class r","agricultural building to"]):
        lo, hi = 120_000, 350_000
    elif any(w in d for w in ["rural worker","agricultural worker","isolated dwelling"]):
        lo, hi = 200_000, 450_000
    elif any(w in d for w in ["listed building","heritage","conservation area"]):
        lo, hi = 150_000, 600_000
    elif any(w in d for w in ["shepherd hut","glamping","holiday accommodation"]):
        lo, hi = 25_000, 120_000
    elif any(w in d for w in ["equestrian","stable"]):
        lo, hi = 30_000, 150_000
    elif any(w in d for w in ["residential","dwelling","housing","new home"]):
        lo, hi = 180_000, 500_000
    else:
        lo, hi = 80_000, 250_000

    return _fmt_value(int(lo * ne_discount)), _fmt_value(int(hi * ne_discount))


def _fmt_value(n):
    if n >= 1_000_000:
        return f"\u00a3{n/1_000_000:.1f}m"
    return f"\u00a3{n//1000}k"


def impact_probability(desc, triggers, score):
    """
    Routes to the correct secondary metric based on CLIENT_TYPE.
    Retail -> retail impact study probability (0-100).
    Rural  -> appeal urgency score (0-100): how quickly John should act.
    Column label stays 'Impact Probability' in the sheet for both.
    """
    ct = (CLIENT_TYPE or "retail").lower()
    if ct == "rural":
        return _appeal_urgency(desc, triggers, score)
    else:
        return _retail_impact_probability(desc, triggers, score)


def _retail_impact_probability(desc, triggers, score):
    """Probability of needing a formal retail impact study."""
    d  = desc.lower()
    tw = " ".join(triggers).lower() if triggers else ""
    p  = 40

    sqm_m = re.findall(r'(\d[\d,]*)\s*(?:sq\.?\s*m|sqm|m2)', d)
    if sqm_m:
        try:
            sqm = int(sqm_m[0].replace(",",""))
            if sqm >= 2500: p += 40
            elif sqm >= 1000: p += 25
            elif sqm >= 500:  p += 10
        except Exception:
            pass

    for kw, pts in [("supermarket",25),("food store",25),("retail park",20),
                    ("out of centre",20),("out-of-centre",20),
                    ("major",10),("district centre",10)]:
        if kw in d: p += pts

    if "sequential test"   in tw: p += 15
    if "retail impact"     in tw: p += 15
    if "impact assessment" in tw: p += 10
    if "main town centre"  in tw: p += 5
    if "primary shopping"  in tw: p += 5

    p += (score - 50) // 5
    return min(p, 98)


def _appeal_urgency(desc, triggers, score):
    """
    Appeal urgency score for rural/full-service clients (0-100).
    Enforcement notice (28 days) -> highest urgency.
    Prior approval Class Q/R (1 month) -> very urgent.
    Full application (6 months) -> standard.
    """
    d  = desc.lower()
    tw = " ".join(triggers).lower() if triggers else ""
    u  = 30

    if any(w in d for w in ("enforcement notice","breach of condition",
                             "enforcement action","breach of planning control",
                             "unauthorised development")):
        u += 45
    elif any(w in d for w in ("class q","class r","prior approval",
                               "class ma","permitted development")):
        u += 35

    for w in ("lack of evidence","failure to demonstrate","not been provided",
               "insufficient information","in the absence of","not justified"):
        if w in tw:
            u += 20
            break

    for w in ("national park","northumberland national park",
               "aonb","area of outstanding natural beauty"):
        if w in tw or w in d:
            u += 15
            break

    if any(w in tw or w in d for w in ("paragraph 84","para 84",
                                        "exceptional quality","paragraph 80")):
        u += 12

    u += max(0, (score - 58) // 2)
    return min(u, 98)

_CH_CACHE = {}  # avoid re-querying same company name

def lookup_companies_house(name):
    """
    Free Companies House API — no key required.
    Returns dict with: ch_number, reg_address, contact_link
    """
    if not name or len(name) < 4:
        return {}
    key = name.strip().lower()
    if key in _CH_CACHE:
        return _CH_CACHE[key]

    # Strip common suffixes to improve match quality
    clean = re.sub(
        r'(ltd|limited|plc|llp|llc|group|holdings|properties|developments?|'
        r'architects?|associates?|consulting|consultants?|design|enterprises?|'
        r'investments?|ventures?|solutions?|services?|uk)\b',
        "", name, flags=re.I
    ).strip(" .,")
    if len(clean) < 3:
        clean = name

    try:
        url  = f"https://api.company-information.service.gov.uk/search/companies?q={requests.utils.quote(clean)}&items_per_page=3"
        resp = requests.get(url, timeout=8,
                            headers={"User-Agent":"MAPlanning/1.0"})
        if resp.status_code != 200:
            _CH_CACHE[key] = {}
            return {}

        items = resp.json().get("items", [])
        if not items:
            _CH_CACHE[key] = {}
            return {}

        # Pick best match: prefer active companies, then closest name
        best = None
        for item in items:
            status = item.get("company_status","").lower()
            if status in ("active",""):
                best = item; break
        if not best:
            best = items[0]

        ch_num  = best.get("company_number","")
        addr_obj= best.get("registered_office_address",{})
        addr    = ", ".join(filter(None,[
            addr_obj.get("address_line_1",""),
            addr_obj.get("locality",""),
            addr_obj.get("postal_code",""),
        ]))
        ch_link = f"https://find-and-update.company-information.service.gov.uk/company/{ch_num}"

        result = {
            "ch_number":    ch_num,
            "reg_address":  addr,
            "contact_link": ch_link,
        }
        _CH_CACHE[key] = result
        time.sleep(0.3)   # respect CH rate limit
        return result

    except Exception as e:
        log(f"  ⚠️  Companies House lookup failed for '{name[:30]}': {e}", 2)
        _CH_CACHE[key] = {}
        return {}


def enrich_lead(lead):
    """
    Adds sales intelligence fields to a qualified lead dict.
    Called after PDF scan confirms the lead is real.
    """
    desc     = lead.get("desc","")
    triggers = lead.get("triggers","").split(", ")
    council  = lead.get("council","")
    score    = lead.get("score", 50)

    log(f"  🔬 Enriching…", 2)

    # 0. AI lead evaluation (most valuable field — Mark reads this first)
    _ai_key_oai  = os.environ.get("OPENAI_API_KEY","").strip()
    _ai_key_anth = os.environ.get("ANTHROPIC_API_KEY","").strip()
    lead["ai_evaluation"] = ""
    if _ai_key_oai or _ai_key_anth:
        try:
            _ai_prompt = f"""You are a specialist UK retail planning consultant at MAPlanning.
Analyse this refused planning application and write a concise 3-part assessment (max 120 words total):

COUNCIL: {council}
DESCRIPTION: {desc}
TRIGGER PHRASES FOUND IN DECISION NOTICE: {", ".join(triggers)}
DECISION: {lead.get("decision","REFUSED")}

Write exactly this structure (use the bold labels):
**Why refused:** [1 sentence — the actual planning reason, citing the specific policy failure e.g. 'No sequential test submitted' or 'Failed to demonstrate no impact on town centre vitality']
**Appeal grounds:** [1 sentence — what Mark can argue, citing NPPF paras if relevant, e.g. 'Strong appeal grounds on lack of evidence — inspector will look for sequential search; applicant has none to show']
**First action:** [1 sentence — who to call and what to say, e.g. 'Call applicant today — offer to prepare sequential test assessment and appeal statement; 75% winnable on current evidence']

Be direct and commercially useful. No padding."""

            if _ai_key_oai:
                _ai_r = requests.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {_ai_key_oai}",
                             "Content-Type": "application/json"},
                    json={"model": "gpt-4o",
                          "max_tokens": 200,
                          "temperature": 0.2,
                          "messages": [{"role": "user", "content": _ai_prompt}]},
                    timeout=25
                )
                if _ai_r.status_code == 200:
                    lead["ai_evaluation"] = _ai_r.json()["choices"][0]["message"]["content"].strip()
                    log(f"  🤖 AI evaluation: OpenAI gpt-4o ✅", 2)
            elif _ai_key_anth:
                _ai_r2 = requests.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": _ai_key_anth,
                             "anthropic-version": "2023-06-01",
                             "Content-Type": "application/json"},
                    json={"model": "claude-sonnet-4-6",
                          "max_tokens": 200,
                          "messages": [{"role": "user", "content": _ai_prompt}]},
                    timeout=25
                )
                if _ai_r2.status_code == 200:
                    lead["ai_evaluation"] = _ai_r2.json()["content"][0]["text"].strip()
                    log(f"  🤖 AI evaluation: Claude Sonnet ✅", 2)
        except Exception as _aie:
            log(f"  ⚠️  AI eval error: {_aie}", 2)

    # 1. Project value estimate
    lo, hi = estimate_project_value(desc, council, triggers)
    lead["est_value"] = f"{lo} – {hi}"
    log(f"  💰 Est. value: {lead['est_value']}", 2)

    # 2. Impact probability
    prob = impact_probability(desc, triggers, score)
    lead["impact_prob"] = prob
    log(f"  📊 Impact probability: {prob}%", 2)

    # 3. Companies House lookup for applicant (developer)
    applicant = lead.get("applicant","")
    ch_app    = lookup_companies_house(applicant) if applicant else {}
    lead["developer"]    = applicant  # keep original name
    lead["ch_number"]    = ch_app.get("ch_number","")
    lead["reg_address"]  = ch_app.get("reg_address","")
    lead["contact_link"] = ch_app.get("contact_link","")
    if ch_app:
        log(f"  🏢 CH: {lead['ch_number']} | {lead['reg_address'][:50]}", 2)

    # 4. Architect — treat agent as architect for planning purposes
    #    (planning agent is almost always an architect or planning consultant)
    lead["architect"] = lead.get("agent","")

    # 5. Calculate Appeal Window
    # NPPF: 6 months (182 days) from decision date for full planning appeals.
    lead["days_to_appeal"] = "Unknown"
    if lead.get("date_dec"):
        try:
            # Idox portals return dates in multiple formats — try all:
            # "27/04/2026"  "27-Apr-2026"  "2026-04-27"  "27 Apr 2026"
            _raw = lead["date_dec"].strip()
            dec_date = None
            for _fmt in ["%d/%m/%Y", "%d-%b-%Y", "%Y-%m-%d",
                         "%d %b %Y", "%d-%m-%Y", "%d/%m/%y"]:
                try:
                    dec_date = datetime.strptime(_raw, _fmt)
                    break
                except ValueError:
                    continue
            if dec_date:
                appeal_deadline = dec_date + timedelta(days=182)
                days_left = (appeal_deadline - datetime.now()).days
                if days_left > 60:
                    lead["days_to_appeal"] = (
                        f"{days_left}d remaining "
                        f"(deadline {appeal_deadline.strftime('%d %b %Y')})"
                    )
                elif days_left > 0:
                    lead["days_to_appeal"] = (
                        f"⚠️ URGENT: {days_left}d left "
                        f"(deadline {appeal_deadline.strftime('%d %b %Y')})"
                    )
                elif days_left > -30:
                    lead["days_to_appeal"] = (
                        f"⛔ CLOSED {abs(days_left)}d ago "
                        f"({appeal_deadline.strftime('%d %b %Y')})"
                    )
                else:
                    lead["days_to_appeal"] = "Window Closed"
        except Exception:
            pass

    # 6. Winability, recommended action, top trigger, enforcement flag
    _trig_str = lead.get("triggers","").lower()
    _desc_l2  = lead.get("desc","").lower()
    _TOP_TIER = ["lack of evidence","failure to demonstrate","not been provided",
                 "not been submitted","no sequential","fails the sequential",
                 "retail impact assessment","no identified need"]
    _MID_TIER = ["sequential test","out of centre","vitality and viability",
                 "insufficient evidence","no evidence","not justified"]
    _top_trigger = next((t for t in _TOP_TIER if t in _trig_str), 
                   next((t for t in _MID_TIER if t in _trig_str), 
                   (lead.get("triggers","").split(", ")[0] if lead.get("triggers") else "")))
    lead["top_trigger"] = _top_trigger
    _top_hit = any(t in _trig_str for t in _TOP_TIER)
    _mid_hit = any(t in _trig_str for t in _MID_TIER)
    if _top_hit and score >= 75:
        lead["winability"] = "HIGH — strong evidence failure grounds"
        lead["recommended_action"] = f"📞 CALL TODAY — '{_top_trigger}' = clear appeal grounds"
    elif _top_hit or (_mid_hit and score >= 65):
        lead["winability"] = "MEDIUM — sequential test or impact grounds"
        lead["recommended_action"] = f"📧 EMAIL THIS WEEK — grounds on '{_top_trigger}'"
    else:
        lead["winability"] = "LOW — general retail refusal, monitor"
        lead["recommended_action"] = f"👀 MONITOR — review if client contacts you"
    _enf_words = ("enforcement notice","breach of condition","enforcement action",
                  "breach of planning control","unauthorised development")
    lead["is_enforcement"] = "YES" if any(w in _desc_l2 for w in _enf_words) else ""

    # 7. Appeal urgency (retail-specific)
    # High urgency when: evidence failures + recent decision + high score
    _urgency = 30
    desc_l = (lead.get("desc","") or "").lower()
    trig_l = (lead.get("triggers","") or "").lower()
    if any(w in trig_l for w in ("lack of evidence","failure to demonstrate",
                                   "insufficient evidence","not been provided",
                                   "not justified")):
        _urgency += 25  # evidence failure = strong grounds, act fast
    if any(w in trig_l for w in ("sequential test","out of centre","out-of-centre")):
        _urgency += 20
    if "retail impact" in trig_l:
        _urgency += 15
    _urgency += max(0, (score - 60) // 3)  # higher score = more urgent
    lead["appeal_urgency"] = min(98, _urgency)

    return lead



# ════════════════════════════════════════════════════════════
# DISCLAIMER AUTO-ACCEPT
# Many Idox portals show a T&C gate before allowing searches.
# The preflight marks these as "ok" (correctly), but without
# auto-accept the scraper reads the DISCLAIMER form instead of
# the SEARCH form, posts to accept it, then finds 0 search
# results. This function detects and bypasses that gate.
# ════════════════════════════════════════════════════════════
def _is_disclaimer_page(html):
    """
    True ONLY if this is a BLOCKING Idox T&C gate.

    CRITICAL FIX: Idox search form fields are named "searchCriteria.description"
    and "searchCriteria.caseDecision". The old regex required the value to start
    with bare field names (e.g. "description") but they actually start with
    "searchCriteria." so has_search_form was always False, falsely flagging
    every search page as a disclaimer gate and blocking all searches.
    """
    tl = html.lower()
    # Detect search form fields — any of these = not a blocking disclaimer gate
    _form_signals = (
        "searchcriteria.description",
        "searchcriteria.casedecision",
        "applicationdecisionstart",
        "applicationdecisionend",
        "name=\"searchtype\"",
        "action=\"advanced\"",
    )
    if any(sig in tl for sig in _form_signals):
        return False  # has search form = definitely not a blocking gate
    # Hard gate indicators (only on actual T&C blocking pages)
    has_gate = any(kw in tl for kw in (
        "disclaimeraccept", "acceptedterms", "accept and continue",
        "you must accept the terms", "before you can search",
    ))
    has_weak = any(kw in tl for kw in (
        "terms and conditions", "agree to the terms",
        "before you continue", "i accept",
    ))
    return has_gate or (has_weak and "acceptedterms" in tl)
def _accept_disclaimer(sess, base_url, html, current_url):
    """
    POST to accept the Idox disclaimer gate, unlocking the session for searches.

    Idox disclaimer forms typically POST to:
      /online-applications/disclaimerAccepted.do
    with hidden fields ACCESSED and SUBMITTED plus a submit button.

    Returns True if acceptance succeeded (or was not needed), False on failure.
    """
    from urllib.parse import urlparse as _up
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form")
    if not form:
        return False

    action = form.get("action", "")
    p = _up(base_url)
    root = f"{p.scheme}://{p.netloc}"
    if action.startswith("http"):
        post_url = action
    elif action.startswith("/"):
        post_url = root + action
    else:
        post_url = base_url.rstrip("/") + "/" + action.lstrip("/")

    # Build POST body from all hidden fields
    fields = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        t = inp.get("type", "text").lower()
        if t == "submit":
            continue
        fields[name] = inp.get("value", "")

    # Ensure the acceptance flags are set
    for flag in ("SUBMITTED", "submitted", "ACCEPTED", "accepted", "AGREE", "agree"):
        if flag in fields:
            fields[flag] = "1"

    try:
        r = sess.post(post_url, data=fields,
                      headers={"Referer": current_url},
                      timeout=20, allow_redirects=True, verify=False)
        log(f"  📋 Disclaimer accepted → HTTP {r.status_code} ({post_url[-50:]})", 1)
        return r.status_code in (200, 302)
    except Exception as e:
        log(f"  ⚠️  Disclaimer POST failed: {e}", 1)
        return False

# ════════════════════════════════════════════════════════════
# SESSION WARMUP
# Idox portals require a properly initialised session before they
# will accept POST requests. A cold session (going straight to
# /search.do) often returns 200 on GET but 403 on POST because:
#   • The JSESSIONID cookie is not seeded from the portal root
#   • The disclaimer acceptance cookie is absent
#   • Some WAF rules require an Origin that matches a prior GET
#
# Fix: before any keyword search, visit the portal root first,
# then the search page, accept any disclaimer encountered.
# After this, POST requests work reliably.
# ════════════════════════════════════════════════════════════
def _warmup_portal_session(sess, base_url):
    """
    Initialise the Idox session before searching.

    1. GET portal root    → seeds JSESSIONID, any tracking cookies
    2. GET search page    → checks for disclaimer gate
    3. Accept disclaimer  → sets acceptance cookie if required
    4. GET search page    → confirms form is now accessible

    Returns True if session is ready for POST searches, False if
    the portal is unreachable or permanently blocked.
    """
    p    = urlparse(base_url)
    root = f"{p.scheme}://{p.netloc}"

    # Step 1 — seed JSESSIONID from portal root
    try:
        sess.get(root, timeout=12, verify=False, allow_redirects=True)
    except Exception:
        pass  # best-effort; the JSESSIONID may still come from step 2

    time.sleep(0.4)

    # Step 2 — load search page
    search_url = f"{base_url}/search.do?action=advanced&searchType=Application"
    r = safe_get(sess, search_url, timeout=18)
    if not r or r.status_code != 200:
        return False

    # Step 3 — accept disclaimer if shown
    if _is_disclaimer_page(r.text):
        log(f"  📋 Session warmup: disclaimer gate — accepting", 1)
        _accept_disclaimer(sess, base_url, r.text, r.url)
        time.sleep(0.8)
        # Step 4 — re-fetch search page to confirm acceptance
        r2 = safe_get(sess, search_url, timeout=18)
        # After disclaimer POST, the server sends us to /advancedSearchResults.do
        # That page is NOT a disclaimer but also NOT a search form, so we need to
        # re-GET the actual search page to confirm the session is unlocked.
        # We also check if JSESSIONID cookie is now set (reliable unlock signal).
        _has_session = bool(sess.cookies.get("JSESSIONID") or
                            sess.cookies.get("PHPSESSID") or
                            any("session" in k.lower() for k in sess.cookies.keys()))
        
        # Re-GET search page (not results page) to confirm unlock
        _check_url = f"{base_url}/search.do?action=advanced&searchType=Application"
        r2_check = None
        try:
            r2_check = sess.get(_check_url, timeout=15, allow_redirects=True, verify=False)
        except Exception:
            pass
        
        _unlocked = (
            _has_session or
            (r2_check and r2_check.status_code == 200 and
             not _is_disclaimer_page(r2_check.text))
        )
        
        if not r2 or r2.status_code != 200 or (not _unlocked):
            log(f"  ⚠️  Session warmup: disclaimer accept did not unlock portal", 1)
            return False

    log(f"  🔥 Session warmed up", 1)
    return True


# ════════════════════════════════════════════════════════════
# FORM DISCOVERY
# Reads ALL fields from the Idox search page HTML so hidden
# CSRF tokens are automatically included in the POST body.
# ════════════════════════════════════════════════════════════
def read_form(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form")
    if not form:
        return None

    action = form.get("action", "")
    if action.startswith("http"):
        form_action = action
    elif action.startswith("/"):
        p = urlparse(base_url)
        form_action = f"{p.scheme}://{p.netloc}{action}"
    else:
        form_action = f"{base_url}/{action.lstrip('/')}"

    fields = {}
    for el in form.find_all(["input", "select", "textarea"]):
        name = el.get("name")
        if not name:
            continue
        tag = el.name.lower()
        if tag == "input":
            t = el.get("type", "text").lower()
            if t == "submit":
                continue
            if t in ("checkbox", "radio") and not el.get("checked"):
                continue
            fields[name] = el.get("value", "")
        elif tag == "select":
            first = el.find("option")
            fields[name] = first.get("value", "") if first else ""
        elif tag == "textarea":
            fields[name] = el.get_text(strip=True)

    # Find description / keyword field
    desc_field = None
    for el in form.find_all("input"):
        nm = el.get("name", "").lower()
        ei = el.get("id",   "").lower()
        if "description" in nm or "description" in ei or "keyword" in nm:
            desc_field = el.get("name")
            break

    # Find decision dropdown — 3-pass matching to avoid picking wrong option
    decision_field = None
    refused_value  = None
    for sel in form.find_all("select"):
        nm = sel.get("name", "").lower()
        ei = sel.get("id",   "").lower()
        if "decision" not in nm and "decision" not in ei:
            continue
        if "appeal" in nm or "appeal" in ei:
            continue
        opts = [(opt.get_text(strip=True), opt.get("value","")) for opt in sel.find_all("option")]
        # Pass 1: exact label "Refused"
        exact = None
        for label, val in opts:
            if label.strip().lower() == "refused":
                exact = (sel.get("name"), val); break
        # Pass 2: label contains "refus" but not "split"/"part"
        partial = None
        if not exact:
            for label, val in opts:
                lt = label.strip().lower()
                if "refus" in lt and "split" not in lt and "part" not in lt:
                    partial = (sel.get("name"), val); break
        # Pass 3: known Idox refused value codes
        coded = None
        if not exact and not partial:
            for label, val in opts:
                if val.upper() in {"REF","REFUSED","R","RFD"}:
                    coded = (sel.get("name"), val); break
        chosen = exact or partial or coded
        if chosen:
            decision_field, refused_value = chosen
        if decision_field:
            break

    # Find decision date start / end fields
    date_start = None
    date_end   = None
    for el in form.find_all("input"):
        nm = (el.get("name", "") + el.get("id", "")).lower()
        if not date_start and any(h in nm for h in [
            "decisionstart", "decidedstart", "applicationdecisionstart"
        ]):
            date_start = el.get("name")
        if not date_end and any(h in nm for h in [
            "decisionend", "decidedend", "applicationdecisionend"
        ]):
            date_end = el.get("name")

    return {
        "form_action": form_action,
        "fields":      fields,
        "desc":        desc_field,
        "decision":    decision_field,
        "refused":     refused_value,
        "date_start":  date_start,
        "date_end":    date_end,
    }

# ════════════════════════════════════════════════════════════
# SEARCH ONE KEYWORD
# ════════════════════════════════════════════════════════════
def _do_post(sess, base_url, keyword, date_from, date_to, with_refused=True):
    """
    One attempt at the Idox search form POST.
    Returns (items_list, form_info_dict) or ([], None) on failure.
    with_refused=False skips the decision filter entirely — used as fallback
    when the refused-filtered search returns 0 results.
    """
    search_url = f"{base_url}/search.do?action=advanced&searchType=Application"

    r = safe_get(sess, search_url, timeout=25)
    if not r or r.status_code != 200:
        log(f"  ❌ Search page HTTP {r.status_code if r else 'no response'}", 1)
        return [], None

    # ── Disclaimer gate: detect, accept once, block if persistently fails ───
    _host_key = base_url.split("/online-applications")[0].rstrip("/")
    if _disclaimer_blocked.get(_host_key):
        return [], None  # already confirmed blocked this session — skip silently

    if _is_disclaimer_page(r.text):
        log(f"  📋 Disclaimer gate — accepting automatically", 1)
        ok = _accept_disclaimer(sess, base_url, r.text, r.url)
        if ok:
            time.sleep(1.5)
            r = safe_get(sess, search_url, timeout=25)
            if r and r.status_code == 200 and not _is_disclaimer_page(r.text):
                _disclaimer_blocked.pop(_host_key, None)  # cleared OK
            else:
                log(f"  ❌ Disclaimer persists — blocking portal for this session", 1)
                _disclaimer_blocked[_host_key] = True
                return [], None
        else:
            log(f"  ❌ Disclaimer accept failed — blocking portal for this session", 1)
            _disclaimer_blocked[_host_key] = True
            return [], None

    form = read_form(r.text, base_url)
    if not form:
        log(f"  ❌ No form on search page", 1)
        return [], None

    post = dict(form["fields"])
    post["searchType"] = "Application"
    post[form["desc"] or "searchCriteria.description"] = keyword

    if with_refused:
        if form["decision"] and form["refused"]:
            post[form["decision"]] = form["refused"]
        else:
            post["searchCriteria.caseDecision"] = "REF"
    # else: leave decision field at its default (blank / any) so ALL decisions come back

    post[form["date_start"] or "date(applicationDecisionStart)"] = date_from
    post[form["date_end"]   or "date(applicationDecisionEnd)"]   = date_to

    # Extract Origin from base_url — some Idox WAF rules require it
    _p    = urlparse(base_url)
    _origin = f"{_p.scheme}://{_p.netloc}"

    try:
        pr = sess.post(
            form["form_action"], data=post,
            headers={
                "Referer":      search_url,
                "Origin":       _origin,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=30, allow_redirects=True,
        )
        log(f"  POST → HTTP {pr.status_code}", 1)
        if pr.status_code == 429:
            wait = int(pr.headers.get("Retry-After", 90))
            log(f"  🚫 429 Rate Limited — marking council blocked for {wait}s", 1)
            _rate_limited_until[base_url] = datetime.now() + timedelta(seconds=wait)
            return [], None
    except Exception as e:
        log(f"  ❌ POST failed: {e}", 1)
        return [], None

    # ── 403 recovery: re-warm the session and retry once ─────────────────
    # 403 here almost always means the session cookies are stale or the
    # disclaimer was never accepted in this session. Re-warming fixes it.
    if pr.status_code == 403:
        log(f"  ⚠️  403 on POST — re-warming session and retrying once", 1)
        _warmup_portal_session(sess, base_url)
        time.sleep(1)
        # Fresh GET to get updated form token
        r2 = safe_get(sess, search_url, timeout=25)
        if r2 and r2.status_code == 200 and not _is_disclaimer_page(r2.text):
            form2 = read_form(r2.text, base_url)
            if form2:
                post2 = dict(form2["fields"])
                post2["searchType"] = "Application"
                post2[form2["desc"] or "searchCriteria.description"] = keyword
                if with_refused and form2["decision"] and form2["refused"]:
                    post2[form2["decision"]] = form2["refused"]
                elif with_refused:
                    post2["searchCriteria.caseDecision"] = "REF"
                post2[form2["date_start"] or "date(applicationDecisionStart)"] = date_from
                post2[form2["date_end"]   or "date(applicationDecisionEnd)"]   = date_to
                try:
                    pr = sess.post(
                        form2["form_action"], data=post2,
                        headers={
                            "Referer":      search_url,
                            "Origin":       _origin,
                            "Content-Type": "application/x-www-form-urlencoded",
                        },
                        timeout=30, allow_redirects=True,
                    )
                    log(f"  POST retry → HTTP {pr.status_code}", 1)
                    if pr.status_code == 403:
                        log(f"  ❌ Still 403 after session re-warm — portal blocking this IP", 1)
                        return [], None
                    form = form2  # use updated form for result fetching
                except Exception as e:
                    log(f"  ❌ POST retry failed: {e}", 1)
                    return [], None
        else:
            log(f"  ❌ Could not re-warm session — skipping this keyword", 1)
            return [], None

    time.sleep(2)  # give server time to store session

    # Some portals redirect the POST straight to results — check first
    # Use title detection (case-insensitive) not URL pattern — Bradford uses lowercase "results"
    if pr.status_code == 200:
        _pr_soup  = BeautifulSoup(pr.text, "html.parser")
        _pr_title = _pr_soup.title.get_text(strip=True) if _pr_soup.title else ""
        _is_results = (
            "result" in pr.url.lower() or
            "result" in _pr_title.lower() or
            (
                "Applications Search" not in _pr_title and
                _pr_title and
                bool(_pr_soup.select("li.searchresult, div.searchresult, li[class*='searchresult']"))
            )
        )
        if _is_results:
            items = collect_pages(sess, base_url, pr, keyword)
            if items:
                return items, form

    # Standard: GET the results page — try two common URL variants
    result_urls = [
        f"{base_url}/advancedSearchResults.do?action=firstPage",
        f"{base_url}/searchResults.do?action=firstPage",
        f"{base_url}/pagedSearchResults.do?action=firstPage", 
    ]
    for rurl in result_urls:
        rr = safe_get(sess, rurl)
        if not rr:
            continue
        # Check if we got results or bounced back to search form
        soup_title = ""
        try:
            from bs4 import BeautifulSoup as _BS
            soup_title = _BS(rr.text, "html.parser").title.get_text(strip=True) if _BS(rr.text,"html.parser").title else ""
        except Exception:
            pass
        is_results_page = (
            "Results" in soup_title or
            "result" in rr.url.lower() or
            ("Applications Search" not in soup_title and soup_title)
        )
        if is_results_page:
            items = collect_pages(sess, base_url, rr, keyword)
            if items:
                return items, form
            # Got a results page but 0 items — no point trying second URL
            break

    # Both result URLs returned 0 / bounced to search — nothing here
    return [], form


def search_one_keyword(sess, base_url, keyword, date_from, date_to):
    # Skip immediately if this council is still rate-limited
    if base_url in _rate_limited_until:
        if datetime.now() < _rate_limited_until[base_url]:
            log(f"  ⏭️  Rate limited — skipping '{keyword}'", 1)
            return []
        else:
            del _rate_limited_until[base_url]  # ban expired, clear it
    log(f"  🔎 '{keyword}'  {date_from} → {date_to}", 1)

    # ── Attempt 1: keyword + refused decision filter + date range ────────────
    items, form = _do_post(sess, base_url, keyword, date_from, date_to, with_refused=True)

    if form:
        log(
            f"  desc='{form['desc']}' decision='{form['decision']}' "
            f"refused='{form['refused']}' "
            f"start='{form['date_start']}' end='{form['date_end']}'", 1
        )

    if items:
        return items

    # ── Attempt 2: 0 results with refused filter — retry WITHOUT it ──────────
    # Reason: some portals use non-standard refused values (e.g. "RAW"),
    # or the refused+keyword combo genuinely has 0 results but keyword alone does.
    # The PDF scanner already filters for refusal trigger words, so this is safe.
    if form is not None:
        log(f"  ⚠️  0 results with decision filter — retrying without it", 1)
        time.sleep(2)
        # Need a fresh session cookie (JSESSIONID) for new search
        items2, _ = _do_post(sess, base_url, keyword, date_from, date_to, with_refused=False)
        if items2:
            log(f"  ✅ Got {len(items2)} results without decision filter — PDF scanner will qualify", 1)
        return items2

    return []


MAX_PAGES = 30   # hard cap — no portal has 30 pages of retail refusals

def collect_pages(sess, base_url, first_resp, keyword):
    all_items    = []
    seen_keyvals = set()   # ← dedup guard: breaks the infinite loop
    page_num     = 1
    resp         = first_resp

    while page_num <= MAX_PAGES:
        soup  = BeautifulSoup(resp.text, "html.parser")
        title = soup.title.get_text().strip() if soup.title else ""
        items = parse_results(soup)

        if not items:
            if page_num == 1:
                # If title looks like an application ref (e.g. "2026/0139/FUL | Change..."),
                # the page HAS results but parse_results missed them.
                # Try once more with a broader selector.
                _title_is_app_ref = bool(re.search(r'\d{2,4}[./]\d{3,6}', title))
                if _title_is_app_ref:
                    # Broader pass: grab any link with keyVal= anywhere on the page
                    _extra = []
                    for _a in soup.find_all("a", href=True):
                        _h = _a["href"]
                        if "keyVal=" in _h:
                            _kv = _h.split("keyVal=")[-1].split("&")[0]
                            _desc = _a.get_text(strip=True)[:200]
                            if _kv and _kv not in {i["keyVal"] for i in _extra}:
                                _extra.append({
                                    "keyVal": _kv, "ref": _kv,
                                    "desc": _desc, "addr": "", "keyword": keyword,
                                })
                    if _extra:
                        items = _extra
                        log(f"  📄 Page {page_num}: {len(items)} results (table fallback)", 1)
                if not items:
                    log(f"  ⚠️  0 results — title='{title}'", 1)
                    snippet = soup.get_text(separator=" ", strip=True)[:250]
                    log(f"  Page text: {snippet}", 1)
                    break
            else:
                log(f"  ✅ {len(all_items)} total across {page_num-1} pages", 1)
                break
            if not items:
                break

        # Duplicate-page detection: if ALL keyVals on this page are ones
        # we have already seen, the server is cycling — stop immediately.
        page_kvs = [i["keyVal"] for i in items]
        new_kvs  = [kv for kv in page_kvs if kv not in seen_keyvals]

        if not new_kvs and page_num > 1:
            log(f"  🔄 Page {page_num} is a duplicate of a previous page — stopping pagination", 1)
            log(f"  ✅ {len(all_items)} total (cycle detected)", 1)
            break

        # Even if some are new, only add genuinely new ones
        for item in items:
            if item["keyVal"] not in seen_keyvals:
                seen_keyvals.add(item["keyVal"])
                all_items.append(item)

        log(f"  📄 Page {page_num}: {len(items)} results ({len(new_kvs)} new)", 1)

        if page_num == MAX_PAGES:
            log(f"  ⚠️  Hit {MAX_PAGES}-page cap — stopping", 1)
            break

        # Check for next-page link
        has_next = bool(
            soup.find("a", string=re.compile(r"Next", re.I)) or
            soup.find("a", href=re.compile(r"searchCriteria\.page="))
        )
        if not has_next:
            log(f"  ✅ {len(all_items)} total", 1)
            break

        page_num += 1
        next_url = f"{base_url}/pagedSearchResults.do?action=page&searchCriteria.page={page_num}"
        resp = safe_get(sess, next_url)
        if not resp:
            break
        time.sleep(0.5)   # reduced from 1s

    log(f"  → {len(all_items)} for '{keyword}'", 1)
    return all_items

# ════════════════════════════════════════════════════════════
# PARSE RESULT CARDS
# ════════════════════════════════════════════════════════════
def extract_ref(text):
    for pat in [
        r'Ref\.?\s*[Nn]o[.:\s]+([A-Z0-9][A-Z0-9/\-]{3,30})',
        r'Reference[:\s]+([A-Z0-9][A-Z0-9/\-]{3,30})',
        r'\b([A-Z]{1,3}\d{4}/\d{4,})\b',
        r'\b(\d{5,}/[A-Z0-9]{2,}/\d{4})\b',
        r'\b([A-Z]{2}/\d{4}/\d{4,}/[A-Z0-9]+)\b',
        r'\b(\d{2}/\d{4,}/[A-Z]+)\b',
    ]:
        m = re.search(pat, text)
        if m:
            c = m.group(1).strip().rstrip(".")
            if 4 < len(c) < 35:
                return c
    return ""

def parse_results(soup):
    items = []
    rows = (
        soup.select("li.searchresult")                       or
        soup.select("div.searchresult")                      or
        soup.select("li[class*='searchresult']")             or
        soup.select("div[class*='searchresult']")            or
        # Table-based results (used by Stockport and some other councils)
        [tr for tr in soup.select("table tr")
         if tr.find("a", href=lambda h: h and "keyVal=" in h)]
    )
    for card in rows:
        a = (
            card.select_one("a[href*='keyVal']") or
            card.select_one("a[href*='applicationDetails']") or
            card.select_one("a")
        )
        if not a:
            continue
        href    = a.get("href", "")
        key_val = href.split("keyVal=")[-1].split("&")[0] if "keyVal=" in href else ""
        if not key_val:
            continue
        card_text = card.get_text(separator=" ", strip=True)
        desc      = a.get_text(strip=True)[:250]
        ref       = extract_ref(card_text) or key_val
        addr_el   = card.select_one(".address") or card.select_one(".addressCol")
        addr      = addr_el.get_text(strip=True) if addr_el else ""
        if not addr:
            m = re.search(r'([A-Z][^\|]{8,80}[A-Z]{1,2}\d{1,2}\s?\d[A-Z]{2})', card_text)
            addr = m.group(1).strip() if m else ""
        items.append({"ref": ref, "keyVal": key_val, "desc": desc, "addr": addr[:150]})
    return items

# ════════════════════════════════════════════════════════════
# APPLICATION DETAILS (summary + details tabs)
# ════════════════════════════════════════════════════════════
# ════════════════════════════════════════════════════════════
# DECISION CLASSIFIER — shared constants
# ════════════════════════════════════════════════════════════
_REFUSAL_WORDS  = ("refus",)
_APPROVAL_WORDS = (
    "approv", "grant", "permit", "permitted",
    "lawful", "certif",
    "prior approval", "no prior approval",
    "no objection", "withdrawn", "invalid",
    "discharge", "not required", "consent",
    # NOTE: "conditions" intentionally excluded — "reasons for refusal...conditions"
    # would false-match. "Approve with conditions" is caught by "approv" already.
)
# Decision values that Idox portals return — used in pass 3 scan
_APPROVAL_EXACT = [
    "permit", "permitted", "permitted development",
    "approve with conditions", "approved with conditions",
    "approved subject to conditions", "approved unconditionally",
    "granted", "approved", "grant",
    "prior approval required", "prior approval not required",
    "no prior approval required", "no objection",
    "withdrawn", "invalid", "lawful development certificate",
    "lawful use", "certificate of lawfulness",
]
_REFUSAL_EXACT = [
    "refused", "refuse", "refusal",
    "application refused",
    "planning permission refused",
    "delegated refusal",
    "committee refusal",
    "rfsd",                          # Idox internal decision code used by some councils
    "rfd",                           # alternative Idox code
    "appeal dismissed", "appeal is dismissed",
]


def _parse_decision_from_soup(soup):
    """
    Extract the Decision field from an Idox summary page.

    4 passes — each more aggressive:
      1. <tr><th>decision</th><td>VALUE</td>  (exact label, never matches "status")
      2. <dt>decision</dt><dd>VALUE</dd>
      3. Exact-line scan of full page text for known decision strings
      4. Substring scan — finds "Permit", "Refused" etc. anywhere in page

    Returns raw text e.g. "Refused", "Permit", "Approve with Conditions"
    or "" if genuinely not found.
    """
    # Pass 1: exact <th>decision</th> label
    for row in soup.find_all("tr"):
        th = row.find("th")
        td = row.find("td")
        if not th or not td:
            continue
        label = th.get_text(strip=True).lower().rstrip(":").strip()
        if label == "decision":
            val = td.get_text(strip=True)
            if val and val.lower() not in ("", "decided", "-", "n/a", "none", "pending"):
                return val

    # Pass 2: <dt>decision</dt><dd>VALUE</dd>
    for dt in soup.find_all("dt"):
        label = dt.get_text(strip=True).lower().rstrip(":").strip()
        if label == "decision":
            dd = dt.find_next_sibling("dd")
            if dd:
                val = dd.get_text(strip=True)
                if val and val.lower() not in ("", "decided", "-", "n/a", "pending"):
                    return val

    # Pass 3: exact-line scan
    page_text = soup.get_text(separator="\n", strip=True)
    for line in page_text.split("\n"):
        stripped = line.strip().lower()
        if stripped in _REFUSAL_EXACT:
            return line.strip()
        if stripped in _APPROVAL_EXACT:
            return line.strip()

    # Pass 4: substring scan — last resort
    page_lower = page_text.lower()
    for word in _REFUSAL_EXACT:
        if word in page_lower:
            idx = page_lower.find(word)
            return page_text[idx:idx+len(word)].strip()
    for word in _APPROVAL_EXACT:
        if word in page_lower:
            idx = page_lower.find(word)
            return page_text[idx:idx+len(word)].strip()

    return ""


def _normalise_decision(raw):
    """
    Convert raw portal decision text to canonical status string.
    Returns "REFUSED", "APPROVED — <detail>", or the raw text verbatim.
    """
    if not raw:
        return ""
    r = raw.lower()
    if any(w in r for w in _REFUSAL_WORDS):
        return "REFUSED"
    if any(w in r for w in _APPROVAL_WORDS):
        return f"APPROVED — {raw}"
    return raw


def get_details(sess, base_url, key_val):
    """
    Fetch summary + details tabs for one application.
    Returns dict with: decision, proposal, address, date_dec, date_rec,
                       applicant, agent, app_type
    """
    d = {}
    r = safe_get(sess, f"{base_url}/applicationDetails.do?activeTab=summary&keyVal={key_val}")
    if r and r.status_code == 200:
        soup = BeautifulSoup(r.text, "html.parser")

        # ── Decision: use 3-pass parser — never picks up "Status: Decided" ──
        raw_decision = _parse_decision_from_soup(soup)
        d["decision"] = raw_decision

        # ── Other fields from table rows ────────────────────────────────────
        for row in soup.select("tr"):
            th = row.find("th")
            td = row.find("td")
            if not th or not td:
                continue
            label = th.get_text(strip=True).lower().strip().rstrip(":")
            value = td.get_text(strip=True)
            if   label == "proposal":                                d["proposal"] = value
            elif label == "address":                                 d["address"]  = value
            elif label in ("decision issued date", "decision date",
                           "date of decision", "date decision issued"): d["date_dec"] = value
            elif label in ("application validated", "date validated",
                           "date received", "received"):
                d.setdefault("date_rec", value)

        log(f"  Decision='{d.get('decision','?')}' | AppType='{d.get('app_type','?')}' | Date='{d.get('date_dec','?')}'", 2)

    # ── Details tab: applicant, agent, app type ──────────────────────────────
    time.sleep(0.5)
    r2 = safe_get(sess, f"{base_url}/applicationDetails.do?activeTab=details&keyVal={key_val}")
    if r2 and r2.status_code == 200:
        soup2 = BeautifulSoup(r2.text, "html.parser")
        for row in soup2.select("tr"):
            th = row.find("th")
            td = row.find("td")
            if not th or not td:
                continue
            label = th.get_text(strip=True).lower().strip().rstrip(":")
            value = td.get_text(strip=True)
            if "applicant name" in label and not d.get("applicant"): d["applicant"] = value
            if "agent name"     in label and not d.get("agent"):     d["agent"]     = value
            if label == "agent"           and not d.get("agent"):     d["agent"]     = value
            if "application type" in label and not d.get("app_type"): d["app_type"] = value
    return d

# DOCUMENT FINDER
# Handles all known Idox HTML layouts for the documents tab,
# and resolves viewDocument.do to direct file URLs.
# ════════════════════════════════════════════════════════════

# ── Document type scoring ─────────────────────────────────────────────────────
# How to score a document by its label text. Higher = more likely to be
# the Decision Notice we want to scan.
_DOC_SCORES = {
    "decision notice":              100,
    "decision":                     80,
    "notice of decision":           80,
    "planning permission":          70,
    "refusal notice":               95,
    "refusal of planning permission":95,
    "officer report":               50,
    "planning officer report":      50,
    "delegated report":             50,
    "committee report":             45,
    "appeal decision":              85,
    "inspector's decision":         85,
    "planning inspectorate":        80,
}

def _score_text(text: str, custom_scores=None) -> int:
    """
    Score a document label/row text to identify the Decision Notice.
    Returns 0 if not relevant, higher = more likely to be what we want.
    Uses custom_scores dict if provided, else falls back to _DOC_SCORES.
    """
    scores = custom_scores if custom_scores else _DOC_SCORES
    t = text.lower().strip()
    best = 0
    for phrase, pts in scores.items():
        if phrase in t:
            best = max(best, pts)
    return best


# Document type priority scores (higher = better)

def _abs_url(root: str, base_url: str, href: str) -> str:
    """
    Convert a relative href to an absolute URL.
    
    Handles all Idox URL patterns:
    - Absolute URLs (http/https): returned as-is
    - Root-relative (/path): prepended with scheme+host
    - Relative paths: resolved against base_url directory
    - Query strings (?key=val): appended to base without path
    - viewDocument.do and similar Idox patterns
    
    Returns empty string if href is None, empty, or a javascript: URI.
    """
    if not href:
        return ""
    href = href.strip()
    if not href or href.startswith(("javascript:", "mailto:", "#")):
        return ""
    # Already absolute
    if href.startswith(("http://", "https://")):
        return href
    # Root-relative
    if href.startswith("/"):
        return root.rstrip("/") + href
    # Relative — resolve against the base_url directory
    from urllib.parse import urljoin
    return urljoin(base_url, href)


def _resolve_viewdoc(sess, url, base_url, soup_of_doc_tab=None):
    """
    Resolve an Idox /viewDocument.do?docRef=XXX URL to a direct file URL.

    Idox portals serve documents via a redirect chain:
      /viewDocument.do?docRef=12345  →  /files/open/1234/DecisionNotice.pdf

    If we try to download the viewDocument.do URL directly, we often get
    an HTML "Document Unavailable" page instead of a PDF.

    This function follows the redirect, extracts the direct PDF URL,
    and optionally pre-fetches the content so scan_pdf doesn't re-download.

    Returns (resolved_url, prefetched_response_or_None)
    """
    if not url:
        return url, None

    # Only resolve viewDocument.do URLs — direct file URLs are already fine
    if "viewDocument.do" not in url and "/files/" in url:
        return url, None

    if "viewDocument.do" not in url:
        # Try to prefetch for efficiency (avoids double-download in scan_pdf)
        try:
            r = sess.get(url, headers={"Accept": "application/pdf,*/*"},
                         timeout=40, allow_redirects=True, verify=False)
            if r.status_code == 200 and "html" not in r.headers.get("Content-Type","").lower():
                return url, r
        except Exception:
            pass
        return url, None

    # ── Follow viewDocument.do redirect ───────────────────────────────────────
    from urllib.parse import urlparse as _up, urljoin as _uj
    p = _up(base_url)
    root = f"{p.scheme}://{p.netloc}"

    try:
        r = sess.get(url, headers={"Accept": "application/pdf,*/*,text/html"},
                     timeout=30, allow_redirects=True, verify=False)

        # If we got a PDF directly, great
        ct = r.headers.get("Content-Type","").lower()
        if r.status_code == 200 and ("pdf" in ct or r.content[:4] == b"%PDF"):
            return r.url, r  # use final URL (after redirects) and pre-fetched content

        # We got HTML — parse it to find the actual file link
        if "html" in ct and r.status_code == 200:
            from bs4 import BeautifulSoup as _BS
            soup = _BS(r.text, "html.parser")
            # Look for a direct PDF link in the response
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if ".pdf" in href.lower() or "/files/" in href:
                    resolved = _abs_url(root, base_url, href)
                    if resolved:
                        # Pre-fetch this direct URL
                        try:
                            r2 = sess.get(resolved,
                                          headers={"Accept": "application/pdf,*/*"},
                                          timeout=40, allow_redirects=True, verify=False)
                            if r2.status_code == 200:
                                return resolved, r2
                        except Exception:
                            pass
                        return resolved, None

        # Fallback: return original URL, let scan_pdf handle it
        return url, None

    except Exception:
        return url, None

def find_decision_doc(sess, base_url, key_val, custom_scores=None): # Add custom_scores here
    """
    Fetch the Documents tab and find the best decision notice.
    Returns (store_url, content_response_or_None).
      store_url          — URL to save in Sheets (direct PDF if possible)
      content_response   — response object if we already have the bytes
                           (avoids double-download in scan_pdf)
    """
    log(f"  📂 Documents tab…", 2)
    from urllib.parse import urlparse
    p    = urlparse(base_url)
    root = f"{p.scheme}://{p.netloc}"

    tab_url = f"{base_url}/applicationDetails.do?activeTab=documents&keyVal={key_val}"
    r = sess.get(tab_url, timeout=25, allow_redirects=True, verify=False)
    if not r or r.status_code != 200:
        log(f"  ❌ Documents tab HTTP {getattr(r,'status_code','?')}", 2)
        return None, None

    soup = BeautifulSoup(r.text, "html.parser")

    # ── Gather candidates from ALL HTML patterns ─────────────
    # Each candidate: {"score": int, "url": str, "label": str}
    candidates = []

    def _add(href, label, score):
        u = _abs_url(root, base_url, href)
        if u:
            candidates.append({"score": score, "url": u, "label": label})

    # Strategy 1: <tr> with <td> cells (classic Idox table layout)
    doc_tables = soup.find_all("table")
    for tbl in doc_tables:
        for row in tbl.find_all("tr"):
            tds = row.find_all("td")
            if len(tds) < 2:
                continue
            # All text in this row
            row_text = " ".join(td.get_text(strip=True) for td in tds)
            score = _score_text(row_text, custom_scores)
            if score == 0:
                continue
            # Find a link in this row
            for td in reversed(tds):
                for a in td.find_all("a", href=True):
                    _add(a["href"], row_text[:50], score)
                    break

    # Strategy 2: <li> items (newer Idox accordion / list layout)
    for li in soup.find_all("li"):
        li_text = li.get_text(separator=" ", strip=True)
        score = _score_text(li_text, custom_scores)
        if score == 0:
            continue
        for a in li.find_all("a", href=True):
            _add(a["href"], li_text[:50], score)

    # Strategy 3: any <a> whose text or nearby heading scores well
    for a in soup.find_all("a", href=True):
        link_text = a.get_text(strip=True)
        parent_text = a.parent.get_text(separator=" ", strip=True) if a.parent else ""
        score = max(_score_text(link_text, custom_scores), _score_text(parent_text, custom_scores))
        if score >= 25:  # only meaningful scores
            _add(a["href"], link_text[:50], score)

    # Strategy 4: direct /files/ PDF links (always include, score by filename)
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if "/files/" in h and ".pdf" in h.lower():
            fname = h.split("/")[-1].lower()
            score = 90 if any(w in fname for w in ["dec", "refus", "notice"]) else 10
            _add(h, f"files/{h.split('/')[-1][:40]}", score)

    # Deduplicate by URL, keep highest score per URL
    seen_urls = {}
    for cand in candidates:
        u = cand["url"]
        if u not in seen_urls or cand["score"] > seen_urls[u]["score"]:
            seen_urls[u] = cand

    ranked = sorted(seen_urls.values(), key=lambda x: -x["score"])

    if not ranked:
        # Strategy 5: last resort — grab ANY PDF link on the page
        # Some councils put all docs in a generic list with no useful labels.
        # We prefer decision-notice-looking filenames, then any PDF.
        _all_pdfs = []
        for a in soup.find_all("a", href=True):
            h = a["href"]
            if ".pdf" in h.lower() or "download" in h.lower() or "getfile" in h.lower():
                _u = _abs_url(root, base_url, h)
                if _u:
                    _fname = h.lower().split("/")[-1].split("?")[0]
                    # Score: prefer decision/refusal named files
                    _sc5 = 50 if any(w in _fname for w in ["dec", "refus", "notice", "officer"]) else 5
                    _all_pdfs.append((_sc5, _u, a.get_text(strip=True)[:40]))
        
        if _all_pdfs:
            _all_pdfs.sort(key=lambda x: -x[0])
            _best_pdf_score, _best_pdf_url, _best_pdf_label = _all_pdfs[0]
            log(f"  ⚠️  No scored docs — using PDF fallback: {_best_pdf_label[:50]}", 2)
            candidates.append({"score": _best_pdf_score, "url": _best_pdf_url, "label": _best_pdf_label})
            ranked = [candidates[-1]]
        else:
            log(f"  ❌ No document links found ({len(soup.find_all('a'))} total anchors on page)", 2)
            return None, None

    log(f"  Found {len(ranked)} candidate doc links", 2)
    for cand in ranked[:3]:
        log(f"    score={cand['score']:3d} | {cand['label'][:55]}", 2)

    # Take best candidate
    best = ranked[0]
    log(f"  → Best: score={best['score']} | {best['url'][-65:]}", 2)

    # Resolve viewDocument.do to direct URL (fixes "Document Unavailable")
    resolved_url, prefetched = _resolve_viewdoc(sess, best["url"], base_url, soup_of_doc_tab=soup)
    if resolved_url != best["url"]:
        log(f"  ✅ Resolved to direct URL: …{resolved_url[-65:]}", 2)

    return resolved_url, prefetched


# ════════════════════════════════════════════════════════════
# PDF SCANNER  —  accepts pre-fetched response to avoid double download
# ════════════════════════════════════════════════════════════
# Words that MUST appear in the PDF for it to count as a refusal.
# An approved application's officer report can contain trigger topic words
# ("sequential test", "nppf") while still recommending approval.
# We require at least one explicit refusal phrase in the document.
_REFUSAL_PHRASES = [
    # Standard English LPA language
    "is refused",
    "be refused",
    "hereby refused",
    "refusal of",
    "reasons for refusal",
    "reason for refusal",
    "refuse planning permission",
    "refused planning permission",
    "application is refused",
    "permission is refused",
    "appeal is dismissed",
    # Officer report / delegated report language
    "recommendation: refuse",
    "recommended for refusal",
    "refusal be granted",
    "officer recommendation: refusal",
    "concluded that planning permission be refused",
    "delegate to refuse",
    "delegated refusal",
    "recommend refusal",
    "recommends refusal",
    # Planning Inspectorate (appeal decisions)
    "the appeal is dismissed",
    "i dismiss this appeal",
    "appeal dismissed",
    "planning permission is not granted",
    # Welsh Planning decisions (TAN 4 / PPW)
    "gwrthodwyd",              # Welsh: "refused"
    "yn cael ei wrthod",       # Welsh: "is refused"
    # Non-standard council wording
    "not be granted",
    "refused on the grounds",
    "refused for the following reasons",
    "the local planning authority refuses",
    "permission be withheld",
    "has been refused",
]

def scan_pdf(sess, pdf_url, prefetched_response=None):
    """
    Download and scan a PDF for:
      1. Retail/Housing planning trigger words (topic relevance)
      2. Explicit refusal language (REQUIRED — prevents approved apps slipping through)

    Returns (trigger_words, is_refused):
      trigger_words  — list of matched PDF_TRIGGERS
      is_refused     — True only if PDF contains explicit refusal language
    Both must be non-empty/True for a lead to qualify.
    """
    log(f"  📥 …{pdf_url[-65:]}", 2)
    try:
        if prefetched_response is not None:
            r = prefetched_response
            log(f"  (using prefetched response)", 2)
        else:
            r = sess.get(
                pdf_url,
                headers={"Accept": "application/pdf,*/*", "Referer": pdf_url},
                timeout=50, allow_redirects=True,
            )

        ct   = r.headers.get("Content-Type", "").lower()
        size = len(r.content)
        log(f"  HTTP {r.status_code} | {size:,}b | {ct[:35]}", 2)

        if r.status_code != 200:
            return [], False

        # Got HTML back = session error / "Document Unavailable"
        if "html" in ct:
            snippet = r.text[:300].replace("\n", " ")
            log(f"  ⚠️  Got HTML (session issue or wrong URL): {snippet[:120]}", 2)
            return [], False

        if size < 800:
            log(f"  ⚠️  Too small to be real PDF ({size}b)", 2)
            return [], False

        # Confirm it's a PDF (magic bytes)
        if not r.content[:4] == b"%PDF":
            if size > 5000:
                log(f"  ⚠️  No PDF magic bytes but large — trying anyway", 2)
            else:
                log(f"  ⚠️  Not a PDF", 2)
                return [], False

        text = ""
        with pdfplumber.open(io.BytesIO(r.content)) as pdf:
            log(f"  {len(pdf.pages)}pp", 2)
            for pg in pdf.pages:
                t = pg.extract_text()
                if t:
                    text += t.lower() + " "

        if not text.strip():
            log(f"  ⚠️  No extractable text — scanned image PDF?", 2)
            return [], False

        log(f"  {len(text):,} chars extracted", 2)

        # ── Check 1: is this actually a refusal? ─────────────────────
        is_refused = any(phrase in text for phrase in _REFUSAL_PHRASES)
        if is_refused:
            log(f"  ✅ Refusal confirmed in PDF text", 2)
        else:
            log(f"  ⚠️  No refusal language found — likely approved/other decision", 2)

        # ── Check 2: Proximity-based Trigger Words ──────────────
        found = []
        # Anchor words that indicate the actual decision context
        anchors = ["refuse","refused","refusal","dismiss","dismissed","unacceptable","harm","conflict","contrary to","reasons for refusal","is refused","be refused","not justified","fails to","would cause","has not been","not been demonstrated","policy","nppf"]
        
        for w in PDF_TRIGGERS:
            if w in text:
                # Find where the trigger word is in the document
                trigger_idx = text.find(w)
                
                # Create a window of ~800 chars (~100 words) around the trigger
                window_start = max(0, trigger_idx - 2000)
                window_end = min(len(text), trigger_idx + len(w) + 2000)
                window_text = text[window_start:window_end]
                
                # Only count the trigger if an anchor word is nearby
                if any(anchor in window_text for anchor in anchors):
                    found.append(w)
                    log(f"  🎯 '{w}' (validated by proximity)", 2)
                else:
                    # For confirmed refusal docs, include high-value triggers even without proximity
                    if is_refused and w in ("sequential","nppf","out of centre","vitality",
                                            "viability","national planning policy framework"):
                        found.append(w)
                        log(f"  🎯 '{w}' (confirmed refusal doc — included)", 2)
                    else:
                        log(f"  ⚠️ '{w}' found without refusal context — ignoring", 2)

        if not found:
            log(f"  ❌ No validated triggers within proximity of refusal language", 2)

        # Final output for this application
        return found, is_refused

    except Exception as e:
        log(f"  ⚠️  Critical error scanning PDF: {e}", 2)
        return [], False
# ════════════════════════════════════════════════════════════
# PROCESS ONE APPLICATION
# ════════════════════════════════════════════════════════════
def process_app(sess, base_url, council, item):
    kv  = item["keyVal"]
    ref = item["ref"]
    log(f"")
    log(f"  ──────────────────────────────────────────────")
    log(f"  📋 {ref}")
    log(f"  {item['desc'][:90]}")


    # Hard exclusion: skip administrative application types before ANY portal hit
    # Mark: DO NOT scrape Discharge of Condition, Prior Approval, Reserved Matters
    _dl = item["desc"].lower()
    # Mark's exclude_words from maplanning.json — skip these entirely
    _ADMIN = (
        "discharge of condition", "discharge of planning condition",
        "reserved matters", "approval of details", "approval of reserved",
        "details reserved by condition", "condition discharge",
        "certificate of lawful",
        "advertisement consent", "listed building consent",
        "hedgerow removal",
        "non-material amendment", "minor material amendment",
        "section 73", "s73",
        "screening opinion", "scoping opinion",
        "environmental impact assessment screening",
        "prior notification", "prior approval",
        "class ma", "part 6", "part 7",
        "notification under", "prior notification under",
        "telecommunications", "street works", "temporary structure",
        "hmo", "house in multiple occupation", "hostel",
        "c4 use", "sui generis hmo",
        "lawful development",
        "tree preservation",
        "single storey extension", "loft conversion", "porch",
        "garage alteration",
    )
    if any(ex in _dl for ex in _ADMIN):
        return None  # administrative — not a planning lead

    # Must have at least one retail/Class E keyword in the description
    if not any(kw.lower() in _dl for kw in RETAIL_KEYWORDS[:25]):
        return None

    det = get_details(sess, base_url, kv)

    # Pre-filter 1: skip clearly non-refused decisions immediately
    decision_raw = det.get("decision", "").lower().strip()
    # Non-refusal decisions — skip immediately without hitting Documents tab
    _NON_REFUSAL = [
        # Standard granted decisions
        "granted", "grant permission", "grant planning permission",
        "approved", "approval", "permitted", "permit",
        "conditional grant", "conditional approval",
        "grant subject to", "granted subject to",
        "permission granted", "planning permission granted",
        "granted conditionally", "granted with conditions",
        # Administrative
        "conditions discharged",
        "prior approval not required",
        "prior approval required and approved",
        "prior approval refused",   # prior approval ≠ s.78 planning refusal
        "not required", "withdrawn", "invalid", "void",
        "non determined",
        "no objection",
        "permission with legal agreement",
        "permission subject to",
        "application permitted",
        "approve",  # some portals use bare 'Approve'
    ]
    
    if decision_raw:
        if any(w in decision_raw for w in _NON_REFUSAL):
            log(f"  ⏭️  Portal says '{det.get('decision','')}' — skip", 2)
            return None

    # If portal says "Refused" explicitly, log it — scan_pdf is still the final gate
    if decision_raw and any(w in decision_raw for w in ("refus", "refuse")):
        log(f"  ✅ Portal confirms refusal: '{det.get('decision','')}'", 2)

    custom_scores = CLIENT_CONFIG.get("doc_scores") if "CLIENT_CONFIG" in globals() else None
    doc_url, prefetched = find_decision_doc(sess, base_url, kv, custom_scores=custom_scores)
    if not doc_url:
        log(f"  ⚠️  No decision doc — skip")
        return None

    triggers, is_refused = scan_pdf(sess, doc_url, prefetched_response=prefetched)

    # Gate 1: must have explicit refusal language in PDF
    if not is_refused:
        # Fallback: check the decision field from the portal itself
        decision_raw = det.get("decision", "").lower()
        portal_refused = any(w in decision_raw for w in ("refus", "refuse", "refused"))
        if not portal_refused:
            log(f"  ❌ Not confirmed as refused (PDF + portal both lack refusal language) — skip")
            return None
        else:
            log(f"  ✅ Refusal confirmed via portal decision field: '{det.get('decision','')}'")

    # Gate 2: must have retail planning trigger words
    if not triggers:
        log(f"  ❌ No retail trigger words — not a retail impact refusal")
        return None

    log(f"  🏆 QUALIFIED — Triggers: {triggers}")
    desc = det.get("proposal", item["desc"])
    sc   = score_lead(desc, triggers, client_type=CLIENT_TYPE)
    log(f"  Score: {sc}/100")

    # ── Minimum score gate ────────────────────────────────────────────────
    # Discard low-quality matches that only matched generic policy phrases.
    # Genuine winnable leads always score 60+ (evidence or sequential trigger
    # alone pushes base 40 + desc signal well above this threshold).
    if sc < MIN_LEAD_SCORE:
        log(f"  ⏭️  Score {sc} < {MIN_LEAD_SCORE} minimum — skipping (not a qualified lead)")
        return None

    # Normalise decision to canonical status using shared helper
    raw_dec        = det.get("decision", "").strip()
    decision_status = _normalise_decision(raw_dec) if raw_dec else "REFUSED"

    lead = {
        "council":   council,
        "ref":       ref,
        "addr":      det.get("address",   item["addr"]),
        "desc":      desc,
        "app_type":  det.get("app_type",  ""),
        "applicant": det.get("applicant", ""),
        "agent":     det.get("agent",     ""),
        "date_rec":  det.get("date_rec",  ""),
        "date_dec":  det.get("date_dec",  ""),
        "decision":  decision_status,
        "triggers":  ", ".join(triggers),
        "score":     sc,
        "keyword":   item["keyword"],
        "url":       f"{base_url}/applicationDetails.do?activeTab=summary&keyVal={kv}",
        "doc_url":   doc_url,
    }
    # Sales intelligence enrichment (safe in thread — no Sheets I/O)
    enrich_lead(lead)
    return lead   # ← CRITICAL: return the lead so _worker can collect it

# ════════════════════════════════════════════════════════════
# SCRAPE ONE COUNCIL
# ════════════════════════════════════════════════════════════
def scrape_council(council, base_url, date_from, date_to):
    log(f"\n{'='*60}")
    log(f"🏛️  {council.upper()}  |  {date_from} → {date_to}")
    log(f"{'='*60}")

    sess      = new_session()
    all_items = []
    qualified = []

    # Warm up the session before any keyword search.
    # This seeds JSESSIONID from the portal root and accepts any disclaimer,
    # preventing the 403-on-POST that occurs with cold sessions.
    log(f"  🔥 Warming session…", 1)
    if not _warmup_portal_session(sess, base_url):
        log(f"  ❌ Session warmup failed — {council} unreachable, skipping")
        return []

    for kw in RETAIL_KEYWORDS:
        try:
            items = search_one_keyword(sess, base_url, kw, date_from, date_to)
            new   = [i for i in items
                     if i["keyVal"] not in {x["keyVal"] for x in all_items}]
            for i in new:
                i["keyword"] = kw
            all_items.extend(new)
            # Longer delay for rate-sensitive councils
            kw_sleep = SLOW_COUNCILS.get(base_url, SLOW_COUNCILS.get(base_url.rstrip('/'), 1.0))
            time.sleep(kw_sleep)
        except Exception as e:
            log(f"  ❌ Keyword '{kw}': {e}")

    log(f"\n  {len(all_items)} unique applications to scan")

    if not all_items:
        return []

    import threading as _thr
    _lock = _thr.Lock()
    _q_results = []
    _MAX_W = 3

    def _worker(it):
        try:
            _ws = new_session()
            _warmup_portal_session(_ws, base_url)
            _lead = process_app(_ws, base_url, council, it)
            if _lead:
                with _lock: _q_results.append(_lead)
        except Exception as _we:
            log(f"  ❌ {it.get('ref','?')}: {_we}")

    _threads = []
    for idx, item in enumerate(all_items):
        log(f"\n  [{idx+1}/{len(all_items)}]")
        while sum(1 for t in _threads if t.is_alive()) >= _MAX_W:
            time.sleep(0.3)
        _t = _thr.Thread(target=_worker, args=(item,), daemon=True)
        _t.start()
        _threads.append(_t)
        time.sleep(0.3)
    for _t in _threads:
        _t.join(timeout=90)

    qualified = sorted(_q_results, key=lambda x: x.get("score",0), reverse=True)

    # ── Write leads SEQUENTIALLY (safe — all threads done) ───────────────────
    written = 0
    for lead in qualified:
        try:
            if write_lead(lead):
                written += 1
        except Exception as _we:
            log(f"  ❌ write_lead failed for {lead.get('ref','?')}: {_we}")

    log(f"\n✅ {council}: {len(qualified)} qualified | {written} written to Sheets")
    return qualified

# ════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════════════════════════
# MARK'S IDEA — COMPETITOR ALERT + AI OBJECTION DRAFT SYSTEM
# ════════════════════════════════════════════════════════════════════════════════
#
# WHAT IT DOES:
#   When a new retail/Class E planning application is submitted (not yet decided),
#   the engine checks whether existing competitors are operating within 1,500m.
#   If yes, it:
#     1. Lists the nearby competitors with distances
#     2. Drafts a formal planning objection using AI
#     3. Writes all this to a "New Applications" tab in Google Sheets
#     4. Sends Mark an email with the draft objection to review
#
# WHY IT MATTERS — SECOND REVENUE STREAM:
#   Current model:  find REFUSED apps → help applicant appeal → fee from applicant
#   New model:      find NEW apps → alert existing competitor → fee from incumbent
#   These don't conflict — different clients, different moment, same expertise.
#   Aldi/Lidl/McDonald's/Costa have no idea when a competitor applies nearby.
#   MAPlanning becomes the early warning system — and charges for the objection.
#
# AI PROVIDER — SUPPORTS BOTH:
#   Mark has OpenAI paid credits → set OPENAI_API_KEY GitHub secret
#   If OPENAI_API_KEY not set, falls back to ANTHROPIC_API_KEY (Claude)
#   If neither set → drafts a template objection without AI (still useful)
#
# REQUIRED GITHUB SECRETS:
#   OPENAI_API_KEY       (or ANTHROPIC_API_KEY) — for AI objection drafting
#   GOOGLE_MAPS_API_KEY  — for competitor proximity search (optional but recommended)
#                          Without it: competitor check is skipped, only app found
#
# NEW GOOGLE SHEET TAB: "New Applications"
#   Columns: Council | Ref | Address | Proposal | Applicant | Date Received |
#             Competitor Count | Competitors (name + distance) | AI Objection Draft |
#             Objection Quality | Status
# ════════════════════════════════════════════════════════════════════════════════

_COMPETITOR_RADIUS_M = 1500   # standard retail impact catchment (NPPF Annex 2)
_COMPETITOR_TYPES = [
    # (display label, Google Places type, description keywords that trigger this search)
    ("Aldi",          "grocery_or_supermarket", ["aldi"]),
    ("Lidl",          "grocery_or_supermarket", ["lidl"]),
    ("Iceland",       "grocery_or_supermarket", ["iceland", "food warehouse"]),
    ("B&M",           "department_store",       ["b&m", "b and m bargains"]),
    ("Home Bargains",  "department_store",      ["home bargains"]),
    ("McDonald's",    "restaurant",             ["mcdonald", "mcdonalds"]),
    ("KFC",           "restaurant",             ["kfc", "kentucky fried"]),
    ("Starbucks",     "cafe",                   ["starbucks"]),
    ("Costa Coffee",  "cafe",                   ["costa coffee", "costa"]),
    ("Greggs",        "bakery",                 ["greggs"]),
    ("Poundland",     "store",                  ["poundland"]),
    ("Savers",        "store",                  ["savers health"]),
    ("Boots",         "pharmacy",               ["boots pharmacy"]),
    ("Tesco Express", "grocery_or_supermarket", ["tesco express", "tesco"]),
    ("Co-op",         "grocery_or_supermarket", ["co-op", "coop", "cooperative"]),
    ("Drive-Through", "restaurant",             ["drive-through", "drive through", "drive thru"]),
]


def geocode_uk_address(address: str) -> tuple:
    """
    Geocode a UK address to (lat, lon).
    
    Strategy:
    1. Extract postcode from address → query postcodes.io (free, no key, very fast)
    2. If no postcode: use Google Geocoding API (requires GOOGLE_MAPS_API_KEY)
    3. If neither works: return (None, None)
    """
    # Strategy 1: postcodes.io (completely free, no API key)
    pc_match = re.search(
        r"\b([A-Z]{1,2}\d{1,2}[A-Z]?\s*\d[A-Z]{2})\b",
        address, re.I
    )
    if pc_match:
        _pc = re.sub(r"\s+", "", pc_match.group(1).upper())
        try:
            _r = requests.get(
                f"https://api.postcodes.io/postcodes/{_pc}",
                timeout=8, headers={"User-Agent": "MAPlanning/1.0"}
            )
            if _r.status_code == 200:
                _d = _r.json().get("result", {})
                if _d.get("latitude"):
                    return float(_d["latitude"]), float(_d["longitude"])
        except Exception:
            pass

    # Strategy 2: Google Geocoding API
    _gmk = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if _gmk:
        try:
            from urllib.parse import quote as _q
            _r2 = requests.get(
                f"https://maps.googleapis.com/maps/api/geocode/json"
                f"?address={_q(address + ', UK')}&key={_gmk}",
                timeout=10
            )
            if _r2.status_code == 200:
                _res = _r2.json().get("results", [])
                if _res:
                    _loc = _res[0]["geometry"]["location"]
                    return float(_loc["lat"]), float(_loc["lng"])
        except Exception:
            pass

    return None, None


def find_nearby_competitors(lat: float, lon: float, desc: str) -> list:
    """
    Search Google Places for existing retail competitors near an application.
    Returns list of dicts: {name, address, type, distance_m, maps_url}
    
    Requires GOOGLE_MAPS_API_KEY. Without it, returns [] with a log message.
    """
    _gmk = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if not _gmk:
        log("  ℹ️  No GOOGLE_MAPS_API_KEY — competitor check requires Google Maps API", 2)
        log("     Add secret GOOGLE_MAPS_API_KEY for automatic competitor detection", 2)
        return []

    desc_lower = desc.lower()
    seen_ids   = set()
    competitors = []

    # Decide which place types to search based on what was proposed
    types_to_search = set()
    for _label, _ptype, _terms in _COMPETITOR_TYPES:
        if any(t in desc_lower for t in _terms):
            types_to_search.add((_label, _ptype))

    # Always include generic retail types for food/retail applications
    if any(w in desc_lower for w in ["food", "supermarket", "convenience", "grocery", "retail"]):
        types_to_search.add(("Supermarket", "grocery_or_supermarket"))
    if any(w in desc_lower for w in ["restaurant", "café", "cafe", "coffee", "takeaway", "hot food"]):
        types_to_search.add(("Restaurant/Cafe", "restaurant"))
        types_to_search.add(("Cafe", "cafe"))

    if not types_to_search:
        types_to_search = {("Retail", "store"), ("Supermarket", "grocery_or_supermarket")}

    import math as _math
    for _label, _ptype in types_to_search:
        try:
            _r = requests.get(
                "https://maps.googleapis.com/maps/api/place/nearbysearch/json",
                params={
                    "location": f"{lat},{lon}",
                    "radius":   str(_COMPETITOR_RADIUS_M),
                    "type":     _ptype,
                    "key":      _gmk,
                    "language": "en",
                },
                timeout=12
            )
            if _r.status_code != 200:
                continue
            for _pl in _r.json().get("results", []):
                _pid = _pl.get("place_id", "")
                if not _pid or _pid in seen_ids:
                    continue
                seen_ids.add(_pid)
                _loc  = _pl.get("geometry", {}).get("location", {})
                _plat, _plon = float(_loc.get("lat", lat)), float(_loc.get("lng", lon))
                # Haversine distance
                _dlat = _math.radians(_plat - lat)
                _dlon = _math.radians(_plon - lon)
                _a    = (_math.sin(_dlat/2)**2 +
                         _math.cos(_math.radians(lat)) *
                         _math.cos(_math.radians(_plat)) *
                         _math.sin(_dlon/2)**2)
                _dist = int(6371000 * 2 * _math.asin(_math.sqrt(min(1.0,_a))))
                competitors.append({
                    "name":       _pl.get("name", ""),
                    "address":    _pl.get("vicinity", ""),
                    "type":       _label,
                    "distance_m": _dist,
                    "maps_url":   f"https://www.google.com/maps/place/?q=place_id:{_pid}",
                })
            time.sleep(0.25)
        except Exception as _pe:
            log(f"  ⚠️  Places ({_label}): {_pe}", 2)

    # Sort by distance, deduplicate by name
    seen_names = set()
    unique = []
    for c in sorted(competitors, key=lambda x: x["distance_m"]):
        _k = c["name"].lower()
        if _k not in seen_names:
            seen_names.add(_k)
            unique.append(c)

    return unique[:10]


def draft_ai_objection(lead: dict, competitors: list) -> str:
    """
    Draft a formal planning objection using AI.
    
    SUPPORTED AI PROVIDERS (checked in order):
    1. OpenAI     → set OPENAI_API_KEY   (Mark has paid credits here)
    2. Anthropic  → set ANTHROPIC_API_KEY (Claude, alternative)
    3. No key set → returns a structured template with placeholders
    
    The objection covers:
    - NPPF 2024 Chapter 7, paras 88-91 (town centre sequential test)
    - Retail Impact Assessment requirement
    - Lack of evidence failure
    - Specific competitor context
    """
    _comp_lines = "\n".join(
        f"  • {c['name']} ({c['type']}) — {c['distance_m']}m away at {c['address']}"
        for c in competitors[:6]
    ) if competitors else "  (No competitors identified in proximity search)"

    _prompt = f"""You are a specialist UK retail planning consultant at MAPlanning (Mark Alexander Planning).
Draft a formal planning objection letter (550-750 words) on behalf of an existing nearby retailer
who is potentially impacted by this new planning application.

PLANNING APPLICATION DETAILS:
  Council:      {lead.get('council', '')}
  Reference:    {lead.get('ref', '')}
  Address:      {lead.get('addr', '')}
  Proposal:     {lead.get('desc', '')}
  Application type: {lead.get('app_type', 'Full planning application')}
  Applicant:    {lead.get('applicant', 'Not stated')}

EXISTING NEARBY OPERATORS (potential clients for MAPlanning's objection service):
{_comp_lines}

The objection MUST cover all four of these grounds:

1. SEQUENTIAL TEST (NPPF paras 88-90, 2024):
   The applicant has not demonstrated adequate sequential search. The sequential approach
   requires proposals for main town centre uses in out-of-centre locations to first consider
   sequentially preferable in-centre and edge-of-centre sites. Without a robust Sequential
   Test Assessment, permission should be refused.

2. RETAIL IMPACT ASSESSMENT (NPPF para 91, 2024):
   For retail proposals exceeding the locally-defined impact threshold, a Retail Impact
   Assessment is required. The application does not appear to be accompanied by a sufficient
   assessment of impact on the vitality and viability of existing centres.

3. LACK OF EVIDENCE:
   The application is deficient in evidence: no quantitative or qualitative need has been
   demonstrated; no sequential search submitted; no impact assessment provided. These are
   specific, curable reasons for refusal under the NPPF.

4. HARM TO VITALITY AND VIABILITY:
   The proposal would divert trade from established town centre and local retailers, harming
   vitality and viability in conflict with NPPF Chapter 7 objectives.

FORMAT: Start "Dear Sir/Madam," and end "Yours faithfully,\\n[Client Name]\\nOn behalf of [existing operator]"
Cite specific NPPF 2024 paragraph numbers. Use formal planning consultancy language.
Return ONLY the letter. No preamble or explanation."""

    # ── Try OpenAI first (Mark has paid credits) ──────────────────────────────
    _openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if _openai_key:
        try:
            _r = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {_openai_key}",
                    "Content-Type":  "application/json",
                },
                json={
                    "model":       "gpt-4o",       # full model — legal document qualityity
                    "max_tokens":  1200,
                    "temperature": 0.3,            # low temp = consistent, formal tone
                    "messages":    [{"role": "user", "content": _prompt}],
                },
                timeout=40
            )
            if _r.status_code == 200:
                _text = _r.json()["choices"][0]["message"]["content"].strip()
                log(f"  ✅ OpenAI objection draft ({len(_text)} chars, model: gpt-4o)", 2)
                return _text
            else:
                log(f"  ⚠️  OpenAI API {_r.status_code} — trying Anthropic fallback", 2)
        except Exception as _oae:
            log(f"  ⚠️  OpenAI error: {_oae} — trying Anthropic fallback", 2)

    # ── Try Anthropic Claude (fallback) ───────────────────────────────────────
    _anth_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if _anth_key:
        try:
            _r2 = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":         _anth_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type":      "application/json",
                },
                json={
                    "model":      "claude-sonnet-4-6",  # better legal writing quality
                    "max_tokens": 1200,
                    "messages":   [{"role": "user", "content": _prompt}],
                },
                timeout=40
            )
            if _r2.status_code == 200:
                _text2 = _r2.json()["content"][0]["text"].strip()
                log(f"  ✅ Claude objection draft ({len(_text2)} chars)", 2)
                return _text2
            else:
                log(f"  ⚠️  Anthropic API {_r2.status_code}", 2)
        except Exception as _ae:
            log(f"  ⚠️  Anthropic error: {_ae}", 2)

    # ── No AI key: return structured template ─────────────────────────────────
    log("  ℹ️  No OPENAI_API_KEY or ANTHROPIC_API_KEY set — using template objection", 2)
    log("     Add OPENAI_API_KEY secret to GitHub for AI-generated objections", 2)
    _comp_summary = (
        ", ".join(f"{c['name']} ({c['distance_m']}m)" for c in competitors[:3])
        if competitors else "existing operators in the area"
    )
    return f"""Dear Sir/Madam,

RE: Planning Application {lead.get('ref', '[REFERENCE]')} — {lead.get('addr', '[ADDRESS]')}
    {lead.get('desc', '[PROPOSAL]')}

I write on behalf of {_comp_summary} to object to the above application on the following grounds:

1. FAILURE OF SEQUENTIAL TEST (NPPF 2024, paragraphs 88-90)
The application does not demonstrate that a sequential assessment has been undertaken. 
NPPF paragraph 89 requires applicants for main town centre uses in out-of-centre locations 
to demonstrate that there are no sequentially preferable sites available. In the absence 
of a Sequential Test Assessment, the application should be refused.

2. ABSENCE OF RETAIL IMPACT ASSESSMENT (NPPF 2024, paragraph 91)
No Retail Impact Assessment accompanies this application. Where proposals for retail uses 
exceed the locally set threshold, a Retail Impact Assessment is required to demonstrate 
no unacceptable impact on the vitality and viability of existing centres.

3. HARM TO VITALITY AND VIABILITY
The proposed development would divert expenditure from established retailers and centres 
in conflict with the NPPF's objective (Chapter 7) of supporting the vitality and viability 
of existing town centres.

4. LACK OF EVIDENCE
The application lacks sufficient evidence to demonstrate compliance with the NPPF sequential 
approach and impact requirements. Failure to demonstrate compliance constitutes grounds for refusal.

We respectfully request that the Local Planning Authority refuse this application.

Yours faithfully,
[Client Name]
On behalf of [Existing Operator]
Prepared by MAPlanning — Retail Planning Consultants"""


def _get_or_create_new_apps_tab(spreadsheet):
    """Get or create the 'New Applications' tab for the competitor alert system."""
    _TAB = "New Applications"
    try:
        return spreadsheet.worksheet(_TAB)
    except Exception:
        ws = spreadsheet.add_worksheet(_TAB, rows=2000, cols=11)
        ws.update(values=[[
            "Council", "Reference", "Address", "Proposal", "Applicant",
            "Date Received", "Competitor Count", "Competitors (name + distance)",
            "AI Objection Draft (preview)", "Objection Quality", "Status",
        ]], range_name="A1")
        try:
            ws.spreadsheet.batch_update({"requests": [{
                "repeatCell": {
                    "range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1},
                    "cell": {"userEnteredFormat": {
                        "backgroundColor": {"red": 0.067, "green": 0.302, "blue": 0.455},
                        "textFormat": {
                            "foregroundColor": {"red":1,"green":1,"blue":1},
                            "bold": True,
                        },
                    }},
                    "fields": "userEnteredFormat(backgroundColor,textFormat)",
                }
            }]})
        except Exception:
            pass
        return ws


def write_new_application(lead: dict, ws_tab) -> bool:
    """Write a new (pending) application + competitor data to the New Applications tab."""
    _comp_str = " | ".join(
        f"{c['name']} ({c['distance_m']}m)"
        for c in lead.get("competitors", [])[:5]
    ) or "None found"

    _objection = lead.get("ai_objection", "")
    _preview   = (_objection[:400] + "…") if len(_objection) > 400 else _objection

    # Quality rating based on competitor count and objection length
    _comp_count = len(lead.get("competitors", []))
    if _comp_count >= 3 and len(_objection) > 500:
        _quality = "HIGH — multiple competitors + AI draft ready"
    elif _comp_count >= 1 and _objection:
        _quality = "MEDIUM — competitor found, review draft"
    elif _comp_count >= 1:
        _quality = "LOW — competitor found, no AI draft (add API key)"
    else:
        _quality = "INFO — application found, no competitors detected"

    try:
        sheets_retry(lambda: ws_tab.append_row([
            lead.get("council",""),
            lead.get("ref",""),
            lead.get("addr","")[:200],
            lead.get("desc","")[:300],
            lead.get("applicant",""),
            lead.get("date_rec",""),
            str(_comp_count),
            _comp_str,
            _preview,
            _quality,
            "REVIEW",
        ], value_input_option="USER_ENTERED"))
        return True
    except Exception as _e:
        log(f"  ❌ New app write failed: {_e}", 2)
        return False


def process_new_application(sess, base_url, council, item) -> dict | None:
    """
    Process a planning application still in progress (not yet decided).
    
    Checks for nearby competitors and drafts an AI objection if found.
    This is the engine for Mark's competitor alert idea.
    
    Returns lead dict (with competitors + ai_objection fields) or None if not relevant.
    """
    kv  = item["keyVal"]
    ref = item["ref"]

    # Must be a relevant retail/Class E use
    desc_lower = item["desc"].lower()
    if not any(kw.lower() in desc_lower for kw in RETAIL_KEYWORDS[:15]):
        return None

    # Skip excluded types
    if any(ex.lower() in desc_lower for ex in EXCLUDE_WORDS):
        return None

    det = get_details(sess, base_url, kv)

    # Only undecided applications
    decision_raw = (det.get("decision","") or "").lower().strip()
    _decided = ["granted","approved","refused","dismissed","withdrawn","invalid",
                "not required","permitted development"]
    if decision_raw and any(w in decision_raw for w in _decided):
        return None

    addr = det.get("address", item["addr"])
    desc = det.get("proposal",  item["desc"])

    log(f"  🔍 New app {ref}: geocoding + competitor check", 2)
    lat, lon = geocode_uk_address(addr)
    if lat:
        log(f"  📍 {lat:.4f}, {lon:.4f}", 2)
    else:
        log(f"  ⚠️  Could not geocode '{addr[:50]}' — competitor search skipped", 2)

    competitors = find_nearby_competitors(lat, lon, desc) if lat else []
    if competitors:
        log(f"  🎯 {len(competitors)} competitors within {_COMPETITOR_RADIUS_M}m", 2)
        for c in competitors[:3]:
            log(f"     • {c['name']}: {c['distance_m']}m", 2)

    _lead = {
        "council":      council,
        "ref":          ref,
        "addr":         addr,
        "desc":         desc,
        "app_type":     det.get("app_type",""),
        "applicant":    det.get("applicant",""),
        "date_rec":     det.get("date_rec",""),
        "url":          f"{base_url}/applicationDetails.do?activeTab=summary&keyVal={kv}",
        "competitors":  competitors,
        "ai_objection": "",
    }

    # Only draft objection when competitors are actually found nearby
    if competitors:
        _lead["ai_objection"] = draft_ai_objection(_lead, competitors)

    return _lead

def run():
    run_start = datetime.now()
    today     = datetime.now()
    date_to   = today.strftime("%d/%m/%Y")
    date_from = (today - timedelta(weeks=WEEKS_TO_SCRAPE)).strftime("%d/%m/%Y")

    print("=" * 60)
    print(f"🏗️  MAPlanning Retail Lead Engine v26")
    print(f"📅  {today.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"📆  {date_from} → {date_to}  ({WEEKS_TO_SCRAPE} weeks)")
    print(f"🏛️  {len(COUNCILS)} councils configured")
    print(f"🔎  Mode: {RUN_MODE} | Keywords: {len(RETAIL_KEYWORDS)}")
    print(f"🤖  AI: {'OpenAI ✅' if os.environ.get('OPENAI_API_KEY') else 'Anthropic ✅' if os.environ.get('ANTHROPIC_API_KEY') else '⚠️ No AI key (add OPENAI_API_KEY)'}")
    print(f"🗺️  Maps: {'Google Places ✅' if os.environ.get('GOOGLE_MAPS_API_KEY') else '⚠️ No GOOGLE_MAPS_API_KEY (competitor check disabled)'}")
    print("=" * 60)

    # ── Step 1: connect to Sheets & load existing refs ──────
    if not get_sheet():
        print("❌ Sheets connection failed — stopping"); return
    load_existing_refs()

    # ── Step 2: pre-flight — fast parallel check ───────────
    # ── Step 2: pre-flight — fast parallel check ───────────
    live_councils, _dead = preflight_check(COUNCILS)
    if not live_councils:
        print("❌ No reachable councils — check network"); return

    # ── Batch slicing (set SCRAPE_BATCH env var e.g. "2/4") ─  # NEW
    _batch = os.environ.get("SCRAPE_BATCH", "1/1")              # NEW
    _bnum, _btotal = int(_batch.split("/")[0]), int(_batch.split("/")[1])  # NEW
    _items = list(live_councils.items())                         # NEW
    _size  = -(-len(_items) // _btotal)                         # NEW
    _slice = _items[(_bnum - 1) * _size : _bnum * _size]        # NEW
    live_councils = dict(_slice)                                 # NEW
    log(f"Batch {_bnum}/{_btotal}: {len(live_councils)} councils")  # NEW

    # ── Step 3: scrape every live council ───────────────────
    import random
    grand   = []
    summary = {}
    failed  = []
    total   = len(live_councils)

    for idx, (name, url) in enumerate(live_councils.items()):
        if not time_ok(need_s=90):
            log(f"\n⏰ Runtime budget low — stopping after {idx}/{total} councils")
            log(f"   ({total-idx} councils skipped: {', '.join(list(live_councils.keys())[idx:idx+5])}…)")
            break
        log(f"\n{'━'*60}")
        log(f"Council {idx+1}/{total}: {name}")
        log(f"{'━'*60}")
        try:
            leads = scrape_council(name, url, date_from, date_to)
            summary[name] = len(leads)
            grand.extend(leads)
        except Exception as e:
            log(f"❌ {name}: {str(e)[:80]}")
            failed.append(name)
            summary[name] = 0

        if idx < total - 1:
            pause = random.uniform(2, 4)
            log(f"⏸️  {pause:.1f}s | {total-idx-1} remaining | {len(grand)} leads so far")
            time.sleep(pause)

    grand.sort(key=lambda x: x["score"], reverse=True)

    # ── Run summary ──────────────────────────────────────────────────────
    _enf  = [l for l in grand if l.get("is_enforcement") == "YES"]
    _high = [l for l in grand if l.get("score",0) >= 80]
    log(f"\n{'='*60}")
    log(f"📊 RUN SUMMARY: {len(grand)} qualified leads | {len(_high)} HIGH | {len(_enf)} enforcement")
    if _enf:
        log("  ⚡ ENFORCEMENT (28-day window):")
        for l in _enf[:3]: log(f"    • {l['council']} | {l['ref']} | {l['addr'][:40]}")
    if _high:
        log("  🔴 HIGH PRIORITY:")
        for l in _high[:5]: log(f"    [{l['score']}] {l['council']} | {l.get('recommended_action','')[:70]}")
    log(f"{'='*60}")

    # ── NEW APPLICATIONS MODE — Mark's competitor alert system ────────────────
    _new_app_count = 0
    if RUN_MODE in ("applications", "both"):
        log(f"\n{'━'*60}")
        log("🔍 COMPETITOR ALERT MODE — scanning new/pending applications")
        log(f"{'━'*60}")

        # Get or create the New Applications sheet tab
        _ws_new = None
        try:
            _sh = get_sheet()
            if _sh:
                _ws_new = _get_or_create_new_apps_tab(_sh.spreadsheet)
        except Exception as _te:
            log(f"  ⚠️  New Applications tab: {_te}")

        # Scan each live council for new applications
        for _nc_name, _nc_url in live_councils.items():
            if not time_ok(need_s=45):
                log("⏰ Time budget low — stopping competitor scan"); break
            try:
                _nsess = new_session()
                _warmup_portal_session(_nsess, _nc_url)
                for _kw in RETAIL_KEYWORDS[:10]:  # top 10 keywords
                    try:
                        _items = search_one_keyword(
                            _nsess, _nc_url, _kw, date_from, date_to)
                        for _item in _items[:3]:  # max 3 per keyword per council
                            if _item["ref"] in _existing_refs:
                                continue
                            _nl = process_new_application(
                                _nsess, _nc_url, _nc_name, _item)
                            if _nl:
                                if _ws_new:
                                    write_new_application(_nl, _ws_new)
                                _new_app_count += 1
                                if _nl.get("competitors"):
                                    log(f"  🎯 {_item['ref']}: "
                                        f"{len(_nl['competitors'])} competitors nearby")
                        time.sleep(0.8)
                    except Exception:
                        continue
            except Exception as _nce:
                log(f"  ⚠️  {_nc_name} (new apps): {str(_nce)[:60]}")

        if _new_app_count > 0:
            log(f"\n  📊 {_new_app_count} new applications processed → 'New Applications' tab")
        else:
            log(f"  📊 No relevant new applications found this scan")

    run_duration_min = (datetime.now() - run_start).total_seconds() / 60

    # ── Step 4: count leads added in the past 7 days from the sheet ────────
    # This runs AFTER scraping so newly-written leads are included in the count.
    weekly_count, weekly_leads = get_weekly_lead_count()

    # ── Final report ─────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"📊 FINAL RESULTS")
    print(f"{'='*60}")

    print(f"\n  Run duration: {run_duration_min:.1f} minutes")
    print(f"  Councils attempted:  {total}")
    print(f"  New leads this run:  {len(grand)}")
    print(f"  Leads (past 7 days, including previous runs): {weekly_count}")

    print(f"\n  Council breakdown:")
    for c, n in summary.items():
        mark = "❌ FAILED" if c in failed else f"🏆 {n} leads" if n else "  0"
        print(f"    {c:25s}: {mark}")

    if failed:
        print(f"\n  Failed during scrape ({len(failed)}):")
        for fc in failed:
            print(f"    {fc}")

    print(f"\n  {'─'*36}")
    print(f"  {'NEW LEADS THIS RUN':25s}: {len(grand)}")
    print(f"  {'LEADS PAST 7 DAYS':25s}: {weekly_count}")
    print(f"{'='*60}")

    if grand:
        print(f"\n🏆 TOP NEW LEADS:")
        for lead in grand[:10]:
            print(f"\n  [{lead['score']}pts] {lead['council']} | {lead['ref']}")
            print(f"  {lead['addr']}")
            print(f"  {lead['desc'][:100]}")
            print(f"  Triggers: {lead['triggers']}")
            print(f"  {lead['url']}")

    # ── Email digest — sent AFTER scraper fully completes ───────────────────
    # Conditions before sending:
    #   1. Must be in automated mode (GMAIL_APP_PASSWORD set)
    #   2. Must have scraped at least some councils (run_duration > 1 min
    #      guards against spurious fast crashes triggering email)
    #   3. Send regardless of 0 new leads — weekly_count from sheet still
    #      makes the email useful (shows what was already found)
    if os.environ.get("GMAIL_APP_PASSWORD"):
        councils_with_results = sum(1 for n in summary.values() if n >= 0)
        if run_duration_min < 1.0 and len(grand) == 0:
            log("⚠️  Run completed in < 1 min with 0 leads — suppressing email.")
        else:
            log("\n📧 Sending email digest (run complete)…")
            
            # This looks up the specific client's email from GitHub Secrets
            client_email = os.environ.get(CLIENT_EMAIL_VAR, "")
            if not client_email:
                log(f"⚠️ Warning: GitHub Secret {CLIENT_EMAIL_VAR} not found. Email may not send.")
                
            os.environ["GMAIL_TO"] = client_email 
            
            email_digest.send_digest(
                grand, summary, failed,
                date_from, date_to,
                weekly_count=weekly_count,
                weekly_leads=weekly_leads,
                run_duration_min=run_duration_min,
                log_fn=log,
            )
    else:
        log("ℹ️  Email skipped (Colab mode — set GMAIL_APP_PASSWORD)")
        
# ── Authenticate Google ──────────────────────────────────────
# In GitHub Actions: GCP_SERVICE_ACCOUNT_JSON env var is set — no action needed here.
# In Colab: trigger interactive auth so default() works.
if not os.environ.get("GCP_SERVICE_ACCOUNT_JSON"):
    try:
        from google.colab import auth
        auth.authenticate_user()
        print("✅ Google Colab auth done")
    except Exception:
        pass  # already authenticated or running locally

run()
