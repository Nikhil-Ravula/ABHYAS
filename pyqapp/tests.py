from django.test import TestCase, override_settings
from django.core.cache import cache
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from pyqapp.models import HQSSONonce, UserSession
from types import SimpleNamespace
import secrets
from unittest.mock import patch

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

    @patch('pyqapp.views.sync_user_to_hq')
    def test_login_creates_usersession_and_increments_login_count(self, mirror):
        # First login (should create UserSession and set login_count=1)
        response = self.client.post(reverse('login'), {
            'username': self.username,
            'password': self.password,
        })
        self.assertEqual(response.status_code, 302)  # Should redirect to dashboard
        
        user_session = UserSession.objects.get(user=self.user)
        self.assertEqual(user_session.login_count, 1)
        self.assertIsNotNone(user_session.session_key)
        mirror.assert_called_once()
        self.assertEqual(mirror.call_args.args[0], self.user)
        self.assertIs(mirror.call_args.args[1], response.wsgi_request)

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


class RegistrationViewTests(TestCase):
    @patch('pyqapp.views.sync_user_to_hq')
    def test_registration_schedules_hq_mirror_after_local_user_creation(self, mirror):
        response = self.client.post(reverse('register'), {
            'username': 'new-local-user',
            'email': 'new-local@example.com',
            'password': 'testpassword123',
        })

        self.assertEqual(response.status_code, 302)
        user = User.objects.get(username='new-local-user')
        mirror.assert_called_once()
        self.assertEqual(mirror.call_args.args[0], user)
        self.assertIs(mirror.call_args.args[1], response.wsgi_request)


class HQMirrorTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='mirror-user',
            email='mirror@example.com',
            password='testpassword123',
            first_name='Mirror',
            last_name='User',
        )

    @override_settings(
        HQ_SYNC_SECRET='',
        HQ_SYNC_URL='https://novamymentor.in/api/hq/users/sync',
    )
    @patch('pyqapp.hq_sync.threading.Thread')
    def test_mirror_is_scheduled_without_waiting_for_remote_request(self, thread):
        from pyqapp.hq_sync import sync_user_to_hq
        sync_secret = secrets.token_urlsafe(32)

        with self.settings(HQ_SYNC_SECRET=sync_secret):
            scheduled, status, result = sync_user_to_hq(self.user)

        self.assertEqual((scheduled, status, result), (True, None, 'scheduled'))
        thread.assert_called_once()
        thread.return_value.start.assert_called_once_with()
        payload, url, secret = thread.call_args.kwargs['args']
        self.assertEqual(payload['source_app'], 'abhyas')
        self.assertEqual(payload['app_key'], 'abhyas')
        self.assertEqual(payload['external_id'], str(self.user.pk))
        self.assertEqual(payload['username'], self.user.username)
        self.assertEqual(payload['email'], self.user.email)
        self.assertEqual(url, 'https://novamymentor.in/api/hq/users/sync')
        self.assertEqual(secret, sync_secret)

    @override_settings(
        HQ_SYNC_SECRET='',
        HQ_SYNC_URL='https://novamymentor.in/api/hq/users/sync',
    )
    @patch('pyqapp.hq_sync.requests.post')
    def test_worker_posts_contract_payload_without_secret_in_json(self, post):
        from pyqapp.hq_sync import _build_payload, _post_user_to_hq

        post.return_value = SimpleNamespace(status_code=201)
        payload = _build_payload(self.user)
        sync_secret = secrets.token_urlsafe(32)
        result = _post_user_to_hq(
            payload,
            'https://novamymentor.in/api/hq/users/sync',
            sync_secret,
        )

        self.assertEqual(result, (True, 201, 'ok'))
        post.assert_called_once_with(
            'https://novamymentor.in/api/hq/users/sync',
            json=payload,
            headers={
                'Content-Type': 'application/json',
                'X-HQ-Sync-Secret': sync_secret,
            },
            timeout=5,
            allow_redirects=False,
        )
        self.assertNotIn('_sync_secret', post.call_args.kwargs['json'])

    @override_settings(HQ_SYNC_SECRET='')
    @patch('pyqapp.hq_sync.threading.Thread')
    def test_mirror_fails_closed_without_sync_secret(self, thread):
        from pyqapp.hq_sync import sync_user_to_hq

        self.assertEqual(sync_user_to_hq(self.user), (False, None, 'no_secret'))
        thread.assert_not_called()


class HQCallbackTests(TestCase):
    def setUp(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public_key = self.private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        cache.clear()

    def _token(self, **overrides):
        import time
        import uuid
        import jwt

        now = int(time.time())
        payload = {
            'iss': 'https://novamymentor.in',
            'aud': 'abhyas',
            'sub': 'hq-user',
            'email': 'hq-user@example.com',
            'iat': now,
            'exp': now + 60,
            'jti': uuid.uuid4().hex,
        }
        payload.update(overrides)
        return jwt.encode(payload, self.private_key, algorithm='RS256')

    def test_callback_rejects_when_public_key_is_missing(self):
        with self.settings(HQ_JWT_PUBLIC_KEY=''):
            response = self.client.get(
                reverse('hq_callback'),
                {'hq_token': self._token()},
            )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(User.objects.filter(username='hq-user').exists())

    def test_callback_fails_closed_when_public_key_is_malformed(self):
        with self.settings(HQ_JWT_PUBLIC_KEY='not-a-pem-key'):
            response = self.client.get(
                reverse('hq_callback'),
                {'hq_token': self._token()},
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn('auth_error=1', response.url)
        self.assertFalse(User.objects.filter(username='hq-user').exists())

    def test_callback_accepts_valid_rs256_token_once(self):
        with patch('pyqapp.hq_views.sync_user_to_hq') as mirror:
            with self.settings(HQ_JWT_PUBLIC_KEY=self.public_key):
                token = self._token()
                first = self.client.get(reverse('hq_callback'), {'hq_token': token})
                second = self.client.get(reverse('hq_callback'), {'hq_token': token})

        self.assertEqual(first.status_code, 302)
        self.assertEqual(first.url, reverse('links'))
        self.assertEqual(second.status_code, 302)
        self.assertIn('auth_error=1', second.url)
        user = User.objects.get(username='hq-user')
        self.assertFalse(user.has_usable_password())
        self.assertTrue(UserSession.objects.filter(user=user).exists())
        nonce = HQSSONonce.objects.get()
        remaining = (nonce.expires_at - timezone.now()).total_seconds()
        self.assertGreater(remaining, 65)
        self.assertLessEqual(remaining, 70)
        mirror.assert_called_once()
        self.assertEqual(mirror.call_args.args[0], user)
        self.assertIs(mirror.call_args.args[1], first.wsgi_request)

    def test_callback_rejects_wrong_issuer_and_long_claim_window(self):
        import time

        with self.settings(HQ_JWT_PUBLIC_KEY=self.public_key):
            wrong_issuer = self.client.get(
                reverse('hq_callback'),
                {'hq_token': self._token(iss='https://evil.example')},
            )
            long_window = self.client.get(
                reverse('hq_callback'),
                {'hq_token': self._token(exp=int(time.time()) + 61)},
            )

        self.assertIn('auth_error=1', wrong_issuer.url)
        self.assertIn('auth_error=1', long_window.url)
        self.assertFalse(User.objects.filter(username='hq-user').exists())

    def test_callback_rate_limit_rejects_after_one_attempt(self):
        with self.settings(
            HQ_JWT_PUBLIC_KEY=self.public_key,
            HQ_SSO_RATE_LIMIT=1,
        ):
            first = self.client.get(
                reverse('hq_callback'),
                {'hq_token': self._token(jti='rate-limit-one')},
            )
            second = self.client.get(
                reverse('hq_callback'),
                {'hq_token': self._token(jti='rate-limit-two')},
            )

        self.assertEqual(first.url, reverse('links'))
        self.assertIn('auth_error=1', second.url)

    def test_callback_rejects_wrong_audience_and_non_rs256_algorithm(self):
        import jwt
        import time

        with self.settings(HQ_JWT_PUBLIC_KEY=self.public_key):
            wrong_audience = self.client.get(
                reverse('hq_callback'),
                {'hq_token': self._token(aud='vitharn')},
            )
            hs256_token = jwt.encode(
                {
                    'iss': 'https://novamymentor.in',
                    'aud': 'abhyas',
                    'sub': 'hs256-user',
                    'iat': int(time.time()),
                    'exp': int(time.time()) + 60,
                    'jti': 'hs256-jti',
                },
                secrets.token_bytes(32),
                algorithm='HS256',
            )
            wrong_algorithm = self.client.get(
                reverse('hq_callback'),
                {'hq_token': hs256_token},
            )

        self.assertIn('auth_error=1', wrong_audience.url)
        self.assertIn('auth_error=1', wrong_algorithm.url)
        self.assertFalse(User.objects.filter(username='hs256-user').exists())

    def test_callback_fails_closed_when_replay_database_is_unavailable(self):
        with self.settings(HQ_JWT_PUBLIC_KEY=self.public_key):
            with patch(
                'pyqapp.hq_views.HQSSONonce.objects.create',
                side_effect=RuntimeError,
            ):
                response = self.client.get(
                    reverse('hq_callback'),
                    {'hq_token': self._token(jti='cache-failure-jti')},
                )

        self.assertIn('auth_error=1', response.url)
        self.assertFalse(User.objects.filter(username='hq-user').exists())
