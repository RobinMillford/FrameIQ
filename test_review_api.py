"""
Test script for Review API endpoints
Run this to verify all endpoints are working
"""
import requests
import json

BASE_URL = "http://localhost:5000"

# Note: You'll need to be logged in to test authenticated endpoints
# For now, this tests the public endpoints

def test_get_media_reviews():
    """Test getting reviews for a movie"""
    print("\n🧪 Testing: GET /api/media/movie/550/reviews")
    response = requests.get(f"{BASE_URL}/api/media/movie/550/reviews")
    print(f"Status: {response.status_code}")
    print(f"Response: {json.dumps(response.json(), indent=2)}")
    return response.status_code == 200

def test_get_review():
    """Test getting a specific review"""
    print("\n🧪 Testing: GET /api/reviews/1")
    response = requests.get(f"{BASE_URL}/api/reviews/1")
    print(f"Status: {response.status_code}")
    if response.status_code == 200:
        print(f"Response: {json.dumps(response.json(), indent=2)}")
    else:
        print(f"Response: {response.json()}")
    return response.status_code in [200, 404]  # 404 is ok if no reviews yet

def test_get_user_reviews():
    """Test getting user's reviews"""
    print("\n🧪 Testing: GET /api/users/1/reviews")
    response = requests.get(f"{BASE_URL}/api/users/1/reviews")
    print(f"Status: {response.status_code}")
    if response.status_code == 200:
        data = response.json()
        print(f"Total reviews: {data['total']}")
        print(f"Reviews: {len(data['reviews'])}")
    else:
        print(f"Response: {response.json()}")
    return response.status_code in [200, 404]

if __name__ == "__main__":
    print("=" * 50)
    print("Review API Test Suite")
    print("=" * 50)
    
    tests = [
        ("Get Media Reviews", test_get_media_reviews),
        ("Get Specific Review", test_get_review),
        ("Get User Reviews", test_get_user_reviews),
    ]
    
    results = []
    for name, test_func in tests:
        try:
            passed = test_func()
            results.append((name, "✅ PASSED" if passed else "❌ FAILED"))
        except Exception as e:
            results.append((name, f"❌ ERROR: {str(e)}"))
    
    print("\n" + "=" * 50)
    print("Test Results")
    print("=" * 50)
    for name, result in results:
        print(f"{name}: {result}")
    
    passed_count = sum(1 for _, r in results if "✅" in r)
    print(f"\n{passed_count}/{len(tests)} tests passed")
