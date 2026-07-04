import os
import json
import base64
import threading
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from google.cloud import firestore
from google.oauth2 import service_account

# --- Tiny Web Server for Render Compliance ---
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Bot Agent Registry is Running Live!"

def run_flask():
    # Render provides a PORT environment variable dynamically
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host='0.0.0.0', port=port)

# --- Configuration & Secrets ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

def get_firestore_client():
    """Initializes Firestore, handling both Base64 and raw JSON formats securely."""
    try:
        raw_creds = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
        
        if not raw_creds:
            print("Error: FIREBASE_SERVICE_ACCOUNT environment variable is missing.")
            return None

        # Attempt to decode assuming it's a Base64 string from GitHub/Render settings
        try:
            decoded_str = base64.b64decode(raw_creds).decode('utf-8')
            creds_dict = json.loads(decoded_str)
        except Exception:
            # If Base64 decoding fails, fallback and assume it's raw unencoded JSON
            creds_dict = json.loads(raw_creds)
            
        credentials = service_account.Credentials.from_service_account_info(creds_dict)
        return firestore.Client(credentials=credentials)
    except Exception as e:
        print(f"Error initializing Firestore: {e}")
        return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a welcome message when a user types /start."""
    welcome_msg = (
        "👋 <b>Welcome to the AI Job Hunter SaaS Agent!</b>\n\n"
        "I will monitor job boards and send you personalized AI-filtered matches.\n\n"
        "To register, send the /register command with your location and job titles separated by a semicolon (;).\n\n"
        "<b>Example:</b>\n"
        "<code>/register Pune, India ; Data Analyst, Python Developer</code>"
    )
    await update.message.reply_text(welcome_msg, parse_mode='HTML')

async def register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Registers a user and saves their profile configurations to Firestore."""
    db = get_firestore_client()
    
    if db is None:
        await update.message.reply_text("❌ Database connection could not be established. Please check system configurations.")
        return

    chat_id = str(update.message.chat_id)
    user_name = update.message.from_user.first_name

    if not context.args:
        await update.message.reply_text("⚠️ Please provide your location and job titles.\nExample: <code>/register Pune ; Data Analyst</code>", parse_mode='HTML')
        return

    user_input = " ".join(context.args)
    if ";" not in user_input:
        await update.message.reply_text("⚠️ Please separate your location and job titles with a semicolon (;).\nExample: <code>/register Pune ; Data Analyst</code>", parse_mode='HTML')
        return

    try:
        # Parse configuration tokens
        location_part, jobs_part = user_input.split(";", 1)
        location = location_part.strip()
        search_terms = [term.strip() for term in jobs_part.split(",")]

        user_data = {
            "chat_id": chat_id,
            "name": user_name,
            "location": location,
            "search_terms": search_terms,
            "active": True
        }
        
        # Write user specification to the dedicated database collection
        db.collection("users").document(chat_id).set(user_data)
        
        success_msg = (
            f"✅ <b>Registration Successful!</b>\n\n"
            f"📍 <b>Location:</b> {location}\n"
            f"💼 <b>Searching for:</b> {', '.join(search_terms)}\n\n"
            f"You will now receive automated matching updates directly in this chat!"
        )
        await update.message.reply_text(success_msg, parse_mode='HTML')
        
    except Exception as e:
        await update.message.reply_text(f"❌ An error occurred during registration. Details logged internally.")
        print(f"Registration Error Trace: {e}")

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deactivates a user's subscription profile."""
    db = get_firestore_client()
    if db is None:
        await update.message.reply_text("❌ Connection error. Unable to process command.")
        return

    chat_id = str(update.message.chat_id)
    try:
        db.collection("users").document(chat_id).update({"active": False})
        await update.message.reply_text("🛑 Your job alerts have been paused. Send a new /register configuration to restart.")
    except Exception as e:
        print(f"Deactivation Error: {e}")
        await update.message.reply_text("❌ Failed to update alert preferences.")

def main():
    # 1. Start the micro web endpoint inside a background daemon thread
    threading.Thread(target=run_flask, daemon=True).start()
    
    # 2. Initialize the application engine with extended network tolerance settings
    print("Starting the Telegram Bot Listener...")
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .build()
    )
    
    # Register command pathways
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("register", register))
    app.add_handler(CommandHandler("stop", stop))
    
    # Start long-polling interface
    app.run_polling()

if __name__ == "__main__":
    main()
