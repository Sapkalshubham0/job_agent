import os
import json
from datetime import datetime
import requests
from jobspy import scrape_jobs
from google import genai
from google.cloud import firestore
from google.oauth2 import service_account

# --- Configuration & Secrets ---
# These must be set in your GitHub Actions Repository Secrets
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
FIREBASE_CREDS_JSON = os.environ.get("FIREBASE_SERVICE_ACCOUNT")

# Define your core search parameters
SEARCH_TERMS = ["Data Analyst", "Data Engineer", "Python Developer"]
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
    """Uses Gemini to filter the job and extract HR contact details into JSON."""
    if not job_description:
        return {"is_match": False}
        
    client = genai.Client(api_key=GEMINI_API_KEY)
    
    prompt = f"""
    You are an expert recruitment automation assistant. 
    Review this job post for a candidate specializing in SQL, Power BI, and Python.
    They are looking for entry-level roles or short-term internships.
    
    Job Title: {title}
    Company: {company}
    Job Description: {job_description[:3000]}
    
    Tasks:
    1. Determine if this is a strong match based ONLY on the candidate's skills and experience level (true/false).
    2. Extract any specific HR email addresses mentioned for application submissions.
    3. Extract any contact phone numbers mentioned.
    4. Extract any external application links mentioned in the text (if none, use '{default_url}').
    
    Provide your response strictly in the following JSON format:
    {{
        "is_match": true/false,
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
        return {"is_match": False}

def save_to_database(db, job_data):
    """Saves the record to Firestore to mirror your tracking spreadsheet."""
    if db is None:
        return False
        
    try:
        # Create a unique document ID based on company and date to prevent duplicates
        doc_id = f"{job_data['Company']}_{job_data['Date']}".replace(" ", "_").replace("/", "-")
        doc_ref = db.collection("job_applications").document(doc_id)
        
        # If this exact job was already logged today, skip it
        if doc_ref.get().exists:
            return False
            
        # Write to Firestore matching your spreadsheet columns
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
    jobs_df = scrape_jobs(
        site_name=["linkedin"], 
        search_term=" OR ".join(SEARCH_TERMS),
        location=LOCATION,
        results_wanted=15, 
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
        
        if extracted.get("is_match"):
            match_count += 1
            
            # Prepare data to match your spreadsheet columns exactly
            job_record = {
                "Date": today_str,
                "Type": "Email", 
                "Application Status": "Applied",
                "Link": extracted.get("link", job_url),
                "Email": extracted.get("email", "-"),
                "Phone": extracted.get("phone", "-"),
                # Adding these two for better database context
                "Company": company,
                "Title": title
            }
            
            # 4. Save to Database
            is_new = save_to_database(db, job_record)
            
            # 5. Send Alert (Only if it's a new database entry)
            if is_new:
                msg = (
                    f"🚨 <b>New Job Match Logged!</b>\n\n"
                    f"💼 <b>Role:</b> {title}\n"
                    f"🏢 <b>Company:</b> {company}\n"
                    f"📧 <b>HR Email:</b> {job_record['Email']}\n"
                    f"📞 <b>Phone:</b> {job_record['Phone']}\n\n"
                    f"<a href='{job_record['Link']}'>Apply Here</a>"
                )
                send_telegram_message(msg)
                print(f"Logged & Sent alert for: {title} at {company}")
            else:
                print(f"Skipped duplicate alert for: {title} at {company}")
                
    print(f"Finished. Processed {match_count} relevant matches out of {len(jobs_df)} jobs.")

if __name__ == "__main__":
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GEMINI_API_KEY, FIREBASE_CREDS_JSON]):
        print("CRITICAL: Missing one or more API keys in environment variables!")
    else:
        main()
        