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

import pytest
from tests.native.build import compile_service


CHILD = '''
import json,os,signal,sys,time,select
from pathlib import Path
role=sys.argv[1] if Path(sys.argv[0]).name=='roles' else Path(sys.argv[0]).name
log=Path(os.environ['TC_TEST_ROOT'])/'events'
def event(kind):
    with log.open('a') as f:f.write(json.dumps(dict(kind=kind,role=role,pid=os.getpid(),ppid=os.getppid(),group=os.getpgrp(),args=sys.argv[1:]))+'\\n')
def stopped(sig,frame):
    event('stop')
    sys.exit(0)
signal.signal(signal.SIGTERM,stopped)
signal.signal(signal.SIGHUP,lambda sig,frame:event('reload'))
event('start')
if role=='diskd' and (Path(os.environ['TC_TEST_ROOT'])/'diskd-fail').exists():sys.exit(7)
while True:
    if role=='diskd':time.sleep(.1);continue
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
    executable('diskd',CHILD)
    executable('atactl','''
import os,sys,json
from pathlib import Path
with (Path(os.environ['TC_TEST_ROOT'])/'events').open('a') as out:
    out.write(json.dumps(dict(kind='command',role='ata',args=sys.argv[1:]))+'\\n')
''')
    executable('acp','''
import os,sys,time
from pathlib import Path
root=Path(os.environ['TC_TEST_ROOT'])
key=sys.argv[-1]
if (root/'record-acp').exists():
    import json
    with (root/'events').open('a') as out:
        out.write(json.dumps(dict(kind='command',role='acp',pid=os.getpid(),ppid=os.getppid(),group=os.getpgrp(),key=key))+'\\n')
if key=='syNm' and (root/'bad-name').exists():sys.exit(1)
if key=='syPW' and (root/'bad-auth').exists():sys.exit(1)
if key=='MaSt':
    if (root/'slow-mast').exists():time.sleep(60)
    if (root/'bad-mast').exists():sys.exit(1)
    print((root/'inventory').read_text());sys.exit(0)
if sys.argv[1:3]==['rpc','diskd.useVolume']:
    if (root/'hold-activation').exists():
        import json
        def event(kind):
            with (root/'events').open('a') as out:
                out.write(json.dumps(dict(kind=kind,role='activation',pid=os.getpid()))+'\\n')
        event('blocked')
        while (root/'hold-activation').exists():time.sleep(.05)
        event('released')
    failed=root/'fail-claim'
    sys.exit(1 if failed.exists() and failed.read_text() in sys.argv[-1] else 0)
print({'syNm':(root/'name').read_text() if (root/'name').exists() else 'Capsule','syAP':'116','syAM':'TimeCapsule6,116','syPW':'password'}[key])
''')
    executable('ps','''
import os,json
from pathlib import Path
root=Path(os.environ['TC_TEST_ROOT'])
if (root/'record-ps').exists():
    with (root/'events').open('a') as out:out.write(json.dumps(dict(kind='command',role='ps'))+'\\n')
if not (root/'diskd-absent').exists():print('2 1 2 S diskd /sbin/diskd -i lo0 -d local.')
else:
    for line in (root/'events').read_text().splitlines():
        row=json.loads(line)
        if row['role']=='diskd' and row['kind']=='start':
            try:os.kill(row['pid'],0)
            except ProcessLookupError:continue
            print(f"{row['pid']} {row['ppid']} {row['group']} S diskd /sbin/diskd -i lo0 -d local.")
p=root/'external-processes'
if p.exists():
    for row in p.read_text().splitlines():
        try:os.kill(int(row.split()[0]),0)
        except ProcessLookupError:continue
        print(row)
''')
    executable('fstat','''
import os,sys
from pathlib import Path
if not (Path(os.environ['TC_TEST_ROOT'])/'no-listener').exists():
    os.kill(int(sys.argv[-1]),0)
    print('root smbd 1 3* internet stream tcp deadbeef *:445')
    print('root smbd 1 4* internet6 stream tcp deadbeef *:445')
    print('root rsync 1 4* internet stream tcp 192.0.2.1:873')
''')
    binary=compile_service(root/'manager',flags=[
        f'-DTC_SERVICE_BIN="{root}/roles"',f'-DTC_RAM_ROOT="{root}/ram"',f'-DTC_HOSTS_PATH="{root}/hosts"',
        f'-DTC_FLASH_CONFIG_PATH="{root}/config"',f'-DTC_VOLUMES_ROOT="{root}"',
        f'-DTC_ACP_PATH="{root}/acp"',f'-DTC_DISKD_PATH="{root}/diskd"',f'-DTC_ATACTL_PATH="{root}/atactl"',f'-DTC_PS_PATH="{root}/ps"',f'-DTC_FSTAT_PATH="{root}/fstat"',
    ])
    return root,binary


@pytest.fixture
def manager(manager_tools):
    root,binary=manager_tools
    for path in ('ram','dk2','dk3'):
        shutil.rmtree(root/path,ignore_errors=True)
    for name in ('bad-mast','bad-name','bad-auth','name','slow-mast','no-listener','external-processes','diskd-absent','diskd-fail','record-acp','fail-claim','record-ps','hold-activation'):
        (root/name).unlink(missing_ok=True)
    (root/'ram/var').mkdir(parents=True)
    (root/'dk2/.samba4/private').mkdir(parents=True)
    (root/'dk3').mkdir()
    image=root/'dk2/.samba4/smbd'
    image.write_text(f'#!{sys.executable}\n'+CHILD);image.chmod(0o755)
    (root/'mounts').write_text(f'{root}/dk2 dk2 1\n{root}/dk3 dk3 1\n')
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
        process=subprocess.Popen([str(binary),'manager'],
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
    # The image is removed by the next stage job, after another storage job.
    deadline=time.monotonic()+20
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


def test_diskless_discovery_does_not_wait_for_authentication(manager):
    root,start,events,wait,inventory,_=manager
    inventory([]);(root/'bad-auth').touch()
    start()
    wait(lambda rows:any(e['role']=='discovery' and '--diskless' in e['args'] for e in rows))
    assert not any(e['role']=='smbd' for e in events())


def test_server_string_change_keeps_existing_discovery_generation(manager):
    root,start,events,wait,_,_=manager
    (root/'name').write_text('CapsuleLongName First')
    process=start()
    values=wait(lambda rows:any(e['role']=='discovery' and '--adisk-share' in e['args'] for e in rows))
    before=[e['pid'] for e in values if e['role']=='discovery' and e['kind']=='start']
    (root/'name').write_text('CapsuleLongName Second');process.send_signal(signal.SIGHUP)
    wait(lambda rows:any(e['role']=='smbd' and e['kind']=='reload' for e in rows))
    assert [e['pid'] for e in events() if e['role']=='discovery' and e['kind']=='start']==before
    assert 'CapsuleLongName Second' in (root/'ram/etc/smb.conf').read_text()


def test_rsync_is_owned_then_drained_before_its_ram_files_are_removed(manager):
    root,start,_,wait,_,_=manager
    shutil.copy2(root/'dk2/.samba4/smbd',root/'dk2/.samba4/rsync')
    (root/'dk2/.samba4/rsyncd.conf').write_text('[Data]\npath = /old/ShareRoot\n')
    (root/'config').write_text('TELEMETRY=0\nRSYNC_ENABLED=1\n')
    process=start();values=wait(started('rsync'))
    rsync=next(e for e in values if e['role']=='rsync' and e['kind']=='start')
    assert rsync['ppid']==process.pid and rsync['group']==rsync['pid']
    assert rsync['args'][:2]==['--daemon','--no-detach']
    (root/'config').write_text('TELEMETRY=0\nRSYNC_ENABLED=0\n');process.send_signal(signal.SIGHUP)
    wait(lambda rows:any(e['role']=='rsync' and e['kind']=='stop' for e in rows)
         and not (root/'ram/sbin/rsync').exists() and not (root/'ram/etc/rsyncd.conf').exists())
    assert not (root/'ram/etc/rsyncd.conf').exists()
    with pytest.raises(ProcessLookupError):os.kill(rsync['pid'],0)


def test_missing_diskd_is_started_on_loopback_and_survives_manager_stop(manager):
    root,start,_,wait,_,_=manager
    (root/'diskd-absent').touch()
    process=start();values=wait(started('diskd'))
    diskd=next(e for e in values if e['role']=='diskd' and e['kind']=='start')
    assert diskd['args']==['-i','lo0','-d','local.']
    process.terminate();assert process.wait(timeout=10)==0
    # diskd is Apple's storage owner. Stopping our manager must not take it
    # down with Samba/discovery; it is adopted by init until the next boot.
    os.kill(diskd['pid'],0)


def test_failed_diskd_restart_is_backed_off_while_discovery_still_runs(manager):
    root,start,events,wait,_,_=manager
    (root/'diskd-absent').touch();(root/'diskd-fail').touch()
    start();wait(started('discovery'))
    time.sleep(1)
    assert len([e for e in events() if e['role']=='diskd' and e['kind']=='start']) == 1


def test_discovery_receives_exact_applied_names_devices_and_uuids_as_argv(manager):
    import configparser
    root,start,_,wait,inventory,volumes=manager
    name="James's café $(touch PWNED); Backup"
    for disk in volumes:disk['partitions'][0]['name']=name
    inventory(volumes);start()
    values=wait(lambda rows:any(e['role']=='discovery' and '--adisk-share' in e['args'] for e in rows))
    launched=next(e for e in values if e['role']=='discovery' and '--adisk-share' in e['args'])
    args=launched['args'];rows=[]
    for i,arg in enumerate(args):
        if arg=='--adisk-share':rows.append(args[i+1:i+5])
    conf=configparser.ConfigParser(interpolation=None,delimiters=('=',))
    conf.read(root/'ram/etc/smb.conf')
    assert [row[0] for row in rows]==conf.sections()[1:]
    assert [row[1] for row in rows]==['dk2','dk3']
    assert [row[2] for row in rows]==[disk['partitions'][0]['uuid'] for disk in volumes]
    assert all(row[3]=='0x82' for row in rows)
    assert not (root/'PWNED').exists()


@pytest.mark.parametrize('apple_roles', [('wcifsfs',), ('wcifsfs', 'wcifsnd')])
def test_native_cifs_reappearance_keeps_discovery_and_samba(manager, apple_roles):
    root,start,events,wait,_,_=manager
    process=start()
    values=wait(lambda rows:any(e['role']=='discovery' and '--adisk-share' in e['args'] for e in rows))
    before=len([e for e in values if e['role']=='discovery' and e['kind']=='start'])
    stopped=len([e for e in values if e['role']=='discovery' and e['kind']=='stop'])
    apple=[subprocess.Popen([sys.executable,'-c','import time; print("ready",flush=True); time.sleep(30)'],
                            stdout=subprocess.PIPE,text=True,start_new_session=True) for _ in apple_roles]
    try:
        for child in apple:assert child.stdout.readline().strip()=='ready'
        (root/'external-processes').write_text(''.join(
            f'{child.pid} 1 {child.pid} S {role} /sbin/{role}\n' for child,role in zip(apple,apple_roles)))
        process.send_signal(signal.SIGHUP)
        wait(lambda rows:all(child.poll() is not None for child in apple))
        assert len([e for e in events() if e['role']=='discovery' and e['kind']=='start'])==before
        assert len([e for e in events() if e['role']=='discovery' and e['kind']=='stop'])==stopped
        assert len([e for e in events() if e['role']=='smbd' and e['kind']=='start'])==1
    finally:
        for child in apple:
            if child.poll() is None:child.kill()
            child.wait()


@pytest.mark.parametrize('owned', [False, True])
def test_native_nbns_audit_distinguishes_foreign_and_owned_children(manager, owned):
    root,start,events,wait,_,_=manager
    process=start()
    values=wait(lambda rows:any(e['role']=='discovery' and '--adisk-share' in e['args'] for e in rows))
    controller=[e for e in values if e['role']=='discovery' and e['kind']=='start'][-1]
    before=len([e for e in values if e['role']=='discovery' and e['kind']=='start'])
    stopped=len([e for e in values if e['role']=='discovery' and e['kind']=='stop'])
    native=subprocess.Popen([sys.executable,'-c','import time; print("ready",flush=True); time.sleep(60)'],
                            stdout=subprocess.PIPE,text=True,start_new_session=True)
    assert native.stdout.readline().strip()=='ready'
    try:
        # NetBSD 6 boot observation: ACPd spawned wcifsnd after discovery,
        # without a remaining live wcifsfs. A live controller alone must not
        # protect that foreign daemon, but its own child must remain untouched.
        parent=controller['pid'] if owned else 1
        (root/'external-processes').write_text(
            f"{controller['pid']} {process.pid} {controller['group']} S service service: role=discovery nbns=ready\n"
            f"{native.pid} {parent} {native.pid} S wcifsnd /sbin/wcifsnd\n")
        (root/'record-ps').touch()
        if owned:
            # A second audit can only start once the first result was applied.
            for count in (1, 2):
                process.send_signal(signal.SIGHUP)
                wait(lambda rows:sum(e['role']=='ps' for e in rows)>=count)
            assert native.poll() is None
            assert len([e for e in events() if e['role']=='discovery' and e['kind']=='start'])==before
        else:
            process.send_signal(signal.SIGHUP)
            wait(lambda rows:native.poll() is not None)
        assert len([e for e in events() if e['role']=='discovery' and e['kind']=='start'])==before
        assert len([e for e in events() if e['role']=='discovery' and e['kind']=='stop'])==stopped
        assert len([e for e in events() if e['role']=='smbd' and e['kind']=='start'])==1
    finally:
        if native.poll() is None:native.kill()
        native.wait()


def test_term_resistant_foreign_wcifsnd_does_not_reset_discovery(manager):
    root,start,events,wait,_,_=manager
    process=start()
    values=wait(lambda rows:any(e['role']=='discovery' and '--adisk-share' in e['args'] for e in rows))
    starts=len([e for e in values if e['role']=='discovery' and e['kind']=='start'])
    stops=len([e for e in values if e['role']=='discovery' and e['kind']=='stop'])
    native=subprocess.Popen([sys.executable,'-c',
                             'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print("ready",flush=True); time.sleep(60)'],
                            stdout=subprocess.PIPE,text=True,start_new_session=True)
    assert native.stdout.readline().strip()=='ready'
    try:
        (root/'external-processes').write_text(f'{native.pid} 1 {native.pid} S wcifsnd /sbin/wcifsnd\n')
        (root/'record-ps').touch()
        process.send_signal(signal.SIGHUP)
        wait(lambda rows:sum(e['role']=='ps' for e in rows)>=2)
        assert native.poll() is None
        assert len([e for e in events() if e['role']=='discovery' and e['kind']=='start'])==starts
        # SIGKILL follows 10 s after first sighting, via ~1 s audits that
        # can each run for seconds on a loaded host.
        wait(lambda rows:native.poll() is not None,timeout=25)
        assert len([e for e in events() if e['role']=='discovery' and e['kind']=='start'])==starts
        assert len([e for e in events() if e['role']=='discovery' and e['kind']=='stop'])==stops
        assert len([e for e in events() if e['role']=='smbd' and e['kind']=='start'])==1
    finally:
        if native.poll() is None:native.kill()
        native.wait()


def test_healthy_rechecks_do_not_restat_or_restage_disk_software(manager):
    root,start,events,wait,_,_=manager
    process=start();wait(started('smbd'))
    # Deploy owns software replacement. Reconciliation of unchanged MaSt and
    # Healthy reconciliation must not read the sleeping HDD to fingerprint executables.
    (root/'dk2/.samba4/smbd').unlink()
    (root/'config').write_text('TELEMETRY=1\n');process.send_signal(signal.SIGHUP)
    wait(started('telemetry'))
    assert len([e for e in events() if e['role']=='smbd' and e['kind']=='start'])==1
    assert not any(e['role']=='smbd' and e['kind']=='stop' for e in events())


def test_controller_death_cleans_orphaned_native_nbns_before_replacement(manager):
    root,start,events,wait,_,_=manager
    process=start()
    values=wait(lambda rows:any(e['role']=='discovery' and '--adisk-share' in e['args'] for e in rows))
    controller=next(e for e in reversed(values) if e['role']=='discovery' and e['kind']=='start')
    before=len([e for e in values if e['role']=='discovery' and e['kind']=='start'])
    orphan=subprocess.Popen([sys.executable,'-c','import time; print("ready",flush=True); time.sleep(30)'],
                            stdout=subprocess.PIPE,text=True,start_new_session=True)
    assert orphan.stdout.readline().strip()=='ready'
    try:
        os.kill(controller['pid'],signal.SIGKILL)
        (root/'external-processes').write_text(f'{orphan.pid} 1 {orphan.pid} S wcifsnd /sbin/wcifsnd\n')
        process.send_signal(signal.SIGHUP)
        wait(lambda rows:orphan.poll() is not None and len([e for e in rows if e['role']=='discovery' and e['kind']=='start'])>before)
        assert len([e for e in events() if e['role']=='smbd' and e['kind']=='start'])==1
    finally:
        if orphan.poll() is None:orphan.kill()
        orphan.wait()


@pytest.mark.parametrize('change',['recheck','hotplug'])
def test_boot_and_rechecks_never_migrate_or_consume_legacy_metadata(manager,change):
    root,start,_,wait,inventory,volumes=manager
    if change=='hotplug':inventory(volumes[:1])
    private=root/'dk2/.samba4/private'
    files={'xattr.tdb':b'pending metadata','xattr.tdb.orphaned.1':b'quarantine',
           'xattr-migration-completed.txt':b'old untrusted checkpoint'}
    for name,data in files.items():(private/name).write_bytes(data)
    helper=root/'dk2/.samba4/xattr-hfs-migrate'
    helper.write_text(f'#!{sys.executable}\nfrom pathlib import Path\nPath({str(root/"MIGRATED")!r}).touch()\n')
    helper.chmod(0o755)
    process=start();wait(started('smbd'))
    (root/'config').write_text('TELEMETRY=1\n');process.send_signal(signal.SIGHUP)
    wait(started('telemetry'))
    if change=='hotplug':
        inventory(volumes);process.send_signal(signal.SIGHUP)
        wait(lambda rows:any(e['role']=='smbd' and e['kind']=='reload' for e in rows))
    assert not (root/'MIGRATED').exists()
    assert {name:(private/name).read_bytes() for name in files}==files


def test_ata_tuning_runs_at_start_and_preference_change_not_healthy_rechecks(manager):
    root,start,events,wait,inventory,volumes=manager
    volumes[0]['deviceName']='wd0';inventory(volumes)
    process=start();wait(started('smbd'))
    assert [e['args'] for e in events() if e['role']=='ata']==[['/dev/wd0','setidle','300']]
    (root/'config').write_text('TELEMETRY=1\n');process.send_signal(signal.SIGHUP)
    wait(started('telemetry'))
    assert len([e for e in events() if e['role']=='ata'])==1
    (root/'config').write_text('TELEMETRY=1\nATA_IDLE_SECONDS=900\nATA_STANDBY=1800\n')
    process.send_signal(signal.SIGHUP)
    wait(lambda rows:len([e for e in rows if e['role']=='ata'])==3)
    assert [e['args'] for e in events() if e['role']=='ata'][-2:]==[
        ['/dev/wd0','setidle','900'],['/dev/wd0','setstandby','1800']]


def test_manager_death_cancels_inventory_job_and_its_acp(manager):
    root,start,events,wait,_,_=manager
    (root/'record-acp').touch();(root/'slow-mast').touch()
    process=start()
    rows=wait(lambda rows:any(e['role']=='acp' and e.get('key')=='MaSt' for e in rows))
    acp=next(e for e in rows if e['role']=='acp' and e.get('key')=='MaSt')
    assert acp['group']==acp['ppid'] and acp['group']!=process.pid
    process.kill();process.wait(timeout=5)
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        try:os.kill(acp['pid'],0)
        except ProcessLookupError:break
        time.sleep(.05)
    else:pytest.fail('ACP escaped the dead manager inventory job')
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        # Darwin can return EPERM for an orphaned, zombie-only group after its
        # session leader dies. Assert live members, not that kernel artifact.
        rows=subprocess.check_output(['ps','-axo','pgid=,stat='],text=True).splitlines()
        if not any(len(parts:=row.split())==2 and parts[0]==str(acp['group']) and not parts[1].startswith('Z') for row in rows):break
        time.sleep(.05)
    else:pytest.fail('inventory group still has live members after parent EOF')


def wait_storage_failure(root, stage):
    log=root/'ram/var/runtime.log'
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        if log.exists() and ('stage='+stage) in log.read_text():
            time.sleep(.15) # Let the captured partial result reach the manager.
            return
        time.sleep(.02)
    pytest.fail('storage failure not observed: '+(log.read_text() if log.exists() else 'no log'))


@pytest.mark.parametrize('failure',['claim','guard','share','marker','executable','private','rsync'])
def test_preparation_recovers_on_unchanged_inventory(manager,failure):
    root,start,events,wait,_,_=manager
    home=root/'dk2/.samba4'
    stage='payload inspection'
    if failure=='claim':
        block=root/'fail-claim';block.write_text('dk2');repair=block.unlink;stage='activate'
    elif failure=='guard':
        disk=root/'dk2';saved=root/'saved-disk';disk.rename(saved);disk.symlink_to(saved,target_is_directory=True)
        def repair():disk.unlink();saved.rename(disk)
        stage='root guard'
    elif failure=='share':
        block=root/'dk2/ShareRoot';block.write_text('collision');repair=block.unlink;stage='share preparation'
    elif failure=='marker':
        path=root/'dk2/ShareRoot';path.mkdir();block=path/'.com.apple.timemachine.supported';block.symlink_to(home/'smbd')
        repair=block.unlink;stage='share preparation'
    elif failure=='executable':
        block=home/'smbd';block.chmod(0o600);repair=lambda:block.chmod(0o755)
    elif failure=='private':
        block=home/'private';block.rename(home/'private.saved');repair=lambda:(home/'private.saved').rename(block)
    else:
        (root/'config').write_text('TELEMETRY=0\nRSYNC_ENABLED=1\n')
        shutil.copy2(home/'smbd',home/'rsync')
        repair=lambda:(home/'rsyncd.conf').write_text('port = 873\npath = /old/ShareRoot\n')
    process=start()
    wait_storage_failure(root,stage)
    assert not started('smbd')(events())
    repair()
    # No HUP/topology/mount/user-count change: the explicit retry must recover.
    # Automatic storage retries back off 5 s then 15 s (storage/settle.c).
    wait(started('smbd'),25)
    wait(lambda rows:any(e['role']=='discovery' and '--adisk-share' in e['args'] for e in rows))
    assert '[Data]' in (root/'ram/etc/smb.conf').read_text()
    assert process.poll() is None


def test_partial_retry_preserves_healthy_payload_and_does_not_inspect_it(manager):
    root,start,events,wait,_,_=manager
    block=root/'dk3/.com.apple.timemachine.supported';block.symlink_to(root/'dk2/.samba4/smbd')
    process=start();wait(started('smbd'))
    wait_storage_failure(root,'share preparation')
    # If retry touches the healthy payload, this would stop Samba or recopy it.
    (root/'dk2/.samba4/smbd').unlink()
    block.unlink()
    wait(lambda rows:any(e['role']=='smbd' and e['kind']=='reload' for e in rows),25)
    assert '[USB]' in (root/'ram/etc/smb.conf').read_text()
    assert len([e for e in events() if e['role']=='smbd' and e['kind']=='start'])==1
    assert not any(e['role']=='smbd' and e['kind']=='stop' for e in events())
    # A healthy data-only disk stays classified absent across ordinary HUP.
    process.send_signal(signal.SIGHUP);time.sleep(.5)
    assert not any(e['role']=='smbd' and e['kind']=='stop' for e in events())


def test_internal_pending_payload_recovers_from_external_fallback(manager):
    root,start,events,wait,_,_=manager
    home=root/'dk2/.samba4';(home/'private').rename(home/'private.saved')
    external=root/'dk3/.samba4';(external/'private').mkdir(parents=True)
    shutil.copy2(home/'smbd',external/'smbd')
    start();wait(started('smbd'))
    assert str(external) in (root/'ram/etc/smb.conf').read_text()
    (home/'private.saved').rename(home/'private')
    wait(lambda rows:len([e for e in rows if e['role']=='smbd' and e['kind']=='start'])==2,15)
    assert str(home) in (root/'ram/etc/smb.conf').read_text()


@pytest.mark.parametrize('debug_key',[None,'SMBD_DEBUG_LOGGING','MDNS_DEBUG_LOGGING'])
def test_launch_trims_actual_payload_logs_and_preserves_debug(manager,debug_key):
    root,start,events,wait,_,_=manager
    debug = debug_key is not None
    (root/'config').write_text('TELEMETRY=0\n'+(debug_key+'=1\n' if debug else ''))
    logs=root/'dk2/.samba4/logs';logs.mkdir()
    paths=[logs/'discovery.log',logs/'smbd-console.log']
    content=b'old data\n'*6000+b'tail data\n'*2000
    for path in paths:path.write_bytes(content)
    inodes=[p.stat().st_ino for p in paths]
    process=start()
    rows=wait(lambda rows:any(e['role']=='discovery' and '--adisk-share' in e['args'] for e in rows))
    expected=content if debug else content[-16384:]
    assert [p.read_bytes() for p in paths]==[expected,expected]
    assert [p.stat().st_ino for p in paths]==inodes
    # Periodic/HUP audits may touch RAM logs, never these healthy HDD paths.
    for path in paths:path.write_bytes(content)
    (root/'ram/var/discovery.log').write_bytes(content)
    process.send_signal(signal.SIGHUP)
    # The trim runs at the start of an audit. HUP cannot start one while an
    # earlier audit (budget 20 s) is still reading the process table.
    deadline=time.monotonic()+25
    while (root/'ram/var/discovery.log').stat().st_size>32768 and time.monotonic()<deadline:time.sleep(.05)
    assert (root/'ram/var/discovery.log').stat().st_size==16384
    assert [p.read_bytes() for p in paths]==[content,content]
    old=next(e for e in reversed(rows) if e['role']=='discovery' and e['kind']=='start')
    os.kill(old['pid'],signal.SIGKILL)
    wait(lambda rows:any(e['role']=='discovery' and e['kind']=='start' and e['pid']!=old['pid'] and '--adisk-share' in e['args'] for e in rows))
    assert paths[0].read_bytes()==expected
    assert paths[1].read_bytes()==content


def test_payload_log_symlink_is_rejected_without_touching_target(manager):
    root,start,_,wait,_,_=manager
    logs=root/'dk2/.samba4/logs';logs.mkdir()
    protected=root/'protected-log';protected.write_bytes(b'preserve'*10000)
    (logs/'smbd-console.log').symlink_to(protected)
    process=start()
    # First launch follows the MaSt, settings, storage and stage jobs.
    deadline=time.monotonic()+20
    runtime=root/'ram/var/runtime.log'
    while time.monotonic()<deadline:
        if runtime.exists() and 'log destination unavailable' in runtime.read_text():break
        time.sleep(.05)
    else:pytest.fail('unsafe console log was not rejected')
    assert protected.read_bytes()==b'preserve'*10000
    (logs/'smbd-console.log').unlink()
    process.send_signal(signal.SIGHUP)
    wait(started('smbd'))


def test_pending_payload_does_not_reclaim_unchanged_users_zero_volume(manager):
    root,start,events,wait,inventory,volumes=manager
    volumes[0]['partitions'][0]['users']=0;inventory(volumes)
    (root/'record-acp').touch()
    home=root/'dk2/.samba4';(home/'private').rename(home/'private.saved')
    process=start();wait_storage_failure(root,'payload inspection')
    time.sleep(7) # First automatic retry must inspect only the failed payload.
    claims=[e for e in events() if e['role']=='acp' and e.get('key')=='path:s:'+str(root/'dk2')]
    assert len(claims)==1
    (home/'private.saved').rename(home/'private')
    process.send_signal(signal.SIGHUP);wait(started('smbd'))


def test_unreadable_but_appendable_console_log_does_not_block_samba(manager):
    if os.geteuid()==0:pytest.skip('root bypasses the read-permission fault')
    root,start,_,wait,_,_=manager
    logs=root/'dk2/.samba4/logs';logs.mkdir()
    console=logs/'smbd-console.log';content=b'keep diagnostics\n'*4000
    console.write_bytes(content);console.chmod(0o200)
    try:
        start();wait(started('smbd'))
        assert 'unable to trim' in (root/'ram/var/runtime.log').read_text()
    finally:console.chmod(0o600)
    assert console.read_bytes()==content


@pytest.mark.parametrize('initial', [0, 1])
def test_internal_export_root_change_reloads_without_restarting(manager, initial):
    root, start, events, wait, _, _ = manager
    (root/'config').write_text(f'TELEMETRY=0\nINTERNAL_SHARE_USE_DISK_ROOT={initial}\n')
    process = start()
    rows = wait(started('smbd'))
    pid = next(e['pid'] for e in rows if e['role'] == 'smbd' and e['kind'] == 'start')
    before = (root/'ram/etc/smb.conf').read_text()
    usb = next(line for line in before.splitlines() if 'tc:volume dk3 =' in line)
    (root/'config').write_text(f'TELEMETRY=0\nINTERNAL_SHARE_USE_DISK_ROOT={1-initial}\n')
    process.send_signal(signal.SIGHUP)
    wait(lambda rows: any(e['role'] == 'smbd' and e['kind'] == 'reload' for e in rows))
    after = (root/'ram/etc/smb.conf').read_text()
    expected = str(root/'dk2') + ('/ShareRoot' if initial else '')
    assert f'path = {expected}\n' in after
    assert f'tc:volume dk2 = 11111111-1111-1111-1111-111111111111|{expected}\n' in after
    assert usb in after
    assert [e['pid'] for e in events() if e['role'] == 'smbd' and e['kind'] == 'start'] == [pid]
    assert not any(e['role'] == 'smbd' and e['kind'] == 'stop' for e in events())
