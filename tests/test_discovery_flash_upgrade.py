"""Exercise the installer against a filesystem, including interrupted retries.

Apple can unmount HFS between writes, and an interrupted software install need
not boot. Its metadata must survive and rc.local must only enable verified files.
"""
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from timecapsulesmb.core.config import AppConfig
from timecapsulesmb.deploy import executor
from timecapsulesmb.deploy.commands import (
    EnsureVolumeMountedAction, RemovePathAction, StopManagerAction,
    StopProcessAction, StopServiceRuntimeAction, StopTelemetryAction,
    StopWatchdogAction, WaitForIdleJobsAction, render_remote_action,
)
from timecapsulesmb.deploy.planner import build_deployment_plan
from timecapsulesmb.device.storage import PayloadHome, PayloadVerificationResult
from timecapsulesmb.services.callbacks import OperationCallbacks
from timecapsulesmb.services.deploy import (
    DeployDeviceError, DeployRuntimeConfig,
    upload_and_verify_deployment_payload, complete_deployment_after_upload,
)
from timecapsulesmb.transport.ssh import SshConnection, _verify_remote_size
from timecapsulesmb.transport.errors import ScpError


class Device:
    def __init__(self, root, monkeypatch):
        from tests.test_xattr_migration import fake_inventory
        monkeypatch.setattr('timecapsulesmb.services.deploy.inventory_metadata', lambda *_a: fake_inventory())
        monkeypatch.setattr('timecapsulesmb.services.deploy.inspect_sources', lambda *_a: None)
        self.root = root
        self.home = PayloadHome('/Volumes/dk2', '/dev/dk2', '.samba4')
        binary = root / 'binary'
        binary.write_bytes(b'new executable\n')
        self.plan = build_deployment_plan(
            'host', self.home, binary,  xattr_migrator_path=binary,
            rsync_path=binary, service_path=binary,
        )
        self.connection = SshConnection('host', 'unused', '', remote_has_scp=True)
        self.prepared = SimpleNamespace(
            plan=self.plan, payload_home=self.home,
            payload_context=SimpleNamespace(payload_family="netbsd6_samba4", is_netbsd4=False,
                                            startup_mode="reboot_then_verify"),
        )
        self.failure = None
        self.unmount_after = None
        self.parked = root / "unmounted"
        self.transfers = []
        self.events = []
        self.sources = {}
        self.protected = {
            '/mnt/Flash/ACPData.bin': b'Apple settings',
            '/mnt/Flash/ssh_host_key': b'SSH identity',
            '/Volumes/dk2/Backup.sparsebundle/data': b'user backup',
            '/Volumes/dk2/.samba4/private/xattr.tdb': b'pending metadata',
            '/Volumes/dk2/.samba4/private/xattr.tdb.orphaned.1': b'quarantine',
            '/Volumes/dk2/.samba4/logs/previous.log': b'diagnostics',
        }
        for name, content in self.protected.items():
            self.write(name, content)
        for name in ('/mnt/Flash/rc.local', '/mnt/Flash/service', '/mnt/Flash/.discoveryd.tmp',
                     '/mnt/Flash/mdns-advertiser', '/mnt/Flash/xattr-migrate-wrapper.sh',
                     '/Volumes/dk2/.samba4/mdns-smbd-advertiser',
                     '/Volumes/dk2/.samba4/sbin/smbd', '/Volumes/dk2/.samba4/smbd'):
            self.write(name, b'old or truncated software')
        monkeypatch.setattr(executor, 'run_scp', self.scp)
        monkeypatch.setattr(executor, 'run_ssh', self.ssh)
        monkeypatch.setattr(executor, 'ensure_volume_root_mounted_conn', self.mount)
        monkeypatch.setattr('timecapsulesmb.transport.ssh.run_ssh', self.ssh)
        monkeypatch.setattr('timecapsulesmb.transport.ssh.time.sleep', lambda _seconds: None)
        monkeypatch.setattr('timecapsulesmb.services.deploy.run_ssh', self.ssh)

    def mount(self, *args, **kwargs):
        if self.parked.exists():
            self.parked.rename(self.path('/Volumes/dk2'))
            self.events.append('remount')
        return True

    def path(self, name):
        return self.root / name.lstrip('/')

    def write(self, name, content):
        path = self.path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def scp(self, connection, source, destination, **kwargs):
        self.transfers.append(destination)
        self.sources[destination] = source.read_bytes()
        self.write(destination, source.read_bytes())
        if self.failure == 'transfer' and destination.endswith('/smbd'):
            self.write(destination, b'partial')
            raise RuntimeError('injected transfer')
        if self.failure == 'flash_transfer' and destination == '/mnt/Flash/service':
            self.write(destination, b'partial flash file')
            raise RuntimeError('injected flash transfer')
        if self.failure == 'truncate_flash' and destination == '/mnt/Flash/service':
            self.write(destination, b'truncated')
        if self.failure == 'truncate' and destination.endswith('/smbd'):
            self.write(destination, b'truncated')
        # Replace only the byte transport. Run its production size verification
        # so malformed transfers still fail when the executor stops duplicating it.
        _verify_remote_size(connection, source, destination, timeout=30)
        if destination == self.unmount_after:
            self.path('/Volumes/dk2').rename(self.parked)
            self.events.append('unmount')

    def ssh(self, connection, command, **kwargs):
        if command == '/bin/df -k /mnt/Flash':
            self.events.append('capacity')
            assert not self.path('/mnt/Flash/rc.local').exists()
            free = 0 if self.failure == 'capacity' else 4096
            return SimpleNamespace(stdout=f'Filesystem 1K-blocks Used Avail Capacity Mounted on\n/dev/flash 5000 100 {free} 2% /mnt/Flash\n')
        for prefix in ('/mnt/', '/Volumes/', '/root'):
            command = command.replace(prefix, str(self.root) + prefix)
        if self.failure == 'permissions' and 'chmod' in command:
            raise RuntimeError('injected permissions')
        result = subprocess.run(command, shell=True, executable='/bin/sh', text=True, capture_output=True)
        if result.returncode and kwargs.get("check", True):
            raise RuntimeError(result.stderr or result.stdout)
        return result

    def actions(self, connection, actions, on_action_done=None):
        for i, action in enumerate(actions, 1):
            if isinstance(action, (StopManagerAction, StopServiceRuntimeAction, StopWatchdogAction,
                                   StopProcessAction, StopTelemetryAction, WaitForIdleJobsAction)):
                self.events.append('stop')
                if self.failure == 'stop':
                    raise RuntimeError('process service runtime did not stop')
            elif isinstance(action, EnsureVolumeMountedAction):
                self.mount()
            else:
                self.ssh(connection, render_remote_action(action))
            if on_action_done:
                on_action_done(action, i, len(actions))

    def migrate(self, connection, plan, phase, **kwargs):
        self.events.append(phase)
        assert not self.path('/mnt/Flash/rc.local').exists()
        if self.failure == phase:
            raise RuntimeError('injected migration ' + phase)
        return 'migration complete'

    def flush(self, connection):
        self.events.append('flush')
        if self.failure == 'flush':
            raise RuntimeError('injected flush')

    def stage(self, name):
        if self.failure == 'boot' and name == 'enable_boot':
            raise RuntimeError('injected boot')

    def install(self):
        upload_and_verify_deployment_payload(
            AppConfig.from_values({}), self.connection, self.prepared,
            DeployRuntimeConfig(nbns_enabled=True),
            callbacks=OperationCallbacks(set_stage=self.stage),
            run_remote_actions_func=self.actions, migrate_xattrs_func=self.migrate,
            verify_payload_home=lambda *a, **k: PayloadVerificationResult(True, 'present'),
            flush_remote_writes=self.flush,
        )

    def assert_installed(self):
        assert self.transfers[-1] == '/mnt/Flash/rc.local'
        for name, content in self.sources.items():
            if name == "/mnt/Memory/tc-xattr-hfs-migrate":
                assert not self.path(name).exists()
                continue
            assert self.path(name).read_bytes() == content
            mode = 0o600 if name.endswith('.conf') else 0o755
            assert self.path(name).stat().st_mode & 0o777 == mode
        assert not self.path('/Volumes/dk2/.samba4/mdns-smbd-advertiser').exists()
        assert not self.path('/Volumes/dk2/.samba4/sbin/smbd').exists()
        assert not self.path('/mnt/Flash/xattr-migrate-wrapper.sh').exists()
        assert self.path('/mnt/Flash/service').is_file()
        assert not self.path('/mnt/Flash/mdns-advertiser').exists()
        assert not self.path('/mnt/Flash/.discoveryd.tmp').exists()
        self.assert_protected()

    def assert_protected(self):
        for name, content in self.protected.items():
            assert self.path(name).read_bytes() == content


@pytest.mark.parametrize('failure', ['capacity', 'transfer', 'flash_transfer', 'truncate', 'truncate_flash', 'permissions', 'copy', 'cleanup', 'flush', 'boot'])
def test_interrupted_install_rerun_converges(tmp_path, monkeypatch, failure):
    device = Device(tmp_path, monkeypatch)
    device.failure = failure
    with pytest.raises((RuntimeError, DeployDeviceError, ScpError)):
        device.install()
    assert not device.path('/mnt/Flash/rc.local').exists()
    device.assert_protected()
    device.failure = None
    device.install()
    device.assert_installed()
    # An identical third install has exactly the same software and settings.
    previous = dict(device.sources)
    device.install()
    device.assert_installed()
    assert device.sources == previous


def test_surviving_supervisor_blocks_deletion(tmp_path, monkeypatch):
    device = Device(tmp_path, monkeypatch)
    before = device.path('/mnt/Flash/rc.local').read_bytes()
    device.failure = 'stop'
    with pytest.raises(RuntimeError, match='did not stop'):
        device.install()
    assert device.path('/mnt/Flash/rc.local').read_bytes() == before
    assert device.transfers == []
    assert 'capacity' not in device.events


def test_absent_managed_software_installs_without_old_scripts(tmp_path, monkeypatch):
    device = Device(tmp_path, monkeypatch)
    for action in device.plan.pre_upload_actions:
        if isinstance(action, RemovePathAction):
            path = device.path(action.path)
            if path.is_file():
                path.unlink()
    device.install()
    device.assert_installed()


def test_bad_local_configuration_does_not_stop_runtime(tmp_path, monkeypatch):
    device = Device(tmp_path, monkeypatch)
    monkeypatch.setattr('timecapsulesmb.services.deploy.render_rsync_daemon_config',
                        lambda *a: (_ for _ in ()).throw(ValueError('invalid local config')))
    with pytest.raises(ValueError, match='invalid local config'):
        device.install()
    assert device.events == []
    assert device.path('/mnt/Flash/rc.local').exists()


def test_reboot_request_failure_can_be_retried_without_a_marker(tmp_path, monkeypatch):
    device = Device(tmp_path, monkeypatch)
    device.install()
    requests = []

    def reboot(*args, **kwargs):
        requests.append("reboot")
        if len(requests) == 1:
            raise RuntimeError("request failed")

    with pytest.raises(RuntimeError, match="request failed"):
        complete_deployment_after_upload(device.connection, device.prepared,
                                         no_wait=True, request_reboot_func=reboot)
    # No automatic second request: observing an ambiguous reboot belongs to
    # the reboot flow. A later explicit deploy safely replaces the software.
    assert requests == ["reboot"]
    device.install()
    result = complete_deployment_after_upload(device.connection, device.prepared,
                                              no_wait=True, request_reboot_func=reboot)
    assert result.reboot_requested and not result.verified
    device.assert_installed()


@pytest.mark.parametrize('basename', ['smbd', 'rsyncd.conf'])
def test_diskd_unmount_after_verified_transfer_is_remounted_before_permissions(tmp_path, monkeypatch, basename):
    device = Device(tmp_path, monkeypatch)
    # Apple's diskd may release an idle HDD after SCP closes it. rsyncd.conf is
    # the last HDD transfer, so that case exercises the post-upload mount guard;
    # smbd also exercises remounting before the next transfer.
    device.unmount_after = device.home.payload_dir + '/' + basename
    device.install()
    assert device.events.count('unmount') == 1
    assert device.events.count('remount') == 1
    device.assert_installed()


def test_no_legacy_tdb_skips_migrator_upload_and_both_phases(tmp_path, monkeypatch):
    from timecapsulesmb.deploy.migration import MigrationInventory
    device = Device(tmp_path, monkeypatch)
    tdb = '/Volumes/dk2/.samba4/private/xattr.tdb'
    device.path(tdb).unlink(); device.protected.pop(tdb)
    monkeypatch.setattr('timecapsulesmb.services.deploy.inventory_metadata', lambda *_a: MigrationInventory((), [], [], [], ''))
    device.install()
    assert '/mnt/Memory/tc-xattr-hfs-migrate' not in device.transfers
    assert 'copy' not in device.events and 'cleanup' not in device.events
    device.assert_installed()


def test_copy_precedes_software_removal_and_cleanup_precedes_new_config(tmp_path, monkeypatch):
    device = Device(tmp_path, monkeypatch)
    old_config = b'FRUIT_METADATA_NETATALK=1\n'
    device.write('/mnt/Flash/tcapsulesmb.conf', old_config)
    original = device.migrate
    def migrate(connection, plan, phase, **kwargs):
        assert device.path('/mnt/Flash/tcapsulesmb.conf').read_bytes() == old_config
        expected = b'old or truncated software' if phase == 'copy' else b'new executable\n'
        assert device.path('/mnt/Flash/service').read_bytes() == expected
        return original(connection, plan, phase, **kwargs)
    device.migrate = migrate
    device.install()
    assert device.path('/mnt/Flash/tcapsulesmb.conf').read_bytes() != old_config
    device.assert_installed()


def test_failed_copy_keeps_old_software_and_removes_ram_helper(tmp_path, monkeypatch):
    device = Device(tmp_path, monkeypatch)
    device.failure = 'copy'
    with pytest.raises(DeployDeviceError): device.install()
    assert device.path('/mnt/Flash/service').read_bytes() == b'old or truncated software'
    assert not device.path('/mnt/Memory/tc-xattr-hfs-migrate').exists()
    assert not device.path('/mnt/Flash/rc.local').exists()
    device.assert_protected()


def test_known_software_removed_from_every_detected_payload_only(tmp_path, monkeypatch):
    from tests.test_xattr_migration import fake_inventory
    device = Device(tmp_path, monkeypatch)
    secondary = '/Volumes/dk3/.samba4'
    for name in ('sbin/telemetry', 'sbin/service', 'manager.sh', 'mdns-advertiser'):
        device.write(secondary + '/' + name, b'old program')
    for name in ('private/xattr.tdb', 'private/xattr.tdb.orphaned.1', 'logs/old.log'):
        device.write(secondary + '/' + name, b'preserve')
    inv = fake_inventory()
    inv.volumes = (SimpleNamespace(volume_root='/Volumes/dk3', device_path='/dev/dk3'),)
    inv.payload_dirs = [secondary]
    monkeypatch.setattr('timecapsulesmb.services.deploy.inventory_metadata', lambda *_a: inv)
    device.install()
    for name in ('sbin/telemetry', 'sbin/service', 'manager.sh', 'mdns-advertiser'):
        assert not device.path(secondary + '/' + name).exists()
    for name in ('private/xattr.tdb', 'private/xattr.tdb.orphaned.1', 'logs/old.log'):
        assert device.path(secondary + '/' + name).read_bytes() == b'preserve'
