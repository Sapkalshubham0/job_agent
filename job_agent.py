import os
import json
from datetime import datetime
import requests
from jobspy import scrape_jobs
from google import genai
from google.cloud import firestore
from google.oauth2 import service_account

# --- Configuration & Secrets ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
FIREBASE_CREDS_JSON = os.environ.get("FIREBASE_SERVICE_ACCOUNT")

def get_firestore_client():
    try:
        creds_dict = json.loads(FIREBASE_CREDS_JSON)
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
    """Uses Gemini to evaluate the job based on a specific user's requirements."""
    if not job_description:
        return {"is_match": False, "reason": "No job description provided."}
        
    client = genai.Client(api_key=GEMINI_API_KEY)
    
    # Notice we now inject the user's specific search terms into the AI prompt
    terms_string = ", ".join(user_search_terms)
    
    prompt = f"""
    You are an expert recruitment automation assistant. 
    Review this job post for a candidate looking for roles related to: {terms_string}.
    They are looking for entry-level roles, junior positions, or short-term internships.
    
    Job Title: {title}
    Company: {company}
    Job Description: {job_description[:3000]}
    
    Tasks:
    1. Determine if this is a strong match based on the candidate's specific keywords ({terms_string}) and an entry-level/junior experience level (true/false).
    2. Provide a brief, 1-2 sentence 'reason' explaining exactly WHY it is or isn't a match.
    3. Extract any specific HR email addresses mentioned.
    4. Extract any contact phone numbers mentioned.
    5. Extract any external application links mentioned (if none, use '{default_url}').
    
    Provide your response strictly in the following JSON format:
    {{
        "is_match": true/false,
        "reason": "Brief explanation here...",
        "email": "extracted_email_or_-",
        "phone": "extracted_phone_or_-",
        "link": "extracted_link_or_default_url"
    }}
    """
    
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config={"response_mime_type": "application/json"}
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"Gemini API Error: {e}")
        return {"is_match": False, "reason": "AI processing failed."}

def save_to_database(db, job_data, chat_id):
    """Saves the record to Firestore to prevent duplicate alerts per user."""
    if db is None:
        return False
        
    try:
        # Crucial: Add chat_id to the doc_id so duplicates are tracked per person
        doc_id = f"{chat_id}_{job_data['Company']}_{job_data['Title']}_{job_data['Date']}".replace(" ", "_").replace("/", "-")
        doc_ref = db.collection("job_applications").document(doc_id)
        
        if doc_ref.get().exists:
            return False
            
        doc_ref.set(job_data)
        return True
    except Exception as e:
        print(f"Database Save Error: {e}")
        return False

def send_telegram_message(message, chat_id):
    """Sends a notification to a specific user via Telegram Bot."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        requests.post(url, json=payload).raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Telegram Error sending to {chat_id}: {e}")

def main():
    db = get_firestore_client()
    if not db:
        print("CRITICAL: Database failed to initialize.")
        return

    users = fetch_active_users(db)
    print(f"Found {len(users)} active users in the database.")

    today_str = datetime.today().strftime('%d-%m-%Y')

    # Loop through every registered person
    for user in users:
        chat_id = user.get("chat_id")
        user_name = user.get("name", "User")
        location = user.get("location", "India")
        search_terms = user.get("search_terms", [])
        
        print(f"\n--- Scraping for {user_name} in {location} ---")
        
        if not search_terms:
            continue
            
        search_query = " OR ".join([f'"{term}"' if ' ' in term else term for term in search_terms])
        
        jobs_df = scrape_jobs(
            site_name=["linkedin"], 
            search_term=search_query,
            location=location,
            results_wanted=15, 
            hours_old=48,       
            country_indeed='India'
        )
        
        if jobs_df.empty:
            print(f"No new jobs found for {user_name}.")
            continue

        print(f"Found {len(jobs_df)} jobs for {user_name}. Analyzing with Gemini...")
        
        for _, row in jobs_df.iterrows():
            title = row.get('title', 'Unknown Title')
            company = row.get('company', 'Unknown Company')
            job_url = row.get('job_url', '#')
            description = row.get('description', '')
            
            extracted = parse_and_filter_job(description, title, company, job_url, search_terms)
            is_match = extracted.get("is_match", False)
            reason = extracted.get("reason", "No reason provided.")
            
            job_record = {
                "Date": today_str,
                "User": user_name,
                "Type": "Email", 
                "Application Status": "Applied" if is_match else "Rejected by AI",
                "Link": extracted.get("link", job_url),
                "Email": extracted.get("email", "-"),
                "Phone": extracted.get("phone", "-"),
                "Company": company,
                "Title": title,
                "Is Match": is_match,
                "Reason": reason
            }
            
            is_new = save_to_database(db, job_record, chat_id)
            
            if is_new:
                if is_match:
                    msg = (
                        f"🚨 <b>New Job Match for {user_name}!</b>\n\n"
                        f"💼 <b>Role:</b> {title}\n"
                        f"🏢 <b>Company:</b> {company}\n"
                        f"📧 <b>HR Email:</b> {job_record['Email']}\n\n"
                        f"✅ <b>Why it matches:</b> {reason}\n\n"
                        f"<a href='{job_record['Link']}'>Apply Here</a>"
                    )
                else:
                    msg = (
                        f"⚠️ <b>Irrelevant Job Found</b>\n\n"
                        f"💼 <b>Role:</b> {title}\n"
                        f"🏢 <b>Company:</b> {company}\n\n"
                        f"❌ <b>Why it was rejected:</b> {reason}\n\n"
                        f"<a href='{job_record['Link']}'>View Anyway</a>"
                    )
                    
                send_telegram_message(msg, chat_id)
                print(f"Sent alert to {user_name} for {company}.")
            else:
                print(f"Skipped duplicate alert for {company}.")

if __name__ == "__main__":
    main()
