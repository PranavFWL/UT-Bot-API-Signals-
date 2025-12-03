from dotenv import load_dotenv
import os

# Load .env file
load_dotenv()

USERNAME = os.getenv("USERNAME")
PASSWORD = os.getenv("PASSWORD")
API_KEY = os.getenv("API_KEY")

# Sanity check
if not USERNAME or not PASSWORD or not API_KEY:
    raise ValueError("Missing required environment variables!")
