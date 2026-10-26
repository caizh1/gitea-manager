"""默认动态网络与显式地址在重建、失败回退中保持原来的分配方式。"""
import copy
import unittest
from unittest.mock import MagicMock

from services.restore_secrets import SecretTransition


class RestoreNetworkTests(unittest.TestCase):
    def transition(self, ipam):
        client, original = MagicMock(), MagicMock()
        original.name, original.id, original.short_id = 'gitea', 'original-id', 'original-short'
        original.attrs = {'Image': '测试固定镜像', 'Config': {'Env': ['GITEA__security__SECRET_KEY=测试密钥']},
            'HostConfig': {}, 'Mounts': [], 'NetworkSettings': {'Networks': {'fixture_default': {
                'Aliases': ['gitea', 'original-id', 'original-short', 'business-alias'],
                'IPAddress': '172.18.0.3', 'GlobalIPv6Address': 'fd00::3', 'IPAMConfig': ipam}}}}
        client.api._result.return_value = {'Id': 'replacement-id'}
        return SecretTransition(client, original, '测试事务'), client, original

    def test_dynamic_assignment_is_not_promoted_to_static_during_creation(self):
        for ipam in (None, {}):
            with self.subTest(原配置=ipam):
                transition, client, _ = self.transition(ipam)
                transition.switch()
                endpoint = client.api._post_json.call_args.kwargs['data']['NetworkingConfig']['EndpointsConfig']['fixture_default']
                self.assertNotIn('IPAMConfig', endpoint)
                self.assertEqual(endpoint['Aliases'], ['gitea', 'business-alias'])

    def test_creation_failure_rolls_back_with_dynamic_assignment(self):
        transition, client, original = self.transition(None)
        client.api._post_json.side_effect = RuntimeError('受控创建失败')
        with self.assertRaisesRegex(RuntimeError, '受控创建失败'):
            transition.switch()
        self.assertIs(transition.rollback(), original)
        client.networks.get.return_value.connect.assert_called_once_with(
            original, aliases=['gitea', 'business-alias'], ipv4_address=None, ipv6_address=None)
        original.rename.assert_called_with('gitea')

    def test_validation_failure_removes_replacement_and_reconnects_original(self):
        transition, client, original = self.transition(None)
        replacement = transition.switch()
        self.assertIsNot(replacement, original)
        self.assertIs(transition.rollback(), original)
        replacement.remove.assert_called_once_with(force=True)
        client.networks.get.return_value.connect.assert_called_once_with(
            original, aliases=['gitea', 'business-alias'], ipv4_address=None, ipv6_address=None)

    def test_explicit_ipv4_ipv6_and_link_local_addresses_are_preserved_without_aliasing(self):
        ipam = {'IPv4Address': '172.29.0.8', 'IPv6Address': 'fd00::8', 'LinkLocalIPs': ['169.254.10.8']}
        transition, client, original = self.transition(ipam)
        saved = copy.deepcopy(original.attrs)
        endpoint = transition.endpoint('fixture_default', saved['NetworkSettings']['Networks']['fixture_default'])
        self.assertEqual(endpoint['IPAMConfig'], ipam)
        endpoint['IPAMConfig']['LinkLocalIPs'].append('169.254.10.9')
        self.assertEqual(original.attrs, saved)
        transition.switch(); transition.rollback()
        client.networks.get.return_value.connect.assert_called_once_with(
            original, aliases=['gitea', 'business-alias'], ipv4_address='172.29.0.8', ipv6_address='fd00::8')

    def test_builtin_bridge_does_not_receive_user_network_settings(self):
        transition, _, original = self.transition(None)
        self.assertEqual(transition.endpoint('bridge', original.attrs['NetworkSettings']['Networks']['fixture_default']), {})


if __name__ == '__main__':
    unittest.main()
