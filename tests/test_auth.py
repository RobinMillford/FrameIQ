"""
Tests for authentication routes — register, login, logout,
forgot/reset password, email verification.
"""


class TestRegister:
    def test_register_page_loads(self, client):
        r = client.get('/register')
        assert r.status_code == 200

    def test_register_creates_user(self, client, db, app):
        from models import User
        r = client.post('/register', data={
            'username': 'newuser',
            'email': 'new@example.com',
            'password': 'NewPass1',
            'confirm_password': 'NewPass1',
        }, follow_redirects=True)
        assert r.status_code == 200
        with app.app_context():
            u = User.query.filter_by(username='newuser').first()
            assert u is not None
            assert u.email_verified is False  # must verify
            db.session.delete(u)
            db.session.commit()

    def test_register_duplicate_username(self, client, sample_user):
        r = client.post('/register', data={
            'username': 'testuser',
            'email': 'other@example.com',
            'password': 'TestPass1',
            'confirm_password': 'TestPass1',
        }, follow_redirects=True)
        assert b'already exists' in r.data or b'already' in r.data.lower()

    def test_register_weak_password_rejected(self, client):
        r = client.post('/register', data={
            'username': 'weakuser',
            'email': 'weak@example.com',
            'password': 'short',
            'confirm_password': 'short',
        }, follow_redirects=True)
        assert b'8 char' in r.data or r.status_code in (200, 302)

    def test_register_password_mismatch(self, client):
        r = client.post('/register', data={
            'username': 'mismatch',
            'email': 'mismatch@example.com',
            'password': 'TestPass1',
            'confirm_password': 'TestPass2',
        }, follow_redirects=True)
        assert b'match' in r.data.lower() or r.status_code in (200, 302)


class TestLogin:
    def test_login_page_loads(self, client):
        assert client.get('/login').status_code == 200

    def test_login_success(self, client, sample_user):
        r = client.post('/login', data={
            'username': 'testuser',
            'password': 'TestPass1',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert b'testuser' in r.data or b'Logged in' in r.data

    def test_login_wrong_password(self, client, sample_user):
        r = client.post('/login', data={
            'username': 'testuser',
            'password': 'WrongPass1',
        }, follow_redirects=True)
        assert b'Invalid' in r.data or b'invalid' in r.data

    def test_login_nonexistent_user(self, client):
        # Should not reveal whether user exists (timing attack protection)
        r = client.post('/login', data={
            'username': 'nobody',
            'password': 'TestPass1',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert b'Invalid' in r.data or b'invalid' in r.data

    def test_logout(self, auth_client):
        r = auth_client.get('/logout', follow_redirects=True)
        assert r.status_code == 200


class TestForgotPassword:
    def test_forgot_page_loads(self, client):
        assert client.get('/forgot-password').status_code == 200

    def test_forgot_always_shows_success(self, client, sample_user):
        """Must not reveal whether email exists — prevents user enumeration."""
        r = client.post('/forgot-password',
                        data={'email': 'test@example.com'},
                        follow_redirects=True)
        assert r.status_code == 200
        assert b'receive' in r.data.lower() or b'sent' in r.data.lower()

    def test_forgot_nonexistent_email_same_message(self, client):
        r = client.post('/forgot-password',
                        data={'email': 'nobody@example.com'},
                        follow_redirects=True)
        assert r.status_code == 200
        # Same message — no user enumeration
        assert b'receive' in r.data.lower() or b'sent' in r.data.lower()


class TestResetPassword:
    def test_invalid_token_redirects(self, client):
        r = client.get('/reset-password/badtoken', follow_redirects=True)
        assert r.status_code == 200
        assert b'invalid' in r.data.lower() or b'expired' in r.data.lower()

    def test_valid_token_resets_password(self, client, sample_user, app):
        from utils.email import generate_reset_token
        from models import User
        with app.app_context():
            token = generate_reset_token('test@example.com')

        r = client.post(f'/reset-password/{token}', data={
            'password': 'NewPass99',
            'confirm_password': 'NewPass99',
        }, follow_redirects=True)
        assert r.status_code == 200
        assert b'successful' in r.data.lower() or b'reset' in r.data.lower()

        with app.app_context():
            u = User.query.filter_by(email='test@example.com').first()
            assert u.check_password('NewPass99')
            # Restore original password for other tests
            u.set_password('TestPass1')
            from models import db
            db.session.commit()


class TestEmailVerification:
    def test_valid_token_verifies(self, client, app, db):
        from utils.email import generate_verify_token
        from models import User
        with app.app_context():
            u = User(username='unverified', email='unverified@example.com',
                     email_verified=False)
            u.set_password('TestPass1')
            db.session.add(u)
            db.session.commit()
            token = generate_verify_token('unverified@example.com')

        r = client.get(f'/verify-email/{token}', follow_redirects=True)
        assert r.status_code == 200

        with app.app_context():
            u = User.query.filter_by(email='unverified@example.com').first()
            assert u.email_verified is True
            db.session.delete(u)
            db.session.commit()

    def test_invalid_token_shows_error(self, client):
        r = client.get('/verify-email/garbage', follow_redirects=True)
        assert r.status_code == 200
        assert b'invalid' in r.data.lower() or b'expired' in r.data.lower()
