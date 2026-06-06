from django.test import TestCase
from django.contrib.auth.models import User
from django.urls import reverse
from pyqapp.models import UserSession

class LoginViewTests(TestCase):
    def setUp(self):
        self.username = 'testuser'
        self.password = 'testpassword123'
        self.email = 'testuser@example.com'
        self.user = User.objects.create_user(
            username=self.username,
            email=self.email,
            password=self.password
        )

    def test_login_creates_usersession_and_increments_login_count(self):
        # First login (should create UserSession and set login_count=1)
        response = self.client.post(reverse('login'), {
            'username': self.username,
            'password': self.password,
        })
        self.assertEqual(response.status_code, 302)  # Should redirect to dashboard
        
        user_session = UserSession.objects.get(user=self.user)
        self.assertEqual(user_session.login_count, 1)
        self.assertIsNotNone(user_session.session_key)

        # Log out
        self.client.logout()

        # Second login (should increment login_count to 2)
        response = self.client.post(reverse('login'), {
            'username': self.username,
            'password': self.password,
        })
        self.assertEqual(response.status_code, 302)
        
        user_session.refresh_from_db()
        self.assertEqual(user_session.login_count, 2)
