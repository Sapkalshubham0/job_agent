import os
import json
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from google.cloud import firestore
from google.oauth2 import service_account

# --- Configuration & Secrets ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
FIREBASE_CREDS_JSON = os.environ.get("FIREBASE_SERVICE_ACCOUNT")

def get_firestore_client():
    """Initializes Firestore using the JSON string from environment variables."""
    try:
        creds_dict = json.loads(FIREBASE_CREDS_JSON)
        credentials = service_account.Credentials.from_service_account_info(creds_dict)
        return firestore.Client(credentials=credentials)
    except Exception as e:
        print(f"Error initializing Firestore: {e}")
        return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a welcome message when a user types /start."""
    welcome_msg = (
        "👋 <b>Welcome to the AI Job Hunter!</b>\n\n"
        "I can monitor job boards and send you personalized AI-filtered matches.\n\n"
        "To start receiving alerts, use the /register command with your location and job titles separated by a semicolon (;).\n\n"
        "<b>Example:</b>\n"
        "<code>/register Pune, India ; Data Analyst, Python Developer, MIS</code>"
    )
    await update.message.reply_text(welcome_msg, parse_mode='HTML')

async def register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Registers a user and saves their preferences to Firestore."""
    db = get_firestore_client()
    chat_id = str(update.message.chat_id)
    user_name = update.message.from_user.first_name

    # Check if they provided arguments
    if not context.args:
        await update.message.reply_text("⚠️ Please provide your location and job titles.\nExample: <code>/register Pune ; Data Analyst, Python</code>", parse_mode='HTML')
        return

    # Reconstruct the user's message and split it by the semicolon
    user_input = " ".join(context.args)
    if ";" not in user_input:
        await update.message.reply_text("⚠️ Please separate your location and jobs with a semicolon (;).\nExample: <code>/register Pune ; Data Analyst</code>", parse_mode='HTML')
        return

    try:
        # Parse the input
        location_part, jobs_part = user_input.split(";", 1)
        location = location_part.strip()
        search_terms = [term.strip() for term in jobs_part.split(",")]

        # Save to database
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
            f"You will now receive alerts when I find matches."
        )
        await update.message.reply_text(success_msg, parse_mode='HTML')
        
    except Exception as e:
        await update.message.reply_text(f"❌ An error occurred during registration. Please try again.")
        print(f"Registration Error: {e}")

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deactivates a user's alerts."""
    db = get_firestore_client()
    chat_id = str(update.message.chat_id)
    
    db.collection("users").document(chat_id).update({"active": False})
    await update.message.reply_text("🛑 Your job alerts have been paused. Type /register with your details to start again.")

def main():
    print("Starting the Telegram Bot Listener...")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Add command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("register", register))
    app.add_handler(CommandHandler("stop", stop))
    
    # Start polling for messages
    app.run_polling()

if __name__ == "__main__":
    main()
