"""Run the actual manager with fake Apple observations and real owned children.

The fixtures replace only appliance interfaces (ACP, ps/fstat, mounted volumes,
and daemon executables). Fork/exec, groups, signals, file staging, reloads,
configuration publication, and the manager select loop remain real.
"""
import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from tests.native.build import compile_native
from tests.native.test_plan import NAT_OK


CHILD = '''
import json,os,signal,sys,time,select
from pathlib import Path
role=sys.argv[1] if Path(sys.argv[0]).name=='roles' else 'smbd'
log=Path(os.environ['TC_TEST_ROOT'])/'events'
def event(kind):
    with log.open('a') as f:f.write(json.dumps(dict(kind=kind,role=role,pid=os.getpid(),ppid=os.getppid(),group=os.getpgrp(),args=sys.argv[1:]))+'\\n')
def stopped(sig,frame):
    event('stop')
    sys.exit(0)
signal.signal(signal.SIGTERM,stopped)
signal.signal(signal.SIGHUP,lambda sig,frame:event('reload'))
event('start')
while True:
    if select.select([0],[],[],0.1)[0] and not os.read(0,128):
        event('eof');break
'''


@pytest.fixture(scope='module')
def manager_tools(tmp_path_factory):
    root=tmp_path_factory.mktemp('manager-native')
    def executable(name,code):
        path=root/name
        path.write_text(f'#!{sys.executable}\n'+code)
        path.chmod(0o755)
        return path
    executable('roles',CHILD)
    executable('acp','''
import os,sys,time
from pathlib import Path
root=Path(os.environ['TC_TEST_ROOT'])
key=sys.argv[-1]
if key=='syNm' and (root/'bad-name').exists():sys.exit(1)
if key=='MaSt':
    if (root/'slow-mast').exists():time.sleep(60)
    if (root/'bad-mast').exists():sys.exit(1)
    print((root/'inventory').read_text());sys.exit(0)
if sys.argv[1:3]==['rpc','diskd.useVolume']:sys.exit(0)
print({'syNm':'Capsule','syAP':'116','syAM':'TimeCapsule6,116','syPW':'password'}[key])
''')
    executable('ps',"import os\nfrom pathlib import Path\nprint('2 1 2 S diskd /sbin/diskd -i lo0 -d local.')\np=Path(os.environ['TC_TEST_ROOT'])/'external-processes'\nif p.exists():print(p.read_text())\n")
    executable('fstat','''
import os,sys
from pathlib import Path
if not (Path(os.environ['TC_TEST_ROOT'])/'no-listener').exists():
    os.kill(int(sys.argv[-1]),0)
    print('root smbd 1 3* internet stream tcp 192.0.2.1:445')
    print('root smbd 1 4* internet6 stream tcp [fe80::1%bridge0]:445')
''')
    binary=compile_native('service',root/'manager',flags=[
        f'-DTC_SERVICE_BIN="{root}/roles"',f'-DTC_RAM_ROOT="{root}/ram"',
        f'-DTC_FLASH_CONFIG_PATH="{root}/config"',f'-DTC_VOLUMES_ROOT="{root}"',
        f'-DTC_ACP_PATH="{root}/acp"',f'-DTC_PS_PATH="{root}/ps"',f'-DTC_FSTAT_PATH="{root}/fstat"',
    ])
    return root,binary


@pytest.fixture
def manager(manager_tools):
    root,binary=manager_tools
    for path in ('ram','dk2','dk3'):
        shutil.rmtree(root/path,ignore_errors=True)
    for name in ('bad-mast','bad-name','slow-mast','no-listener','external-processes'):
        (root/name).unlink(missing_ok=True)
    (root/'ram/var').mkdir(parents=True)
    (root/'dk2/.samba4/private').mkdir(parents=True)
    (root/'dk3').mkdir()
    image=root/'dk2/.samba4/smbd'
    image.write_text(f'#!{sys.executable}\n'+CHILD);image.chmod(0o755)
    (root/'mounts').write_text(f'{root}/dk2 dk2 1\n{root}/dk3 dk3 1\n')
    (root/'facts').write_text(NAT_OK)
    (root/'config').write_text('TELEMETRY=0\n')
    (root/'events').write_text('')
    volumes=[dict(deviceName='sd0',builtin=True,partitions=[dict(deviceName='dk2',name='Data',format='hfs',users=1,
                  uuid='11111111-1111-1111-1111-111111111111')]),
             dict(deviceName='sd1',partitions=[dict(deviceName='dk3',name='USB',format='hfs',users=1,
                  uuid='22222222-2222-2222-2222-222222222222')])]
    def inventory(disks): (root/'inventory').write_bytes(plistlib.dumps(disks))
    inventory(volumes)
    process=None
    def start():
        nonlocal process
        log=(root/'stderr').open('w')
        process=subprocess.Popen([str(binary),'manager','--facts-file',str(root/'facts')],
            stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True,
            env={**os.environ,'TC_TEST_ROOT':str(root),'TC_TEST_MOUNTS':str(root/'mounts')})
        log.close()
        return process
    def events():
        return [json.loads(line) for line in (root/'events').read_text().splitlines() if line]
    def wait(predicate,timeout=15):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            values=events()
            if predicate(values):return values
            if process and process.poll() is not None:
                pytest.fail(f'manager exited {process.returncode}: {(root/"stderr").read_text()}')
            time.sleep(.05)
        pytest.fail(f'manager deadline: {events()}\n{(root/"stderr").read_text()}\n{(root/"ram/var/runtime.log").read_text() if (root/"ram/var/runtime.log").exists() else ""}')
    yield root,start,events,wait,inventory,volumes
    if process and process.poll() is None:
        process.terminate()
        try:process.wait(timeout=15)
        except subprocess.TimeoutExpired:process.kill();process.wait();pytest.fail('manager did not stop')
    # Cleanup only this fixture's recorded children if an assertion failed.
    for item in events():
        if item['kind']=='start':
            try:os.kill(item['pid'],signal.SIGTERM)
            except ProcessLookupError:pass


def started(role):return lambda events:any(e['kind']=='start' and e['role']==role for e in events)


def test_supervision_direct_smb_child_and_applied_discovery(manager):
    root,start,events,wait,_,_=manager
    process=start()
    values=wait(started('smbd'))
    smb=next(e for e in values if e['kind']=='start' and e['role']=='smbd')
    assert smb['ppid']==process.pid and smb['group']==smb['pid']
    assert smb['args'][:2]==['-F','--no-process-group']
    values=wait(lambda rows:any(e['role']=='discovery' and '--adisk-share' in e['args'] for e in rows))
    assert (root/'ram/etc/smb.conf').is_file()
    assert '[Data]' in (root/'ram/etc/smb.conf').read_text()
    assert not any(e['role']=='telemetry' for e in values)
    os.kill(smb['pid'],signal.SIGKILL)
    wait(lambda rows:len([e for e in rows if e['role']=='smbd' and e['kind']=='start'])==2)
    process.terminate();assert process.wait(timeout=10)==0
    for item in events():
        if item['kind']=='start':
            with pytest.raises(ProcessLookupError):os.kill(item['pid'],0)


def test_shutdown_remains_responsive_during_slow_mast(manager):
    root,start,_,_,_,_=manager
    (root/'slow-mast').touch()
    process=start();time.sleep(.5)
    before=time.monotonic();process.terminate()
    assert process.wait(timeout=8)==0
    assert time.monotonic()-before<8


def test_failed_stage_never_advertises_desired_shares_and_retries(manager):
    root,start,_,wait,_,_=manager
    # A valid payload with an unusable logs path fails after volume selection.
    (root/'dk2/.samba4/logs').write_text('collision')
    process=start()
    wait(lambda rows:any(e['role']=='discovery' and '--diskless' in e['args'] for e in rows))
    time.sleep(1)
    assert not (root/'ram/etc/smb.conf').exists()
    (root/'dk2/.samba4/logs').unlink()
    process.send_signal(signal.SIGHUP)
    wait(started('smbd'),20)
    wait(lambda rows:any(e['role']=='discovery' and '--adisk-share' in e['args'] for e in rows))


def test_payload_removal_stops_samba_even_with_other_disk_available(manager):
    root,start,_,wait,inventory,volumes=manager
    process=start();wait(started('smbd'))
    inventory(volumes[1:]);(root/'mounts').write_text(f'{root}/dk3 dk3 1\n')
    process.send_signal(signal.SIGHUP)
    wait(lambda rows:any(e['role']=='smbd' and e['kind']=='stop' for e in rows),20)
    # Keep a data-only disk present throughout; RAM's old smbd image is not
    # sufficient authority to resume without a valid payload, by user choice.
    inventory(volumes);(root/'mounts').write_text(f'{root}/dk2 dk2 1\n{root}/dk3 dk3 1\n')
    process.send_signal(signal.SIGHUP)
    wait(lambda rows:len([e for e in rows if e['role']=='smbd' and e['kind']=='start'])==2,20)


def test_data_disk_removal_reloads_without_restarting_unaffected_samba(manager):
    root,start,events,wait,inventory,volumes=manager
    process=start();initial=wait(started('smbd'))
    pid=next(e['pid'] for e in initial if e['role']=='smbd' and e['kind']=='start')
    inventory(volumes[:1]);(root/'mounts').write_text(f'{root}/dk2 dk2 1\n')
    process.send_signal(signal.SIGHUP)
    wait(lambda rows:any(e['role']=='smbd' and e['kind']=='reload' for e in rows),20)
    assert '[USB]' not in (root/'ram/etc/smb.conf').read_text()
    assert [e['pid'] for e in events() if e['role']=='smbd' and e['kind']=='start']==[pid]
    os.kill(pid,0)


def test_failed_payload_switch_then_reversion_recopies_ram_image(manager):
    root,start,events,wait,inventory,volumes=manager
    process=start();wait(started('smbd'))
    external=root/'dk3/.samba4'
    (external/'private').mkdir(parents=True)
    shutil.copy2(root/'dk2/.samba4/smbd',external/'smbd')
    (external/'logs').write_text('stage collision')
    inventory(volumes[1:]);(root/'mounts').write_text(f'{root}/dk3 dk3 1\n')
    process.send_signal(signal.SIGHUP)
    wait(lambda rows:any(e['role']=='smbd' and e['kind']=='stop' for e in rows),20)
    deadline=time.monotonic()+10
    while (root/'ram/sbin/smbd').exists() and time.monotonic()<deadline:time.sleep(.05)
    assert not (root/'ram/sbin/smbd').exists()
    # Desired state reverts to the old applied config, whose image was already
    # removed. Comparing only desired/applied config would skip the required copy.
    inventory(volumes);(root/'mounts').write_text(f'{root}/dk2 dk2 1\n{root}/dk3 dk3 1\n')
    process.send_signal(signal.SIGHUP)
    wait(lambda rows:len([e for e in rows if e['role']=='smbd' and e['kind']=='start'])==2,25)
    assert (root/'ram/sbin/smbd').read_bytes()==(root/'dk2/.samba4/smbd').read_bytes()


def test_unavailable_mast_retains_service_and_singleton_refuses_duplicate(manager,manager_tools):
    root,start,events,wait,_,_=manager
    process=start();wait(started('smbd'))
    (root/'bad-mast').touch();process.send_signal(signal.SIGHUP)
    time.sleep(1)
    assert not any(e['role']=='smbd' and e['kind']=='stop' for e in events())
    duplicate=subprocess.run([str(manager_tools[1]),'manager'],capture_output=True,timeout=5)
    assert duplicate.returncode==1
    assert process.poll() is None


def test_telemetry_preference_does_not_reload_samba(manager):
    root,start,events,wait,_,_=manager
    process=start();wait(started('smbd'))
    (root/'config').write_text('TELEMETRY=1\n');process.send_signal(signal.SIGHUP)
    wait(started('telemetry'))
    assert not any(e['role']=='smbd' and e['kind'] in ('reload','stop') for e in events())
    (root/'config').write_text('TELEMETRY=0\n');process.send_signal(signal.SIGHUP)
    wait(lambda rows:any(e['role']=='telemetry' and e['kind']=='stop' for e in rows))


def test_old_telemetry_draining_does_not_block_samba_start(manager):
    root,start,events,wait,_,_=manager
    child=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print("ready",flush=True); time.sleep(30)'],
                            stdout=subprocess.PIPE,text=True,start_new_session=True)
    assert child.stdout.readline().strip()=='ready'
    try:
        (root/'external-processes').write_text(f'{child.pid} 1 {child.pid} S telemetry /old/telemetry --daemon\n')
        (root/'config').write_text('TELEMETRY=1\n')
        start();wait(started('smbd'))
        assert child.poll() is None
        assert not any(e['role']=='telemetry' for e in events())
    finally:child.kill();child.wait()


def test_failed_name_read_keeps_last_identity_without_renaming_samba(manager):
    root,start,events,wait,_,_=manager
    process=start();wait(started('smbd'))
    before=(root/'ram/etc/smb.conf').read_bytes()
    (root/'bad-name').touch();(root/'config').write_text('TELEMETRY=1\n')
    process.send_signal(signal.SIGHUP)
    # Telemetry starting proves the same settings job completed, avoiding a
    # timing assertion that could pass before the failed name read was handled.
    wait(started('telemetry'))
    assert (root/'ram/etc/smb.conf').read_bytes()==before
    assert not any(e['role']=='smbd' and e['kind'] in ('reload','stop') for e in events())
