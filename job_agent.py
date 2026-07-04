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
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
FIREBASE_CREDS_JSON = os.environ.get("FIREBASE_SERVICE_ACCOUNT")

# Define your broad data-focused search parameters
SEARCH_TERMS = [
    "Power BI Developer", 
    "Data Analyst", 
    "Business Analyst", 
    "MIS", 
    "Excel", 
    "SQL", 
    "Python Data Analyst", 
    "Data Scientist",
    "Data Engineer"
]
LOCATION = "Pune, India"

def get_firestore_client():
    """Initializes Firestore using the JSON string from GitHub secrets."""
    try:
        creds_dict = json.loads(FIREBASE_CREDS_JSON)
        credentials = service_account.Credentials.from_service_account_info(creds_dict)
        return firestore.Client(credentials=credentials)
    except Exception as e:
        print(f"Error initializing Firestore: {e}")
        return None

def parse_and_filter_job(job_description, title, company, default_url):
    """Uses Gemini to evaluate the job and extract a reason & contact details."""
    if not job_description:
        return {"is_match": False, "reason": "No job description provided."}
        
    client = genai.Client(api_key=GEMINI_API_KEY)
    
    prompt = f"""
    You are an expert recruitment automation assistant. 
    Review this job post for a candidate specializing in data fields (Power BI, Analytics, MIS, Excel, SQL, Python).
    They are looking for entry-level roles, junior positions, or short-term internships.
    
    Job Title: {title}
    Company: {company}
    Job Description: {job_description[:3000]}
    
    Tasks:
    1. Determine if this is a strong match based on the candidate's skills and an entry-level/junior experience level (true/false).
    2. Provide a brief, 1-2 sentence 'reason' explaining exactly WHY it is or isn't a match (e.g., 'Requires 5 years experience', 'Not a data role').
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

def save_to_database(db, job_data):
    """Saves the record to Firestore to prevent duplicate alerts."""
    if db is None:
        return False
        
    try:
        # Create a unique document ID based on company and title to prevent duplicates across runs
        doc_id = f"{job_data['Company']}_{job_data['Title']}_{job_data['Date']}".replace(" ", "_").replace("/", "-")
        doc_ref = db.collection("job_applications").document(doc_id)
        
        # If this job was already logged today, skip it
        if doc_ref.get().exists:
            return False
            
        doc_ref.set(job_data)
        return True
    except Exception as e:
        print(f"Database Save Error: {e}")
        return False

def send_telegram_message(message):
    """Sends a notification via Telegram Bot."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        requests.post(url, json=payload).raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Telegram Error: {e}")

def main():
    print(f"Starting job scrape for {LOCATION}...")
    
    # 1. Initialize Database
    db = get_firestore_client()
    if not db:
        print("Warning: Database client failed to initialize. Check FIREBASE_SERVICE_ACCOUNT secret.")
    
    # 2. Scrape Jobs
    search_query = " OR ".join([f'"{term}"' if ' ' in term else term for term in SEARCH_TERMS])
    
    jobs_df = scrape_jobs(
        site_name=["linkedin"], 
        search_term=search_query,
        location=LOCATION,
        results_wanted=25,
        hours_old=1,       
        country_indeed='India'
    )
    
    if jobs_df.empty:
        print("No new jobs found in this run.")
        return

    print(f"Found {len(jobs_df)} jobs. Analyzing with Gemini...")
    today_str = datetime.today().strftime('%d-%m-%Y')
    match_count = 0

    # 3. Process each job
    for _, row in jobs_df.iterrows():
        title = row.get('title', 'Unknown Title')
        company = row.get('company', 'Unknown Company')
        job_url = row.get('job_url', '#')
        description = row.get('description', '')
        
        # Analyze with AI
        extracted = parse_and_filter_job(description, title, company, job_url)
        is_match = extracted.get("is_match", False)
        reason = extracted.get("reason", "No reason provided.")
        
        if is_match:
            match_count += 1
            
        # Prepare data (saving everything to DB now)
        job_record = {
            "Date": today_str,
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
        
        # 4. Save to Database
        is_new = save_to_database(db, job_record)
        
        # 5. Send Alert (Only if it's a new database entry, regardless of match status)
        if is_new:
            if is_match:
                msg = (
                    f"🚨 <b>New Job Match Logged!</b>\n\n"
                    f"💼 <b>Role:</b> {title}\n"
                    f"🏢 <b>Company:</b> {company}\n"
                    f"📧 <b>HR Email:</b> {job_record['Email']}\n"
                    f"📞 <b>Phone:</b> {job_record['Phone']}\n\n"
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
                
            send_telegram_message(msg)
            print(f"Logged & Sent alert for: {title} at {company} (Match: {is_match})")
        else:
            print(f"Skipped duplicate alert for: {title} at {company}")
                
    print(f"Finished. Processed {match_count} relevant matches out of {len(jobs_df)} total jobs.")

if __name__ == "__main__":
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GEMINI_API_KEY, FIREBASE_CREDS_JSON]):
        print("CRITICAL: Missing one or more API keys in environment variables!")
    else:
        main()
