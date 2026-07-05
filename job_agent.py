import os
import json
import base64
import time
import html  # Added to safely format job descriptions for Telegram
from datetime import datetime
import requests
from jobspy import scrape_jobs
from google.cloud import firestore
from google.oauth2 import service_account

# --- Configuration & Secrets ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
FIREBASE_CREDS_JSON = os.environ.get("FIREBASE_SERVICE_ACCOUNT")

# --- OpenRouter Waterfall Setup ---
API_KEYS = [
    os.environ.get("OPENROUTER_API_KEY_1"),
    os.environ.get("OPENROUTER_API_KEY_2"),
    os.environ.get("OPENROUTER_API_KEY_3")
]

# Filter out empty keys
VALID_KEYS = [key for key in API_KEYS if key and key.strip()]

def get_firestore_client():
    """Initializes Firestore, handling both Base64 and raw JSON formats."""
    try:
        raw_creds = FIREBASE_CREDS_JSON
        if not raw_creds:
            print("Error: FIREBASE_SERVICE_ACCOUNT environment variable is missing.")
            return None

        try:
            decoded_str = base64.b64decode(raw_creds).decode('utf-8')
            creds_dict = json.loads(decoded_str)
        except Exception:
            creds_dict = json.loads(raw_creds)
            
        credentials = service_account.Credentials.from_service_account_info(creds_dict)
        return firestore.Client(credentials=credentials)
    except Exception as e:
        print(f"Error initializing Firestore: {e}")
        return None

def fetch_active_users(db):
    """Fetches all registered and active users from Firestore."""
    users = []
    try:
        docs = db.collection("users").where("active", "==", True).stream()
        for doc in docs:
            users.append(doc.to_dict())
    except Exception as e:
        print(f"Error fetching users: {e}")
    return users

def parse_and_filter_job(job_description, title, company, default_url, user_search_terms):
    """Uses OpenRouter to evaluate jobs with fallback logic."""
    if not VALID_KEYS:
        return {"is_match": False, "ai_failed": True, "reason": "System Error: No OpenRouter API keys configured.", "email": "-", "phone": "-", "link": default_url}
        
    terms_string = ", ".join(user_search_terms)
    desc_text = job_description if job_description and len(str(job_description).strip()) > 20 else "DESCRIPTION_MISSING"
    
    prompt = f"""
    You are an expert recruitment automation assistant. 
    Review this job for a candidate looking for roles related to: {terms_string}.
    They are looking for entry-level roles, junior positions, or short-term internships.
    
    Job Title: {title}
    Company: {company}
    Job Description: {desc_text[:3000] if desc_text != 'DESCRIPTION_MISSING' else desc_text}
    
    Instructions:
    1. If the Job Description is 'DESCRIPTION_MISSING', evaluate the match based ONLY on the Job Title and Company.
    2. Determine if this is a strong match based on the candidate's specific keywords ({terms_string}) (true/false).
    3. Provide a brief, 1-2 sentence 'reason' explaining exactly WHY it is or isn't a match.
    4. Extract any specific HR email addresses mentioned.
    5. Extract any contact phone numbers mentioned.
    6. Extract any external application links mentioned (if none, use '{default_url}').
    
    Provide your response STRICTLY as a valid JSON object matching this exact structure. Do not include markdown formatting like ```json.
    {{
        "is_match": true,
        "reason": "Brief explanation here...",
        "email": "extracted_email_or_-",
        "phone": "extracted_phone_or_-",
        "link": "extracted_link_or_default_url"
    }}
    """
    
    last_error = ""
    model_to_use = "openrouter/free"

    for idx, key in enumerate(VALID_KEYS):
        try:
            response = requests.post(
                url="https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {key}",
                    "HTTP-Referer": "https://github.com/Sapkalshubham0/job_agent",
                    "X-Title": "AI Job Hunter SaaS",
                    "Content-Type": "application/json"
                },
                json={
                    "model": model_to_use,
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"}
                },
                timeout=20  
            )
            
            response.raise_for_status() 
            
            # Safely extract content to handle API returning None/null during rate limits
            response_json = response.json()
            raw_content = response_json.get('choices', [{}])[0].get('message', {}).get('content')
            
            if raw_content is None:
                raise ValueError("API returned null content (likely rate-limited).")
                
            result_text = raw_content.strip()
            
            if result_text.startswith("```json"):
                result_text = result_text[7:-3].strip()
            elif result_text.startswith("```"):
                result_text = result_text[3:-3].strip()
                
            parsed_json = json.loads(result_text)
            parsed_json["ai_failed"] = False 
            return parsed_json
        
        except requests.exceptions.RequestException as e:
            if hasattr(e, 'response') and e.response is not None:
                last_error = f"{e.response.status_code} - {e.response.text}"
            else:
                last_error = str(e)
            print(f"Key {idx + 1} failed: {last_error[:200]}... Cascading to next key...")
            time.sleep(2) 
            
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            last_error = f"Failed to parse valid data from AI response: {str(e)}"
            print(f"Key {idx + 1} returned invalid data. Cascading to next key...")
            time.sleep(2)
            
    # FAIL-SAFE TRIGGERED
    return {
        "is_match": False, 
        "ai_failed": True, 
        "reason": f"AI processing failed. Exact error: {last_error[:100]}",
        "email": "-",
        "phone": "-",
        "link": default_url
    }

def save_to_database(db, job_data, chat_id):
    if db is None: return False
    try:
        doc_id = f"{chat_id}_{job_data['Company']}_{job_data['Title']}_{job_data['Date']}".replace(" ", "_").replace("/", "-")
        doc_ref = db.collection("job_applications").document(doc_id)
        if doc_ref.get().exists: return False
        doc_ref.set(job_data)
        return True
    except Exception as e:
        print(f"Database Save Error: {e}")
        return False

def send_telegram_message(message, chat_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    try:
        response = requests.post(url, json=payload)
        if response.status_code != 200:
            print(f"TELEGRAM FAILED! Status: {response.status_code}, Body: {response.text}")
    except requests.exceptions.RequestException as e:
        print(f"Telegram Error sending to {chat_id}: {e}")

def main():
    if not VALID_KEYS:
        print("CRITICAL: No valid OpenRouter API keys found. Halting execution.")
        return

    db = get_firestore_client()
    if not db: return

    users = fetch_active_users(db)
    if not users: return

    print(f"Found {len(users)} active users. Starting batch processing...")
    today_str = datetime.today().strftime('%d-%m-%Y')

    for user in users:
        chat_id = user.get("chat_id")
        user_name = user.get("name", "User")
        location = user.get("location", "India")
        search_terms = user.get("search_terms", [])
        
        print(f"\n--- Scraping for {user_name} in {location} ---")
        if not search_terms: continue
            
        search_query = " OR ".join([f'"{term}"' if ' ' in term else term for term in search_terms])
        
        try:
            jobs_df = scrape_jobs(
                site_name=["linkedin"], search_term=search_query, location=location,
                results_wanted=15, hours_old=1, country_indeed='India'
            )
        except Exception as e:
            print(f"Scraper error for {user_name}: {e}")
            continue
        
        if jobs_df is None or jobs_df.empty: 
            print(f"No new jobs found for {user_name}.")
            continue

        print(f"Found {len(jobs_df)} jobs for {user_name}. Analyzing with OpenRouter...")
        match_count = 0
        
        for _, row in jobs_df.iterrows():
            title = row.get('title', 'Unknown Title')
            company = row.get('company', 'Unknown Company')
            job_url = row.get('job_url', '#')
            job_location = row.get('location', 'Unknown Location')
            description = row.get('description', '')
            
            extracted = parse_and_filter_job(description, title, company, job_url, search_terms)
            is_match = extracted.get("is_match", False)
            ai_failed = extracted.get("ai_failed", False) 
            reason = extracted.get("reason", "No reason provided.")
            
            if is_match and not ai_failed: 
                match_count += 1
            
            job_record = {
                "Date": today_str, "User": user_name, "Type": "Email", 
                "Application Status": "Applied" if is_match else ("AI Failed" if ai_failed else "Rejected by AI"),
                "Link": extracted.get("link", job_url), "Email": extracted.get("email", "-"),
                "Phone": extracted.get("phone", "-"), "Company": company, "Title": title,
                "Is Match": is_match, "Reason": reason
            }
            
            if save_to_database(db, job_record, chat_id):
                
                # Condition 2: If AI Processing Fails (Sends Raw Description)
                if ai_failed:
                    # Safely escape HTML characters and truncate to 1500 chars to respect Telegram limits
                    safe_desc = html.escape(str(description))
                    short_desc = safe_desc[:1500] + "...\n[Truncated]" if len(safe_desc) > 1500 else safe_desc
                    
                    msg = (f"⚠️ <b>[AI Processing Failed] Raw Job Info</b>\n\n"
                           f"💼 <b>Role:</b> {html.escape(title)}\n"
                           f"🏢 <b>Company:</b> {html.escape(company)}\n"
                           f"📍 <b>Location:</b> {html.escape(job_location)}\n\n"
                           f"📝 <b>Job Description Snippet:</b>\n<i>{short_desc}</i>\n\n"
                           f"<a href='{job_record['Link']}'>View Full Job & Apply</a>")
                
                # Condition 3: AI Succeeds and finds a Match
                elif is_match:
                    msg = (f"🚨 <b>New Job Match for {user_name}!</b>\n\n"
                           f"💼 <b>Role:</b> {html.escape(title)}\n"
                           f"🏢 <b>Company:</b> {html.escape(company)}\n"
                           f"📍 <b>Location:</b> {html.escape(job_location)}\n"
                           f"📧 <b>HR Email:</b> {job_record['Email']}\n\n"
                           f"✅ <b>Why it matches:</b> {html.escape(reason)}\n\n"
                           f"<a href='{job_record['Link']}'>Apply Here</a>")
                
                # Condition 1: AI Succeeds and Rejects it
                else:
                    msg = (f"❌ <b>Irrelevant Job Found for {user_name}</b>\n\n"
                           f"💼 <b>Role:</b> {html.escape(title)}\n"
                           f"🏢 <b>Company:</b> {html.escape(company)}\n"
                           f"📍 <b>Location:</b> {html.escape(job_location)}\n\n"
                           f"❌ <b>Why it was rejected:</b> {html.escape(reason)}\n\n"
                           f"<a href='{job_record['Link']}'>View Anyway</a>")
                    
                send_telegram_message(msg, chat_id)
                print(f"Sent alert to {user_name} for {company} (AI Failed: {ai_failed}).")
                
            time.sleep(3) 
                
        print(f"Finished processing for {user_name}: {match_count} relevant matches.")

if __name__ == "__main__":
    main()
