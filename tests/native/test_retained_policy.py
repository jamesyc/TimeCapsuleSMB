"""Exercise the stateless service helper with the manager's in-memory policy."""
import subprocess

import pytest

from tests.native.build import compile_native
from tests.native.test_plan import (
    NAT_OK, NAT_DENIED, NAT_ACP_DEAD, NAT_RECREATED, NAT_LINKS, NAT_ADDRS, facts_text,
    build_plans, roles, status, bind,
)


@pytest.fixture(scope='module')
def service(tmp_path_factory):
    return compile_native('service', tmp_path_factory.mktemp('retained-service') / 'service')


def probe(service, tmp_path, text, history='policy none\n'):
    facts = tmp_path / 'facts'
    facts.write_text(text)
    result = subprocess.run([str(service), '--print-smb-bind-interfaces', '--retain-policy',
                             '--facts-file', str(facts)], input=history,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    tokens, state, policy = result.stdout.split('\n', 2)
    return set(tokens.split()), state, policy


def test_policy_roundtrip_retains_grants_denials_and_prunes_recreated_links(service, tmp_path):
    tokens, state, history = probe(service, tmp_path, NAT_OK)
    assert state == 'status=validated' and '192.168.1.10/24' in tokens
    retained, state, history = probe(service, tmp_path, NAT_ACP_DEAD, history)
    assert retained == tokens and state == 'status=incomplete reason=mode'
    recreated, state, history = probe(service, tmp_path, NAT_RECREATED, history)
    assert '10.0.1.1/24' not in recreated and '192.168.1.10/24' in recreated
    # The old index reappearing later must not revive the retired LAN grant.
    returned, _, history = probe(service, tmp_path, NAT_ACP_DEAD, history)
    assert '10.0.1.1/24' not in returned
    denied, state, history = probe(service, tmp_path, NAT_DENIED, history)
    assert state == 'status=validated' and '192.168.1.10/24' not in denied
    retained, _, _ = probe(service, tmp_path, NAT_ACP_DEAD, history)
    assert retained == denied


def test_cold_service_never_publishes_unvalidated_tokens(service, tmp_path):
    for text in (NAT_ACP_DEAD, NAT_OK.replace('key=usbF status=ok value=0x458', 'key=usbF status=abort value=')):
        tokens, state, history = probe(service, tmp_path, text)
        assert tokens == {'127.0.0.1/8', '::1/128'}
        assert state.startswith('status=incomplete reason=') and history == 'policy none\n'


@pytest.mark.parametrize('history', [
    'garbage\n', 'policy 0 1\n', 'policy 3 2\n', 'policy 1 0 extra\n',
    'policy 1 0\n0 0 bridge0\n', 'policy 1 0\n9 4 bridge0\n',
    'policy 1 0\n9 0 bridge0\n9 1 mgi1\n', 'policy none\n9 0 bridge0\n',
    'policy 1 0\n' + ''.join(f'{i+1} 0 bridge{i}\n' for i in range(17)),
])
def test_untrusted_policy_input_cannot_create_a_grant(service, tmp_path, history):
    facts = tmp_path / 'facts'
    facts.write_text(NAT_ACP_DEAD)
    result = subprocess.run([str(service), '--print-smb-bind-interfaces', '--retain-policy',
                             '--facts-file', str(facts)], input=history,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 13 and result.stdout == ''


def test_kernel_failure_retains_native_addresses_until_complete_observation(tmp_path):
    unavailable = facts_text(acp={}, iflist_ok=0)
    first, failed, recovered = build_plans(tmp_path, NAT_OK, unavailable, NAT_DENIED)
    assert bind(first) == bind(failed)
    assert status(failed)['reason'] == 'iflist'
    assert roles(recovered)['mgi1'] == ('wan', 'none')


def test_native_loop_history_does_not_resurrect_a_disappeared_link(tmp_path):
    absent = facts_text(acp={}, links=[('mgi1', 2)], addrs=[(2, '192.168.1.10', 24)])
    returned = facts_text(acp={}, links=NAT_LINKS, addrs=NAT_ADDRS)
    _, _, last = build_plans(tmp_path, NAT_OK, absent, returned)
    assert roles(last)['bridge0'] == ('isolated', 'none')
    assert roles(last)['mgi1'] == ('wan', 'smb,adisk')


def test_kernel_failure_keeps_latest_observed_address_without_refreshing_policy_age(tmp_path):
    changed_address = NAT_ACP_DEAD.replace('addr=10.0.1.1 ', 'addr=10.0.1.2 ')
    first, changed, failed = build_plans(tmp_path, NAT_OK, changed_address,
                                       facts_text(acp={}, iflist_ok=0))
    assert '10.0.1.1/24' in bind(first)
    assert '10.0.1.2/24' in bind(changed) and '10.0.1.1/24' not in bind(changed)
    assert bind(failed) == bind(changed)
    assert status(changed)['stale_seconds'] == '10'
    assert status(failed)['stale_seconds'] == '20'
