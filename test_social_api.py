"""
Test script for Social/Following API endpoints
Tests follow/unfollow, followers list, following list, and follow status
"""
import requests
import json

# Base URL - adjust if needed
BASE_URL = "http://localhost:5000"

# Test user credentials (you'll need to create these users first or use existing ones)
USER1_EMAIL = "test1@example.com"
USER1_PASSWORD = "password123"

USER2_EMAIL = "test2@example.com"
USER2_PASSWORD = "password123"


def login(email, password):
    """Login and return session"""
    session = requests.Session()
    response = session.post(f"{BASE_URL}/login", data={
        'email': email,
        'password': password
    }, allow_redirects=False)
    
    if response.status_code in [200, 302]:
        print(f"✅ Logged in as {email}")
        return session
    else:
        print(f"❌ Failed to login as {email}")
        return None


def test_follow_unfollow():
    """Test following and unfollowing a user"""
    print("\n" + "="*50)
    print("TEST: Follow/Unfollow User")
    print("="*50)
    
    # Login as user 1
    session1 = login(USER1_EMAIL, USER1_PASSWORD)
    if not session1:
        return
    
    # Get user 2's ID (you'll need to adjust this)
    user2_id = 2  # Replace with actual user ID
    
    # Test 1: Follow user 2
    print(f"\n1. Following user {user2_id}...")
    response = session1.post(f"{BASE_URL}/api/users/{user2_id}/follow")
    print(f"Status: {response.status_code}")
    print(f"Response: {json.dumps(response.json(), indent=2)}")
    
    if response.status_code == 200:
        data = response.json()
        if data.get('is_following'):
            print("✅ Successfully followed user")
        else:
            print("❌ Follow failed")
    
    # Test 2: Unfollow user 2
    print(f"\n2. Unfollowing user {user2_id}...")
    response = session1.post(f"{BASE_URL}/api/users/{user2_id}/follow")
    print(f"Status: {response.status_code}")
    print(f"Response: {json.dumps(response.json(), indent=2)}")
    
    if response.status_code == 200:
        data = response.json()
        if not data.get('is_following'):
            print("✅ Successfully unfollowed user")
        else:
            print("❌ Unfollow failed")
    
    # Test 3: Try to follow self (should fail)
    print(f"\n3. Trying to follow self (should fail)...")
    response = session1.post(f"{BASE_URL}/api/users/1/follow")  # Assuming user1 has ID 1
    print(f"Status: {response.status_code}")
    print(f"Response: {json.dumps(response.json(), indent=2)}")
    
    if response.status_code == 400:
        print("✅ Correctly prevented self-follow")
    else:
        print("❌ Should have prevented self-follow")


def test_followers_list():
    """Test getting followers list"""
    print("\n" + "="*50)
    print("TEST: Get Followers List")
    print("="*50)
    
    session = login(USER1_EMAIL, USER1_PASSWORD)
    if not session:
        return
    
    user_id = 2  # Replace with actual user ID
    
    print(f"\nGetting followers for user {user_id}...")
    response = session.get(f"{BASE_URL}/api/users/{user_id}/followers")
    print(f"Status: {response.status_code}")
    
    if response.status_code == 200:
        data = response.json()
        print(f"Total followers: {data.get('total', 0)}")
        print(f"Followers on this page: {len(data.get('followers', []))}")
        
        if data.get('followers'):
            print("\nFollowers:")
            for follower in data['followers'][:3]:  # Show first 3
                print(f"  - {follower['username']} (ID: {follower['id']})")
        
        print("✅ Successfully retrieved followers list")
    else:
        print(f"❌ Failed to get followers: {response.json()}")


def test_following_list():
    """Test getting following list"""
    print("\n" + "="*50)
    print("TEST: Get Following List")
    print("="*50)
    
    session = login(USER1_EMAIL, USER1_PASSWORD)
    if not session:
        return
    
    user_id = 1  # Replace with actual user ID
    
    print(f"\nGetting users followed by user {user_id}...")
    response = session.get(f"{BASE_URL}/api/users/{user_id}/following")
    print(f"Status: {response.status_code}")
    
    if response.status_code == 200:
        data = response.json()
        print(f"Total following: {data.get('total', 0)}")
        print(f"Following on this page: {len(data.get('following', []))}")
        
        if data.get('following'):
            print("\nFollowing:")
            for user in data['following'][:3]:  # Show first 3
                print(f"  - {user['username']} (ID: {user['id']})")
        
        print("✅ Successfully retrieved following list")
    else:
        print(f"❌ Failed to get following: {response.json()}")


def test_follow_status():
    """Test checking follow status"""
    print("\n" + "="*50)
    print("TEST: Check Follow Status")
    print("="*50)
    
    session = login(USER1_EMAIL, USER1_PASSWORD)
    if not session:
        return
    
    user_id = 2  # Replace with actual user ID
    
    print(f"\nChecking follow status with user {user_id}...")
    response = session.get(f"{BASE_URL}/api/users/{user_id}/follow-status")
    print(f"Status: {response.status_code}")
    
    if response.status_code == 200:
        data = response.json()
        print(f"Response: {json.dumps(data, indent=2)}")
        print(f"  - You follow them: {data.get('is_following')}")
        print(f"  - They follow you: {data.get('is_followed_by')}")
        print(f"  - Mutual follow: {data.get('is_mutual')}")
        print("✅ Successfully retrieved follow status")
    else:
        print(f"❌ Failed to get follow status: {response.json()}")


def test_pagination():
    """Test pagination on followers/following lists"""
    print("\n" + "="*50)
    print("TEST: Pagination")
    print("="*50)
    
    session = login(USER1_EMAIL, USER1_PASSWORD)
    if not session:
        return
    
    user_id = 1
    
    print(f"\nTesting pagination (page 1, 5 per page)...")
    response = session.get(f"{BASE_URL}/api/users/{user_id}/followers?page=1&per_page=5")
    
    if response.status_code == 200:
        data = response.json()
        print(f"Total: {data.get('total')}")
        print(f"Pages: {data.get('pages')}")
        print(f"Current page: {data.get('current_page')}")
        print(f"Has next: {data.get('has_next')}")
        print(f"Items on page: {len(data.get('followers', []))}")
        print("✅ Pagination working correctly")
    else:
        print(f"❌ Pagination test failed")


if __name__ == "__main__":
    print("\n" + "="*60)
    print("SOCIAL/FOLLOWING API TESTS")
    print("="*60)
    print(f"Base URL: {BASE_URL}")
    print(f"User 1: {USER1_EMAIL}")
    print(f"User 2: {USER2_EMAIL}")
    print("\nNOTE: Make sure these users exist in your database!")
    print("="*60)
    
    try:
        # Run all tests
        test_follow_unfollow()
        test_followers_list()
        test_following_list()
        test_follow_status()
        test_pagination()
        
        print("\n" + "="*60)
        print("ALL TESTS COMPLETED")
        print("="*60)
        
    except Exception as e:
        print(f"\n❌ Error running tests: {e}")
        import traceback
        traceback.print_exc()
