import webbrowser
import requests
import base64
import os
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.environ.get("PINTEREST_CLIENT_ID")
CLIENT_SECRET = os.environ.get("PINTEREST_CLIENT_SECRET")
REDIRECT_URI = "https://localhost/"
SCOPE = "boards:read,boards:write,pins:read,pins:write,user_accounts:read"

auth_url = (
    f"https://www.pinterest.com/oauth/"
    f"?client_id={CLIENT_ID}"
    f"&redirect_uri={REDIRECT_URI}"
    f"&response_type=code"
    f"&scope={SCOPE}"
    f"&state=awon_demo"
)

print("\n Opening Pinterest authorization page in your browser...")
print("Log in, click Give access, then copy the 'code' from the URL bar.\n")
webbrowser.open(auth_url)

code = input("Paste the code here and press Enter: ").strip()

credentials = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()

response = requests.post(
    "https://api.pinterest.com/v5/oauth/token",
    headers={
        "Authorization": f"Basic {credentials}",
        "Content-Type": "application/x-www-form-urlencoded",
    },
    data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    },
    timeout=30,
)

print("\n Response:")
print(response.json())