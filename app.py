import streamlit as st
import pandas as pd
import requests
import pgeocode
import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor

st.set_page_config(page_title="Calllist US", layout="wide")

# Initialize US geocoder
geocoder = pgeocode.Nominatim('us')

# -------------------------------------------------------------
# 1. Email Extraction & Filtering Logic
# -------------------------------------------------------------
EMAIL_REGEX = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')
IGNORED_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.pdf', '.css', '.js')
IGNORED_DOMAINS = ('sentry.io', 'wixpress.com', 'example.com', 'domain.com', 'schema.org')


def extract_clean_emails(html_text: str) -> set:
    found = set()
    for email in EMAIL_REGEX.findall(html_text):
        e = email.lower().strip()
        if any(e.endswith(ext) for ext in IGNORED_EXTENSIONS):
            continue
        domain = e.split('@')[-1]
        if any(d in domain for d in IGNORED_DOMAINS):
            continue
        found.add(e)
    return found


def scrape_email_from_website(website_url: str) -> str:
    """Inspects homepage, then falls back to /contact or /about pages."""
    if not website_url or website_url == "N/A" or not website_url.startswith("http"):
        return "N/A"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }
    found_emails = set()

    try:
        # Check homepage
        resp = requests.get(website_url, headers=headers, timeout=4)
        if resp.status_code == 200:
            found_emails.update(extract_clean_emails(resp.text))

            # If no email on home, check for a contact page link
            if not found_emails:
                soup = BeautifulSoup(resp.text, 'html.parser')
                contact_url = None
                for a in soup.find_all('a', href=True):
                    href = a['href'].lower()
                    if any(term in href for term in ['contact', 'about', 'reach']):
                        contact_url = urljoin(website_url, a['href'])
                        break

                if contact_url:
                    contact_resp = requests.get(contact_url, headers=headers, timeout=4)
                    if contact_resp.status_code == 200:
                        found_emails.update(extract_clean_emails(contact_resp.text))
    except Exception:
        pass  # Skip broken, slow, or blocked domains

    return ", ".join(list(found_emails)[:2]) if found_emails else "Not Found"


# -------------------------------------------------------------
# 2. Google Places API (New) Query Function
# -------------------------------------------------------------
def fetch_us_places(api_key: str, query: str, lat: float, lon: float, radius_miles: float):
    url = "https://places.googleapis.com/v1/places:searchText"

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": (
            "places.displayName,"
            "places.nationalPhoneNumber,"
            "places.formattedAddress,"
            "places.websiteUri,"
            "places.googleMapsUri"
        )
    }

    # 1 mile = 1,609.34 meters
    radius_meters = float(radius_miles * 1609.34)

    payload = {
        "textQuery": query,
        "regionCode": "US",
        "locationBias": {
            "circle": {
                "center": {"latitude": lat, "longitude": lon},
                "radius": radius_meters
            }
        }
    }

    response = requests.post(url, headers=headers, json=payload, timeout=10)

    if response.status_code != 200:
        st.error(f"Google Places Error: {response.text}")
        return []

    places = response.json().get("places", [])
    records = []
    for p in places:
        records.append({
            "Business Name": p.get("displayName", {}).get("text", "N/A"),
            "Phone": p.get("nationalPhoneNumber", "N/A"),
            "Address": p.get("formattedAddress", "N/A"),
            "Website": p.get("websiteUri", "N/A"),
            "Maps URL": p.get("googleMapsUri", "N/A")
        })
    return records


# -------------------------------------------------------------
# 3. Streamlit UI & Parallel Execution
# -------------------------------------------------------------
st.title("📞 Calllist US — Business & Email Generator")
st.caption("Find business names, phones, and public emails across any US ZIP code.")

with st.sidebar:
    st.header("Settings")
    api_key = st.text_input(
        "Google Maps API Key",
        value=st.secrets.get("GOOGLE_MAPS_API_KEY", ""),
        type="password"
    )
    scrape_emails_toggle = st.checkbox("Automatically scrape websites for emails", value=True)

col1, col2, col3 = st.columns([2, 2, 1])

with col1:
    profession = st.text_input("Profession / Niche", placeholder="e.g. Roofing, Chiropractor, Plumber")

with col2:
    zip_input = st.text_input("5-Digit US ZIP Code", placeholder="e.g. 90210, 02108", max_chars=5)

with col3:
    radius_miles = st.slider("Radius (Miles)", min_value=1, max_value=50, value=15)

if st.button("Generate Calllist", type="primary"):
    if not api_key:
        st.warning("Please provide your Google Maps API Key in the sidebar.")
    elif not profession:
        st.warning("Please enter a profession.")
    elif not zip_input or len(zip_input.strip()) != 5:
        st.warning("Please enter a valid 5-digit US ZIP code.")
    else:
        clean_zip = zip_input.strip().zfill(5)
        geo_result = geocoder.query_postal_code(clean_zip)

        if pd.isna(geo_result.latitude) or pd.isna(geo_result.longitude):
            st.error(f"Could not find coordinates for ZIP code: {clean_zip}.")
        else:
            lat = float(geo_result.latitude)
            lon = float(geo_result.longitude)
            place_name = f"{geo_result.place_name}, {geo_result.state_code}"

            st.info(f"Targeting: **{place_name} ({clean_zip})**")

            with st.spinner("Fetching businesses from Google Places..."):
                leads = fetch_us_places(api_key, profession, lat, lon, radius_miles)

            if leads:
                if scrape_emails_toggle:
                    with st.spinner(f"Scanning {len(leads)} websites for public emails..."):
                        websites = [l["Website"] for l in leads]
                        with ThreadPoolExecutor(max_workers=10) as executor:
                            emails = list(executor.map(scrape_email_from_website, websites))

                        for i, email in enumerate(emails):
                            leads[i]["Email"] = email

                df_leads = pd.DataFrame(leads)

                # Reorder columns to place Email right next to Phone
                preferred_order = ["Business Name", "Phone", "Email", "Address", "Website", "Maps URL"]
                final_cols = [c for c in preferred_order if c in df_leads.columns]
                df_leads = df_leads[final_cols]

                st.success(f"Retrieved {len(df_leads)} businesses!")
                st.dataframe(df_leads, use_container_width=True)

                csv = df_leads.to_csv(index=False).encode("utf-8")
                st.download_button(
                    label="📥 Download CSV for Dialing & Outreach",
                    data=csv,
                    file_name=f"calllist_{profession.lower().replace(' ', '_')}_{clean_zip}.csv",
                    mime="text/csv"
                )
            else:
                st.warning("No businesses found matching this query in the selected area.")