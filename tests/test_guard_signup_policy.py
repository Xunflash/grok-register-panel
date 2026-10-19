import io
import json
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from webui.guard_signup_policy import signup_policy
from webui import monitor


class SignupPolicyTests(unittest.TestCase):
    def test_adapter_sends_only_policy_and_redacts_response(self):
        with patch('webui.guard_signup_policy.urllib.request.build_opener') as builder:
            builder.return_value.open.return_value = io.BytesIO(json.dumps({'policy': {
                'signup_short_window_risk': True, 'min_hard_generation_ms': 1000,
                'private': 'must-not-return'}}).encode())
            result = signup_policy({'guard_url': 'http://guard.test', 'guard_token': 'test-token'}, True)
            request = builder.return_value.open.call_args.args[0]
            self.assertEqual(request.method, 'PUT')
            self.assertEqual(json.loads(request.data), {'policy': {'signup_short_window_risk': True}})
            self.assertEqual(result, {'enabled': True, 'min_generation_ms': 1000, 'max_attempts': 3})

    def test_reject_string_boolean(self):
        with self.assertRaises(ValueError):
            signup_policy({}, 'false')

    def test_http_auth_validation_and_forwarding(self):
        with patch.dict(os.environ, {'MONITOR_TOKEN': 'test-policy-token'}):
            server = monitor.ThreadingHTTPServer(('127.0.0.1', 0), monitor.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f'http://127.0.0.1:{server.server_port}/api/guard/signup-policy'
            def request(method, body=None, authenticated=True):
                headers={'Content-Type':'application/json'}
                if authenticated:
                    headers['Authorization']='Bearer test-policy-token'
                data=None if body is None else json.dumps(body).encode()
                try:
                    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method=method)) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            try:
                with patch('webui.guard_signup_policy.signup_policy', return_value={'enabled':True}) as adapter:
                    self.assertEqual(request('GET', authenticated=False)[0],401)
                    self.assertEqual(request('POST', {'enabled':True}, False)[0],401)
                    self.assertEqual(request('POST', {'enabled':'false'})[0],400)
                    adapter.assert_not_called()
                    self.assertEqual(request('GET'),(200,{'enabled':True}))
                    self.assertEqual(request('POST',{'enabled':True}),(200,{'enabled':True}))
                    self.assertIs(adapter.call_args.args[1],True)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
