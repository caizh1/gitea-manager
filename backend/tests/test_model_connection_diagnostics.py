"""连接测试错误分类、凭据脱敏及接口详情回传。"""
import json
import unittest
from unittest.mock import Mock, patch
import requests
from ai_review.setup_service import verify_model
from ai_review.common import ReviewError


def reply(value, status=200):
    response = requests.Response()
    response.status_code = status
    response._content = (value if isinstance(value, str) else json.dumps(value)).encode()
    return response


class ConnectionDiagnosticsTests(unittest.TestCase):
    def test_failure_paths_and_redaction(self):
        cases = [
            (requests.exceptions.ConnectTimeout('敏感参数'), 'MODEL_CONNECT_TIMEOUT'),
            (requests.exceptions.ReadTimeout('敏感参数'), 'MODEL_READ_TIMEOUT'),
            (requests.exceptions.SSLError('敏感参数'), 'MODEL_TLS_ERROR'),
            (reply('密钥为 fixture-secret，服务不可用', 503), 'MODEL_HTTP_ERROR'),
            (reply('<html>错误页面</html>'), 'MODEL_INVALID_JSON'),
            (reply({'choices': []}), 'MODEL_SCHEMA_ERROR'),
            (reply({'choices': [{'finish_reason': 'length'}]}), 'MODEL_INCOMPLETE'),
            (reply({'choices': [{'finish_reason': 'stop', 'message': {'content': '非 JSON'}}]}), 'MODEL_INVALID_JSON'),
        ]
        for result, code in cases:
            with self.subTest(code=code), patch('ai_review.setup_service.endpoint', return_value='http://fixture/v1'), patch('ai_review.setup_service.requests.Session') as factory:
                client = factory.return_value.__enter__.return_value
                client.get.return_value = reply({'data': [{'id': 'fixture'}]})
                if isinstance(result, Exception):
                    client.post.side_effect = result
                else:
                    client.post.return_value = result
                with self.assertLogs(level='WARNING') as logs, self.assertRaises(ReviewError) as caught:
                    verify_model(Mock(unseal=Mock(return_value='fixture-secret')), {'api_base_url':'http://fixture/v1','api_key':'密文','model':'fixture'})
                detail = caught.exception.connection_diagnostic
                self.assertEqual(detail['error_code'], code)
                self.assertEqual(detail['request_path'], '/chat/completions')
                self.assertNotIn('fixture-secret', json.dumps(detail) + str(logs.output))
                self.assertNotIn('敏感参数', str(logs.output))
                self.assertEqual(detail['http_status'], None if isinstance(result, Exception) else result.status_code)
                from flask import Flask
                from routes.ai_review_routes import review_error
                with Flask('连接测试').app_context():
                    response, status = review_error(caught.exception)
                    self.assertEqual(status, 400)
                    self.assertEqual(response.json['diagnostic'], detail)

    def test_directory_failure_and_success(self):
        with patch('ai_review.setup_service.endpoint', return_value='http://fixture/v1'), patch('ai_review.setup_service.requests.Session') as factory:
            client = factory.return_value.__enter__.return_value
            store = Mock(unseal=Mock(return_value='fixture-secret'))
            config = {'api_base_url':'http://fixture/v1','api_key':'密文','model':'fixture'}
            client.get.return_value = reply([])
            with self.assertLogs(level='WARNING'), self.assertRaises(ReviewError) as caught:
                verify_model(store, config)
            self.assertEqual(caught.exception.connection_diagnostic['phase'], '读取模型目录')
            client.post.assert_not_called()
            client.get.return_value = reply({'data':[{'id':'fixture'}]})
            client.post.return_value = reply({'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}]})
            self.assertTrue(verify_model(store, config)['listed'])
