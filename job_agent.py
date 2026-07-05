import os
import json
import base64
import time
from datetime import datetime
import requests
from jobspy import scrape_jobs
from google.cloud import firestore
from google.oauth2 import service_account

# --- Configuration & Secrets ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
FIREBASE_CREDS_JSON = os.environ.get("FIREBASE_SERVICE_ACCOUNT")

# --- OpenRouter Waterfall Setup ---
# You can generate free keys at https://openrouter.ai/keys
API_KEYS = [
    os.environ.get("OPENROUTER_API_KEY_1"),
    os.environ.get("OPENROUTER_API_KEY_2"),
    os.environ.get("OPENROUTER_API_KEY_3")
]

# Filter out empty keys so it only tries the ones you actually provided
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
    """Uses OpenRouter's free dynamic endpoint to evaluate jobs with fallback logic."""
    if not VALID_KEYS:
        return {"is_match": False, "reason": "System Error: No OpenRouter API keys configured."}
        
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
    # Use the dynamic free router to automatically find an online, capable free model
    models_to_try = ["openrouter/free"]

    for idx, key in enumerate(VALID_KEYS):
        model = models_to_try[0] 
        
        try:
            # FIX: Removed the markdown brackets from the URL string
            response = requests.post(
                url="[https://openrouter.ai/api/v1/chat/completions](https://openrouter.ai/api/v1/chat/completions)",
                headers={
                    "Authorization": f"Bearer {key}",
                    "HTTP-Referer": "[https://github.com/Sapkalshubham0/job_agent](https://github.com/Sapkalshubham0/job_agent)",
                    "Content-Type": "application/json"
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}]
                },
                timeout=15
            )
            
            response.raise_for_status() 
            
            # Extract text and clean up any accidental markdown blocks open-source models might add
            result_text = response.json()['choices'][0]['message']['content'].strip()
            if result_text.startswith("```json"):
                result_text = result_text[7:-3].strip()
            elif result_text.startswith("```"):
                result_text = result_text[3:-3].strip()
                
            return json.loads(result_text)
        
        except Exception as e:
            last_error = str(e)
            print(f"Key {idx + 1} failed: {last_error}. Cascading...")
            time.sleep(2) 
            
    return {"is_match": False, "reason": f"AI processing failed. Exact error: {last_error[:100]}"}

def save_to_database(db, job_data, chat_id):
    """Saves the record to Firestore, tracking duplicates per user."""
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
    """Sends a notification to a specific user via Telegram."""
    # FIX: Removed the markdown brackets from the URL string
    url = f"[https://api.telegram.org/bot](https://api.telegram.org/bot){TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload)
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
            description = row.get('description', '')
            
            extracted = parse_and_filter_job(description, title, company, job_url, search_terms)
            is_match = extracted.get("is_match", False)
            reason = extracted.get("reason", "No reason provided.")
            
            if is_match: match_count += 1
            
            job_record = {
                "Date": today_str, "User": user_name, "Type": "Email", 
                "Application Status": "Applied" if is_match else "Rejected by AI",
                "Link": extracted.get("link", job_url), "Email": extracted.get("email", "-"),
                "Phone": extracted.get("phone", "-"), "Company": company, "Title": title,
                "Is Match": is_match, "Reason": reason
            }
            
            if save_to_database(db, job_record, chat_id):
                if is_match:
                    msg = (f"🚨 <b>New Job Match for {user_name}!</b>\n\n💼 <b>Role:</b> {title}\n"
                           f"🏢 <b>Company:</b> {company}\n📧 <b>HR Email:</b> {job_record['Email']}\n\n"
                           f"✅ <b>Why it matches:</b> {reason}\n\n<a href='{job_record['Link']}'>Apply Here</a>")
                else:
                    msg = (f"⚠️ <b>Irrelevant Job Found for {user_name}</b>\n\n💼 <b>Role:</b> {title}\n"
                           f"🏢 <b>Company:</b> {company}\n\n❌ <b>Why it was rejected:</b> {reason}\n\n"
                           f"<a href='{job_record['Link']}'>View Anyway</a>")
                    
                send_telegram_message(msg, chat_id)
                print(f"Sent alert to {user_name} for {company}.")
                
            time.sleep(3) # Short pause between requests
                
        print(f"Finished processing for {user_name}: {match_count} relevant matches.")

if __name__ == "__main__":
    main()
