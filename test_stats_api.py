import requests
import json

BASE_URL = "http://127.0.0.1:5000"

def test_user_stats(user_id=1):
    print(f"\nTesting User Stats for ID {user_id}...")
    url = f"{BASE_URL}/api/users/{user_id}/stats"
    try:
        response = requests.get(url)
        print(f"Status Code: {response.status_code}")
        if response.status_code == 200:
            data = response.json()
            print("Successfully fetched stats!")
            print(json.dumps(data, indent=2))
            
            # Verify structure
            assert 'stats' in data
            assert 'genre_distribution' in data
            assert 'genre_performance' in data
            assert 'rating_distribution' in data
            assert 'monthly_activity' in data
            
            stats = data['stats']
            assert 'movies_watched' in stats
            assert 'tv_watched' in stats
            assert 'watchlist_completion' in stats
            
            print("\nStructure validation passed!")
            print(f"Movies: {stats['movies_watched']}, TV: {stats['tv_watched']}")
            print(f"Watchlist Progress: {stats['watchlist_completion']}%")
            print(f"Top Performance Genre: {data['genre_performance']['labels'][0] if data['genre_performance']['labels'] else 'N/A'}")
        else:
            print(f"Error: {response.status_code}")
            print(response.text)
    except Exception as e:
        print(f"Request failed: {e}")

if __name__ == "__main__":
    # Test with user 1 (standard test user)
    test_user_stats(3)
