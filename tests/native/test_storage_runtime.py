"""Real filesystem setup with injected Apple mount/activation observations."""
import os
import plistlib
import subprocess
import sys

import pytest

from tests.native.build import ROOT, instrumentation_flags


@pytest.fixture(scope="module")
def storage_tools(tmp_path_factory):
    root=tmp_path_factory.mktemp("storage-native")
    config=root/"runtime.conf"
    acp=root/"acp"
    acp.write_text(f"#!{sys.executable}\n" + '''
import os,sys
from pathlib import Path
assert sys.argv[1:3]==['rpc','diskd.useVolume']
root=sys.argv[3].removeprefix('path:s:')
with open(os.environ['CLAIMS'],'a') as log:log.write(root+'\\n')
if os.environ.get('CLAIM_FAIL')=='1':sys.exit(1)
mounts=Path(os.environ['TC_TEST_MOUNTS'])
rows=[row for row in mounts.read_text().splitlines() if not row.startswith(root+' ')]
rows.append(root+' '+Path(root).name+' 1')
mounts.write_text('\\n'.join(rows)+'\\n')
''')
    acp.chmod(0o755)
    binary=root/"storage"
    modules=["storage/mast.c","storage/shares.c","storage/runtime.c","samba/config.c",
             "common/config.c","common/worker.c","common/process.c","common/parent.c","common/acp.c"]
    subprocess.run(["cc","-D_GNU_SOURCE","-DTC_NATIVE_TEST","-Wall","-Wextra","-Werror",
                    *instrumentation_flags(),f'-DTC_FLASH_CONFIG_PATH="{config}"',f'-DTC_ACP_PATH="{acp}"',
                    f'-DTC_VOLUMES_ROOT="{root}"',"-I",str(ROOT/"build/native"),
                    *(str(ROOT/"build/native"/module) for module in modules),
                    str(ROOT/"tests/native/unit/test_storage_runtime.c"),"-o",str(binary)],check=True,capture_output=True)
    return root,config,binary


@pytest.fixture
def storage(storage_tools):
    import shutil
    root,config,binary=storage_tools
    for name in ['dk2','dk3']:
        path=root/name
        shutil.rmtree(path,ignore_errors=True)
        path.mkdir()
    mounts=root/'mounts';mounts.write_text('')
    claims=root/'claims';claims.write_text('')
    config.write_text('DISKD_USE_VOLUME_ATTEMPTS=1\nDISKD_USE_VOLUME_MOUNT_TIMEOUT_SECONDS=0\n')
    disks=[dict(deviceName='sd0',builtin=True,partitions=[dict(deviceName='dk2',name='Data',format='hfs',users=1,uuid='11111111-1111-1111-1111-111111111111')]),
           dict(deviceName='sd1',partitions=[dict(deviceName='dk3',name='USB',format='hfs',users=1,uuid='22222222-2222-2222-2222-222222222222')])]
    def run(*args,fail=False):
        return subprocess.run([str(binary),*args],input=plistlib.dumps(disks),capture_output=True,timeout=10,
                              env={**os.environ,'TC_TEST_MOUNTS':str(mounts),'CLAIMS':str(claims),'CLAIM_FAIL':str(int(fail))})
    return root,config,mounts,claims,disks,run


def payload(root,device):
    home=root/device/'.samba4';home.mkdir();(home/'private').mkdir()
    (home/'smbd').write_bytes(b'fake executable payload');(home/'smbd').chmod(0o755)
    return home


def test_new_volumes_claimed_once_and_internal_payload_preferred(storage):
    root,_,_,claims,_,run=storage
    selected=payload(root,'dk2');payload(root,'dk3')
    first=run();assert first.returncode==0,first.stderr
    assert f'available=3 payload={selected} shares=2'.encode() in first.stdout
    assert claims.read_text().splitlines()==[str(root/'dk2'),str(root/'dk3')]
    assert (root/'dk2/ShareRoot/.com.apple.timemachine.supported').is_file()
    assert (root/'dk3/.com.apple.timemachine.supported').is_file()
    claims.write_text('')
    second=run('already-active');assert second.returncode==0,second.stderr
    assert claims.read_text()==''


def test_users_zero_reclaims_unchanged_apple_volume(storage):
    root,_,mounts,claims,disks,run=storage
    payload(root,'dk2');mounts.write_text(f'{root}/dk2 dk2 1\n{root}/dk3 dk3 1\n')
    disks[0]['partitions'][0]['users']=0
    result=run('already-active');assert result.returncode==0,result.stderr
    assert claims.read_text().splitlines()==[str(root/'dk2')]


def test_activation_failure_never_writes_to_unmounted_directories(storage):
    root,_,_,_,_,run=storage
    payload(root,'dk2')
    result=run(fail=True);assert result.returncode==0,result.stderr
    assert b'available=0 payload= shares=0' in result.stdout
    assert not (root/'dk2/ShareRoot').exists()
    assert not (root/'dk3/.com.apple.timemachine.supported').exists()


def test_readonly_volume_excluded_and_external_payload_used(storage):
    root,_,mounts,_,_,run=storage
    payload(root,'dk2');selected=payload(root,'dk3')
    mounts.write_text(f'{root}/dk2 dk2 0\n{root}/dk3 dk3 1\n')
    result=run('already-active');assert result.returncode==0,result.stderr
    assert f'available=2 payload={selected} shares=1'.encode() in result.stdout
    assert not (root/'dk2/ShareRoot').exists()


def test_root_option_and_missing_disabled_rsync_do_not_hide_valid_payload(storage):
    root,config,_,_,_,run=storage
    home=payload(root,'dk2');config.write_text('INTERNAL_SHARE_USE_DISK_ROOT=1\n')
    result=run();assert result.returncode==0,result.stderr
    assert f'payload={home}'.encode() in result.stdout
    assert (root/'dk2/.com.apple.timemachine.supported').is_file()
    assert not (root/'dk2/ShareRoot').exists()


def test_marker_symlink_is_never_followed(storage):
    root,_,_,_,_,run=storage
    payload(root,'dk3')
    protected=root/'protected';protected.write_text('preserve')
    (root/'dk3/.com.apple.timemachine.supported').symlink_to(protected)
    result=run();assert result.returncode==0,result.stderr
    assert protected.read_text()=='preserve'
    assert b'available=1 payload= shares=1' in result.stdout


def test_retained_guard_rejects_replaced_root_at_same_path(storage):
    root, _, mounts, _, _, run = storage
    mounts.write_text(f'{root}/dk2 dk2 1\n')
    result = run('guard')
    assert result.returncode == 0, result.stderr


def test_partial_payload_retry_maps_cache_by_identity_not_inventory_order(storage):
    root,_,_,_,_,run=storage
    home=payload(root,'dk2');payload(root,'dk3')
    (home/'private').rmdir()
    result=run('retry-cache')
    assert result.returncode==0,result.stderr
    assert f'available=3 payload={home} shares=2'.encode() in result.stdout
