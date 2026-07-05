import os
import json
import base64
import threading
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ConversationHandler, filters, ContextTypes
from google.cloud import firestore
from google.oauth2 import service_account

# --- Tiny Web Server for Render Compliance ---
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Bot Agent Registry is Running Live!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host='0.0.0.0', port=port)

# --- Configuration & Secrets ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

# --- Conversation States ---
ASK_LOCATION, ASK_JOBS = range(2)

def get_firestore_client():
    """Initializes Firestore, handling both Base64 and raw JSON formats securely."""
    try:
        raw_creds = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a welcome message when a user types /start."""
    welcome_msg = (
        "👋 <b>Welcome to the AI Job Hunter SaaS Agent!</b>\n\n"
        "I will monitor job boards and send you personalized AI-filtered matches.\n\n"
        "To get started, simply type /register"
    )
    await update.message.reply_text(welcome_msg, parse_mode='HTML')

async def register_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for registration. Checks for one-liner or starts conversation."""
    
    # Check if they used the advanced one-liner format (e.g. /register Pune ; Data Analyst)
    if context.args:
        user_input = " ".join(context.args)
        if ";" in user_input:
            return await process_legacy_registration(update, context, user_input)

    # If they just typed /register, start the interactive flow
    await update.message.reply_text(
        "Let's set up your automated job alerts! 🚀\n\n"
        "<b>First, which city and country are you looking for jobs in?</b>\n"
        "(For example: <i>Pune, India</i> or <i>Remote</i>)",
        parse_mode='HTML'
    )
    return ASK_LOCATION

async def register_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Saves the user's location and asks for their job titles."""
    context.user_data['location'] = update.message.text
    
    await update.message.reply_text(
        f"Got it! Location set to: <b>{context.user_data['location']}</b>\n\n"
        "<b>Next, what job titles or keywords are you looking for?</b>\n"
        "(Please separate them with commas, e.g., <i>Data Analyst, Python, MIS</i>)",
        parse_mode='HTML'
    )
    return ASK_JOBS

async def register_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Saves the jobs, completes registration, and writes to Firestore."""
    jobs_text = update.message.text
    location = context.user_data.get('location', 'India')
    search_terms = [term.strip() for term in jobs_text.split(",") if term.strip()]
    
    await finalize_registration(update, location, search_terms)
    
    # End the conversation flow
    return ConversationHandler.END

async def cancel_registration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancels the interactive registration."""
    await update.message.reply_text("🛑 Registration cancelled. Type /register anytime to start over.")
    return ConversationHandler.END

async def process_legacy_registration(update: Update, context: ContextTypes.DEFAULT_TYPE, user_input: str):
    """Handles the old semicolon-separated one-liner for power users."""
    location_part, jobs_part = user_input.split(";", 1)
    location = location_part.strip()
    search_terms = [term.strip() for term in jobs_part.split(",")]
    
    await finalize_registration(update, location, search_terms)
    return ConversationHandler.END

async def finalize_registration(update: Update, location: str, search_terms: list):
    """Helper function to write data to Firestore and send success message."""
    chat_id = str(update.message.chat_id)
    user_name = update.message.from_user.first_name
    db = get_firestore_client()
    
    if db is None:
        await update.message.reply_text("❌ Database connection error. Please try again later.")
        return

    try:
        user_data = {
            "chat_id": chat_id,
            "name": user_name,
            "location": location,
            "search_terms": search_terms,
            "active": True
        }
        db.collection("users").document(chat_id).set(user_data)
        
        success_msg = (
            f"✅ <b>Registration Successful!</b>\n\n"
            f"📍 <b>Location:</b> {location}\n"
            f"💼 <b>Searching for:</b> {', '.join(search_terms)}\n\n"
            f"You will now receive automated matching updates directly in this chat! (Type /stop to pause anytime)"
        )
        await update.message.reply_text(success_msg, parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text("❌ An error occurred while saving to the database.")
        print(f"Registration Error: {e}")

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deactivates a user's subscription profile."""
    db = get_firestore_client()
    if db is None:
        return

    chat_id = str(update.message.chat_id)
    try:
        db.collection("users").document(chat_id).update({"active": False})
        await update.message.reply_text("🛑 Your job alerts have been paused. Send a new /register configuration to restart.")
    except Exception as e:
        print(f"Deactivation Error: {e}")
        await update.message.reply_text("❌ Failed to update alert preferences.")

def main():
    threading.Thread(target=run_flask, daemon=True).start()
    print("Starting the Telegram Bot Listener...")
    
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .build()
    )
    
    # --- The New Conversation UI Handler ---
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("register", register_start)],
        states={
            ASK_LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_location)],
            ASK_JOBS: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_jobs)],
        },
        fallbacks=[CommandHandler("cancel", cancel_registration)]
    )
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(conv_handler)
    
    app.run_polling()

if __name__ == "__main__":
    main()
