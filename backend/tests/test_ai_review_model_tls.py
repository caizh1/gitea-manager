"""受控慢 TLS 握手：旧五秒失败，新上限内成功；不自动重放请求。"""
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
import ipaddress
import json
import ssl
import threading
import time
import unittest
from unittest.mock import patch

import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

import test_ai_review as baseline
from ai_review.broker import create_broker
from ai_review.engine import capability
from ai_review.model_transport import fetch_model, ModelTransportError


class ModelTlsTests(unittest.TestCase):
    setUp=baseline.StoreTests.setUp
    enqueue=baseline.StoreTests.enqueue

    def test_slow_verified_handshake_completes_once_without_disabling_tls(self):
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'本机隔离模型验收')])
        cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
              .serial_number(x509.random_serial_number()).not_valid_before(datetime.now(timezone.utc)-timedelta(days=1))
              .not_valid_after(datetime.now(timezone.utc)+timedelta(days=1))
              .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False)
              .sign(key,hashes.SHA256()))
        certificate=self.root/'本机测试证书.pem';private=self.root/'本机测试私钥.pem'
        certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        private.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()));private.chmod(0o600)
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(certificate,private)
        received=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']));received.append(self.path)
                body=json.dumps({'choices':[{'finish_reason':'stop','message':{'content':'{"findings":[]}'}}]}).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        class Server(HTTPServer):
            def get_request(self):
                sock,address=super().get_request();sock.settimeout(15);time.sleep(6)
                try:return context.wrap_socket(sock,server_side=True),address
                except OSError:sock.close();raise
        server=Server(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        target=f'https://127.0.0.1:{server.server_port}/v1'
        with requests.Session() as old:
            old.trust_env=False;old.verify=str(certificate)
            with self.assertRaises(requests.RequestException):old.get(target,timeout=(5,20))
        self.assertEqual(received,[])
        with self.assertRaises(ModelTransportError) as untrusted:
            fetch_model(target+'/chat/completions', {}, 'test-only-key', timeout=15)
        self.assertEqual(untrusted.exception.kind,'SSLError')
        self.assertEqual(received,[], '不可信证书必须在发送模型请求前被拒绝')
        self.enqueue();job=self.store.claim();token=capability(self.store,job,'测试模型')
        def verified_fetch(*args, **kwargs):return fetch_model(*args, **kwargs, ca_bundle=str(certificate))
        with patch('ai_review.broker.endpoint',return_value=target),patch.object(self.store,'unseal',return_value='test-only-key'),patch('ai_review.broker.fetch_model',side_effect=verified_fetch):
            client=create_broker(self.store).test_client();response=client.post('/v1/chat/completions',json={'model':'测试模型','messages':[{'role':'user','content':'仅验证握手'}]},headers={'Authorization':'Bearer '+token})
        self.assertEqual(response.status_code,200);self.assertEqual(received,['/v1/chat/completions'])
        self.assertEqual(self.store.job(job['id'])['model_calls'],1)


if __name__=='__main__':unittest.main()
