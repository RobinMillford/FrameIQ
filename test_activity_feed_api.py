import requests
import json

BASE_URL = "http://127.0.0.1:5000"

def test_global_feed():
    print("\nTesting Global Feed...")
    url = f"{BASE_URL}/api/social/global-feed"
    response = requests.get(url)
    print(f"Status Code: {response.status_code}")
    if response.status_code == 200:
        data = response.json()
        print(f"Total reviews: {data.get('total')}")
        print(f"Reviews in this page: {len(data.get('reviews', []))}")
        if data.get('reviews'):
            first = data['reviews'][0]
            print(f"Latest review by: {first['user']['username']} for {first['media']['title']}")
    else:
        print(f"Error: {response.text}")

def test_following_feed():
    # Note: Requires authentication session
    print("\nTesting Following Feed (requires login)...")
    print("Skipping direct auth test in script, checking endpoint structure.")
    url = f"{BASE_URL}/api/social/feed"
    response = requests.get(url)
    # Expect 401 if not logged in (since login_required is on it)
    print(f"Status Code (expect 401 if unauth): {response.status_code}")

if __name__ == "__main__":
    test_global_feed()
    test_following_feed()
