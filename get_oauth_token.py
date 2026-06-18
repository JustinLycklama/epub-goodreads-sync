from google_auth_oauthlib.flow import InstalledAppFlow

flow = InstalledAppFlow.from_client_secrets_file(
    "client_secret.json",
    scopes=["https://www.googleapis.com/auth/drive"],
)
creds = flow.run_local_server(port=0)

print("\n--- Add these as GitHub Actions secrets ---")
print(f"GDRIVE_CLIENT_ID:     {creds.client_id}")
print(f"GDRIVE_CLIENT_SECRET: {creds.client_secret}")
print(f"GDRIVE_REFRESH_TOKEN: {creds.refresh_token}")
