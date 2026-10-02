"""Run the actual manager with fake Apple observations and real owned children.

The fixtures replace only appliance interfaces (ACP, ps/fstat, mounted volumes,
and daemon executables). Fork/exec, groups, signals, file staging, reloads,
configuration publication, and the manager select loop remain real.
"""
import errno
import json
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import time

import pytest
from tests.native.build import compile_service


# This host build shortens the manager's waits; the device keeps the defaults.
# tests/native/unit/test_storage_settle.c pins the device settle and retry.
TIMINGS=dict(
    TC_STORAGE_SETTLE_MS=1000,  # device 5 s
    TC_STORAGE_RETRY_MS=1000,   # device 5 s; automatic retries back off 1x, 3x, then 12x
    JOB_RETRY_MS=1000,          # device 5 s
    TC_STALE_KILL_MS=4000,      # device 10 s
    # The manager samples once per loop pass, at least every second.
    TC_BUFSTALL_TRIGGER_MS=1500, # device 5 s
    TC_BUFSTALL_QUIET_MS=2500,   # device 10 s
    TC_BUFSTALL_HOLD_MS=6000,    # device 60 s
    TC_BUFSTALL_REPORT_MS=20000, # device 1 hour after delivery
    TC_BUFSTALL_REPORT_RETRY_MS=3000, # device 60 s after failure
    TC_BUFSTALL_REPORT_TIMEOUT_MS=15000, # device 180 s
)

CHILD = '''
import json,os,signal,sys,time,select
from pathlib import Path
role=sys.argv[1] if Path(sys.argv[0]).name=='roles' else Path(sys.argv[0]).name
log=Path(os.environ['TC_TEST_ROOT'])/'events'
def event(kind):
    # Events from different children need one clock origin; macOS Python 3.9's
    # time.monotonic() starts separately in each process.
    row=dict(kind=kind,role=role,pid=os.getpid(),ppid=os.getppid(),group=os.getpgrp(),args=sys.argv[1:],at=time.clock_gettime(time.CLOCK_MONOTONIC))
    if role=='smbd' and kind=='start':
        # What smbd's own-name lookups would have found when it started.
        hosts=Path(os.environ['TC_TEST_ROOT'])/'hosts'
        row['hosts']=hosts.read_text() if hosts.is_file() else None
    with log.open('a') as f:f.write(json.dumps(row)+'\\n')
def stopped(sig,frame):
    event('stop')
    sys.exit(0)
signal.signal(signal.SIGTERM,stopped)
# Like a Samba worker stuck in the kernel: ignores SIGTERM and its parent's exit.
stuck=((role=='smbd' and (Path(os.environ['TC_TEST_ROOT'])/'smbd-ignore-term').exists()) or
       (role=='telemetry' and '--report' in sys.argv and (Path(os.environ['TC_TEST_ROOT'])/'telemetry-hang').exists()))
if stuck:signal.signal(signal.SIGTERM,signal.SIG_IGN)
signal.signal(signal.SIGHUP,lambda sig,frame:event('reload'))
event('start')
if role=='telemetry' and '--report' in sys.argv and not stuck:
    root=Path(os.environ['TC_TEST_ROOT'])
    while (root/'report-hold').exists():time.sleep(.05)
    status=int((root/'report-exit').read_text()) if (root/'report-exit').exists() else 0
    event('report-done')
    sys.exit(status)
if role=='diskd' and (Path(os.environ['TC_TEST_ROOT'])/'diskd-fail').exists():sys.exit(7)
while True:
    if role=='diskd' or stuck:time.sleep(.1);continue
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
        f'-DTC_BUFWAKE_DIR="{root}/wake"',
        *(f'-D{name}={value}' for name,value in TIMINGS.items()),
    ])
    return root,binary


@pytest.fixture
def manager(manager_tools):
    root,binary=manager_tools
    for path in ('ram','dk2','dk3'):
        shutil.rmtree(root/path,ignore_errors=True)
    hosts=root/'hosts'
    if hosts.is_dir():hosts.rmdir()
    else:hosts.unlink(missing_ok=True)
    for name in ('bad-mast','bad-name','bad-auth','name','slow-mast','no-listener','external-processes','diskd-absent','diskd-fail','record-acp','fail-claim','record-ps','hold-activation',
                 'bufcache','bufcache.writes','bufcache.wakes','bufcache.tmp','bufcache.new','fork-fail','smbd-ignore-term','telemetry-hang',
                 'report-hold','report-exit'):
        (root/name).unlink(missing_ok=True)
    (root/'wake').mkdir(exist_ok=True)
    (root/'ram/var').mkdir(parents=True)
    (root/'dk2/.samba4/private').mkdir(parents=True)
    (root/'dk3').mkdir()
    image=root/'dk2/.samba4/smbd'
    image.write_text(f'#!{sys.executable}\n'+CHILD);image.chmod(0o755)
    (root/'mounts').write_text(f'{root}/dk2 dk2 1\n{root}/dk3 dk3 1\n')
    (root/'config').write_text('TELEMETRY=0\n')
    (root/'events').write_text('')
    (root/'hostname').write_text('capsule\n')
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
            env={**os.environ,'TC_TEST_ROOT':str(root),'TC_TEST_MOUNTS':str(root/'mounts'),
                 'TC_TEST_HOSTNAME':str(root/'hostname'),'TC_TEST_BUFCACHE':str(root/'bufcache'),
                 'TC_TEST_FORK_FAIL':str(root/'fork-fail')})
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


def runtime_log(root):
    # On the device the manager's own stderr is runtime.log too; here the rig
    # captures it separately from its jobs' log.
    return ''.join(path.read_text() for path in (root/'stderr',root/'ram/var/runtime.log') if path.exists())


def wait_log(root,text,timeout=15):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if text in runtime_log(root):return runtime_log(root)
        time.sleep(.05)
    pytest.fail(f'{text!r} not logged: {runtime_log(root)}')


def found_after_ms(log,name):
    match=re.search(rf'hostname found: {re.escape(name)} after (\d+) ms',log)
    assert match,log
    return int(match.group(1))


def smbd_starts(rows):return [e for e in rows if e['role']=='smbd' and e['kind']=='start']


def test_samba_waits_for_the_hostname_and_starts_once_it_is_mapped(manager):
    # ACPd sets the hostname seconds after boot and gives no notice. Discovery
    # stays up in diskless mode (AirPort Utility needs _airport) meanwhile.
    root,start,events,wait,_,_=manager
    (root/'hostname').write_text('')
    start()
    wait(lambda rows:any(e['role']=='discovery' and '--diskless' in e['args'] for e in rows))
    time.sleep(2) # Samba otherwise starts about a second after the manager.
    assert not smbd_starts(events())
    assert not (root/'hosts').exists()
    (root/'hostname').write_text('capsule\n')
    rows=wait(lambda rows:smbd_starts(rows))
    # smbd found its own name mapped from its very first lookup.
    assert smbd_starts(rows)[0]['hosts']=='127.0.0.1\tcapsule capsule.local\n'
    log=runtime_log(root)
    assert log.count('manager: waiting for the device hostname before starting Samba')==1
    # Staging waited for the name; it did not run and fail on an empty one.
    assert 'stage: could not update' not in log and 'staging failed' not in log
    assert found_after_ms(log,'capsule')>=2000
    assert f'stage: mapped capsule in {root}/hosts' in log
    wait(lambda rows:any(e['role']=='discovery' and '--adisk-share' in e['args'] for e in rows))


def test_hostname_set_at_start_is_found_without_waiting(manager):
    root,start,events,wait,_,_=manager
    start()
    rows=wait(lambda rows:smbd_starts(rows))
    assert smbd_starts(rows)[0]['hosts']=='127.0.0.1\tcapsule capsule.local\n'
    log=runtime_log(root)
    assert found_after_ms(log,'capsule')<1500
    assert 'waiting for the device hostname' not in log


def test_existing_mapping_is_left_as_it_is(manager):
    root,start,events,wait,_,_=manager
    apple='::1\tlocalhost localhost.\n127.0.0.1\tlocalhost capsule\n'
    (root/'hosts').write_text(apple)
    start()
    rows=wait(lambda rows:smbd_starts(rows))
    assert smbd_starts(rows)[0]['hosts']==apple
    assert 'stage: mapped' not in runtime_log(root)


def test_rename_remaps_the_hostname_and_reloads_samba_without_restarting_it(manager):
    root,start,events,wait,_,_=manager
    start()
    first=smbd_starts(wait(lambda rows:smbd_starts(rows)))[0]
    (root/'hostname').write_text('renamed\n')
    wait(lambda rows:any(e['role']=='smbd' and e['kind']=='reload' for e in rows))
    assert (root/'hosts').read_text()=='127.0.0.1\trenamed renamed.local\n'
    log=runtime_log(root)
    assert 'manager: hostname changed from capsule to renamed' in log
    assert 'stage: removed the stale mapping for capsule' in log
    assert [e['pid'] for e in smbd_starts(events())]==[first['pid']]
    assert not any(e['role']=='smbd' and e['kind']=='stop' for e in events())


@pytest.mark.parametrize(('returns','logged'),[
    ('capsule','manager: hostname found again: capsule after '),
    ('other','manager: hostname changed from capsule to other'),
])
def test_losing_the_hostname_keeps_samba_running_until_a_name_returns(manager,returns,logged):
    root,start,events,wait,_,_=manager
    start()
    first=smbd_starts(wait(lambda rows:smbd_starts(rows)))[0]
    (root/'hostname').write_text('')
    wait_log(root,'manager: hostname is no longer set; Samba keeps running, new staging waits')
    time.sleep(1.5)
    assert runtime_log(root).count('hostname is no longer set')==1
    assert not any(e['role']=='smbd' and e['kind']=='stop' for e in events())
    (root/'hostname').write_text(returns+'\n')
    wait_log(root,logged)
    if returns=='other':
        wait(lambda rows:any(e['role']=='smbd' and e['kind']=='reload' for e in rows))
        assert (root/'hosts').read_text()=='127.0.0.1\tother other.local\n'
    assert [e['pid'] for e in smbd_starts(events())]==[first['pid']]


def test_invalid_hostname_is_left_unmapped_and_samba_starts(manager):
    # ACPd copies a user-set syDN; a name /etc/hosts cannot hold never becomes
    # writable, so staging logs it once per run and starts Samba anyway.
    root,start,events,wait,_,_=manager
    (root/'hostname').write_text('bad name\n')
    (root/'hosts').write_text('127.0.0.1\tlocalhost\n')
    start()
    rows=wait(lambda rows:smbd_starts(rows))
    assert smbd_starts(rows)[0]['hosts']=='127.0.0.1\tlocalhost\n'
    log=runtime_log(root)
    assert f'stage: not mapping hostname "bad name" in {root}/hosts: not a plain host name; Samba logins may stall' in log
    assert 'staging failed' not in log


def test_rename_to_an_invalid_hostname_reloads_samba_and_leaves_hosts_alone(manager):
    root,start,events,wait,_,_=manager
    start()
    first=smbd_starts(wait(lambda rows:smbd_starts(rows)))[0]
    (root/'hostname').write_text('bad name\n')
    wait(lambda rows:any(e['role']=='smbd' and e['kind']=='reload' for e in rows))
    assert (root/'hosts').read_text()=='127.0.0.1\tcapsule capsule.local\n'
    assert 'stage: not mapping hostname "bad name"' in runtime_log(root)
    assert [e['pid'] for e in smbd_starts(events())]==[first['pid']]


def test_stale_mapping_from_an_earlier_manager_this_boot_is_removed(manager):
    # /etc/hosts lives on the RAM root: only a manager earlier in this boot,
    # under another hostname, can have left one of our lines behind.
    root,start,events,wait,_,_=manager
    (root/'hosts').write_text('127.0.0.1\tlocalhost\n127.0.0.1\toldname oldname.local\n')
    start()
    rows=wait(lambda rows:smbd_starts(rows))
    assert smbd_starts(rows)[0]['hosts']=='127.0.0.1\tlocalhost\n127.0.0.1\tcapsule capsule.local\n'
    assert 'stage: removed the stale mapping for oldname' in runtime_log(root)


def wait_for_fifo_reader(path,timeout=20):
    """Return a write end of FIFO path once a reader has it open.

    Only the staging job opens the hosts file, so a reader means staging is
    running. Holding the returned end open keeps it blocked in read().
    """
    deadline=time.monotonic()+timeout
    while True:
        try:return os.open(path,os.O_WRONLY|os.O_NONBLOCK)
        except OSError as error:
            if error.errno!=errno.ENXIO:raise
        assert time.monotonic()<deadline,f'nothing opened {path}'
        time.sleep(.05)


def test_rename_while_staging_is_in_flight_restages_with_the_new_name(manager):
    root,start,events,wait,_,_=manager
    hosts=root/'hosts';os.mkfifo(hosts)
    start()
    held=[wait_for_fifo_reader(hosts)] # staging now waits for our writer to finish
    try:
        assert not smbd_starts(events())
        (root/'hostname').write_text('renamed\n')
        # The manager stops the stale run, which cannot finish while our end
        # stays open, and starts another that opens the FIFO again.
        wait_log(root,'manager: staging failed or superseded; will retry',timeout=30)
        held.append(wait_for_fifo_reader(hosts))
    finally:
        for fd in held:os.close(fd) # end of file: staging writes the real file
    rows=wait(lambda rows:smbd_starts(rows),30)
    # Nothing mapped the old name first: smbd saw only the new mapping.
    assert smbd_starts(rows)[0]['hosts']=='127.0.0.1\trenamed renamed.local\n'
    assert len(smbd_starts(events()))==1
    assert 'manager: hostname changed from capsule to renamed' in runtime_log(root)


def test_failed_mapping_holds_samba_and_staging_retries_until_it_works(manager):
    # The mapping is load-bearing: a write failure fails staging, which retries.
    root,start,events,wait,_,_=manager
    (root/'hosts').mkdir() # reading a directory fails with EISDIR
    start()
    wait_log(root,f'stage: could not update {root}/hosts for capsule: Is a directory')
    wait_log(root,'manager: staging failed or superseded; will retry')
    time.sleep(1.5)
    assert not smbd_starts(events())
    assert runtime_log(root).count('stage: could not update')>=2 # it kept retrying
    (root/'hosts').rmdir()
    rows=wait(lambda rows:smbd_starts(rows))
    assert smbd_starts(rows)[0]['hosts']=='127.0.0.1\tcapsule capsule.local\n'


def test_shutdown_remains_responsive_during_slow_mast(manager):
    root,start,_,wait,_,_=manager
    (root/'slow-mast').touch();(root/'record-acp').touch()
    process=start()
    # Stop only once the 60 s MaSt read is in flight. A fixed delay could land
    # before a slow (sanitizer, loaded) manager has installed its handlers.
    wait(lambda rows:any(e['role']=='acp' and e.get('key')=='MaSt' for e in rows))
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


def enable_rsync(root):
    shutil.copy2(root/'dk2/.samba4/smbd',root/'dk2/.samba4/rsync')
    (root/'dk2/.samba4/rsyncd.conf').write_text('[Data]\npath = /old/ShareRoot\n')
    (root/'config').write_text('TELEMETRY=0\nRSYNC_ENABLED=1\n')


def sleeper(ignore_term=False):
    code=('import signal,time\n'+('signal.signal(signal.SIGTERM,signal.SIG_IGN)\n' if ignore_term else '')+
          'print("ready",flush=True)\ntime.sleep(60)\n')
    child=subprocess.Popen([sys.executable,'-c',code],stdout=subprocess.PIPE,text=True,start_new_session=True)
    assert child.stdout.readline().strip()=='ready'
    return child


def rsync_starts(rows):return [e for e in rows if e['role']=='rsync' and e['kind']=='start']


@pytest.mark.parametrize('enabled',[False,True])
def test_rsync_clients_and_ssh_servers_are_left_running(manager,enabled):
    root,start,events,wait,_,_=manager
    if enabled:enable_rsync(root)
    # Issue #346: a client run by hand on the device, and the servers sshd
    # starts for a remote client, are named rsync too. They are the user's
    # transfers, not a stale daemon, and must not hold back the managed one.
    commands=['/mnt/Memory/samba4/sbin/rsync -rlptD --info=progress2 /Volumes/dk2/ShareRoot/ 192.168.1.248::shareroot/',
              'rsync --server -logDtpre.iLsfxCIvu . /Volumes/dk2/ShareRoot/',
              'rsync --server --daemon .']
    users=[sleeper() for _ in commands]
    try:
        (root/'external-processes').write_text(''.join(
            f'{child.pid} 1 {child.pid} S rsync {command}\n' for child,command in zip(users,commands)))
        (root/'record-ps').touch()
        process=start()
        wait(started('rsync') if enabled else started('smbd'))
        for count in (2,3):
            process.send_signal(signal.SIGHUP)
            wait(lambda rows:sum(e['role']=='ps' for e in rows)>=count)
        assert all(child.poll() is None for child in users)
        assert len(rsync_starts(events()))==(1 if enabled else 0)
        assert 'SIGKILL' not in runtime_log(root)
    finally:
        for child in users:
            if child.poll() is None:child.kill()
            child.wait()


def test_foreign_rsync_daemon_is_stopped_before_the_managed_one_starts(manager):
    root,start,events,wait,_,_=manager
    enable_rsync(root)
    foreign=sleeper()
    try:
        (root/'external-processes').write_text(
            f'{foreign.pid} 1 {foreign.pid} S rsync /mnt/Memory/samba4/sbin/rsync --daemon --no-detach\n')
        start()
        values=wait(lambda rows:foreign.poll() is not None and rsync_starts(rows))
        assert foreign.returncode==-signal.SIGTERM
        assert len(rsync_starts(values))==1
        # Routine SIGTERM cleanup is not logged.
        assert 'foreign rsync' not in runtime_log(root)
    finally:
        if foreign.poll() is None:foreign.kill()
        foreign.wait()


def test_term_resistant_rsync_daemon_is_killed_and_logged_once(manager):
    root,start,events,wait,_,_=manager
    enable_rsync(root)
    foreign=sleeper(ignore_term=True)
    try:
        (root/'external-processes').write_text(
            f'{foreign.pid} 1 {foreign.pid} S rsync /mnt/Memory/samba4/sbin/rsync --daemon --no-detach\n')
        (root/'record-ps').touch()
        start()
        log=wait_log(root,f'foreign rsync pid {foreign.pid} ',timeout=25)
        assert re.search(rf'foreign rsync pid {foreign.pid} \(group {foreign.pid}\) ignored SIGTERM for \d+ ms; sending SIGKILL',log)
        # Leave the killed child unreaped: the fake ps keeps listing it, as
        # Apple's ps would list a process stuck in the kernel. Later audits
        # resend SIGKILL without logging again, and rsync stays blocked.
        audits=sum(e['role']=='ps' for e in events())
        wait(lambda rows:sum(e['role']=='ps' for e in rows)>=audits+3)
        assert runtime_log(root).count(f'foreign rsync pid {foreign.pid} ')==1
        assert not rsync_starts(events())
        assert foreign.wait(timeout=5)==-signal.SIGKILL
        wait(started('rsync'))
        assert runtime_log(root).count('sending SIGKILL')==1
    finally:
        if foreign.poll() is None:foreign.kill()
        foreign.wait()


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
        # SIGKILL follows TC_STALE_KILL_MS after first sighting, via ~1 s
        # audits that can each run for seconds on a loaded host.
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
    # Automatic storage retries back off TC_STORAGE_RETRY_MS, then three times
    # that (storage/settle.c).
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
    # The first automatic retry must inspect only the failed payload. Each
    # failed inspection logs its stage; the next retry is 3 s after this one.
    log=root/'ram/var/runtime.log'
    deadline=time.monotonic()+15
    while log.read_text().count('stage=payload inspection')<2 and time.monotonic()<deadline:time.sleep(.05)
    assert log.read_text().count('stage=payload inspection')==2
    time.sleep(.15) # Let the captured partial result reach the manager.
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


# Buffer-cache stall recovery (kern/60584): the fixture file stands in for
# vm.bufmem* and the processes' kernel wait messages (bufstall.c).
HIWATER=40243200
APPLE_LOWATER=HIWATER>>3
RAISED_LOWATER=HIWATER-16


def kernel(root,*waits,bufmem=3000000,lowater=APPLE_LOWATER,readonly=False):
    lines=[f'bufmem {bufmem}',f'lowater {lowater}',f'hiwater {HIWATER}',*(f'wait {pid} {wmesg}' for pid,wmesg in waits)]
    # Replace atomically: the manager may read the file at any moment.
    (root/'bufcache.new').write_text('\n'.join(lines+(['readonly'] if readonly else []))+'\n')
    os.replace(root/'bufcache.new',root/'bufcache')


def lowater_writes(root):
    path=root/'bufcache.writes'
    return [int(value) for value in path.read_text().split()] if path.exists() else []


def wakes(root):
    path=root/'bufcache.wakes'
    return [int(value) for value in path.read_text().split()] if path.exists() else []


def stderr(root):
    return (root/'stderr').read_text()


def until(root,predicate,timeout=30):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if predicate():return
        time.sleep(.05)
    runtime=root/'ram/var/runtime.log'
    pytest.fail(f'deadline: writes {lowater_writes(root)} wakes {len(wakes(root))}\n{stderr(root)}\n'
                f'{runtime.read_text() if runtime.exists() else ""}')


def reports(events):
    return [e['args'][2] for e in events() if e['role']=='telemetry' and e['kind']=='start' and '--report' in e['args']]


def report_matches(events,outcome):
    return lambda rows:len(reports(events))==1 and re.fullmatch(rf'bufstall:{outcome}:\d+',reports(events)[0])


def test_buffer_stall_raises_wakes_in_process_and_restores_after_the_stall(manager):
    root,start,events,wait,_,_=manager
    (root/'config').write_text('TELEMETRY=1\n')
    kernel(root)
    start();wait(started('smbd'))
    kernel(root,(4242,'getnewbuf'),(4243,'select'))
    until(root,lambda:lowater_writes(root)==[RAISED_LOWATER])
    # Recovery writes and wakes first; the following log is a separate event.
    until(root,lambda:('buffer stall: 1 processes waiting for buffers' in stderr(root) and
                      f'raised vm.bufmem_lowater from {APPLE_LOWATER} to {RAISED_LOWATER} (bufmem 3000000, hiwater {HIWATER})'
                      in stderr(root)))
    # Woken on every sample while it stays stalled, all 128 passes each time.
    until(root,lambda:len(wakes(root))>=3)
    assert set(wakes(root))=={128}
    # No report while the stall lasts.
    assert reports(events)==[]
    kernel(root,(4243,'select'),lowater=RAISED_LOWATER)
    until(root,lambda:lowater_writes(root)==[RAISED_LOWATER,APPLE_LOWATER])
    until(root,lambda:re.search(rf'buffer stall over \(resolved, longest wait \d+ s\); restored vm.bufmem_lowater to {APPLE_LOWATER}',
                               stderr(root)))
    wait(report_matches(events,'resolved'))
    count=len(wakes(root));time.sleep(1.5)
    assert len(wakes(root))==count
    # A second episode within the hour is fixed again but not reported again.
    kernel(root,(5000,'needbuf'))
    until(root,lambda:lowater_writes(root)==[RAISED_LOWATER,APPLE_LOWATER,RAISED_LOWATER])
    kernel(root,lowater=RAISED_LOWATER)
    until(root,lambda:lowater_writes(root)==[RAISED_LOWATER,APPLE_LOWATER]*2)
    time.sleep(1)
    assert len(reports(events))==1


def test_buffer_stall_recovers_while_every_fork_fails(manager):
    root,start,events,wait,_,_=manager
    (root/'config').write_text('TELEMETRY=1\n')
    kernel(root)
    start();wait(started('smbd'))
    (root/'fork-fail').touch()
    starts=lambda:[e for e in events() if e['kind']=='start']
    before=len(starts())
    kernel(root,(4242,'getnewbuf'))
    until(root,lambda:lowater_writes(root)==[RAISED_LOWATER])
    until(root,lambda:len(wakes(root))>=2)
    kernel(root,lowater=RAISED_LOWATER)
    until(root,lambda:lowater_writes(root)==[RAISED_LOWATER,APPLE_LOWATER])
    # Detection, raise, wake and restore never started a process.
    assert len(starts())==before
    # The report waits for fork to work again, then goes out once.
    time.sleep(1.5)
    assert reports(events)==[]
    (root/'fork-fail').unlink()
    wait(report_matches(events,'resolved'))


def test_unsent_reports_are_merged_into_the_worst_outcome_and_longest_wait(manager):
    root,start,events,wait,_,_=manager
    (root/'config').write_text('TELEMETRY=1\n')
    kernel(root)
    start();wait(started('smbd'))
    (root/'fork-fail').touch()
    # A resolved episode, then a capped one, both while reports cannot start.
    kernel(root,(4242,'getnewbuf'))
    until(root,lambda:lowater_writes(root)==[RAISED_LOWATER])
    kernel(root,lowater=RAISED_LOWATER)
    until(root,lambda:'buffer stall over (resolved' in stderr(root))
    kernel(root,(5000,'getnewbuf'),bufmem=RAISED_LOWATER)
    until(root,lambda:'cannot help' in stderr(root))
    kernel(root,bufmem=RAISED_LOWATER)
    until(root,lambda:'buffer stall over (capped' in stderr(root))
    longest=max(int(n) for n in re.findall(r'longest wait (\d+) s',stderr(root)))
    (root/'fork-fail').unlink()
    wait(report_matches(events,'capped'))
    assert reports(events)==[f'bufstall:capped:{longest}']


def test_hung_report_is_killed_at_stop(manager):
    root,start,events,wait,_,_=manager
    (root/'config').write_text('TELEMETRY=1\n')
    (root/'telemetry-hang').touch()
    kernel(root)
    process=start();wait(started('smbd'))
    kernel(root,(4242,'getnewbuf'))
    until(root,lambda:lowater_writes(root)==[RAISED_LOWATER])
    kernel(root,lowater=RAISED_LOWATER)
    wait(report_matches(events,'resolved'))
    pid=next(e['pid'] for e in events() if e['role']=='telemetry' and '--report' in e['args'])
    # It ignores SIGTERM and its parent's exit; the manager kills it.
    process.terminate();process.wait(timeout=30)
    with pytest.raises(ProcessLookupError):os.kill(pid,0)


@pytest.mark.parametrize('status', [0, 1, 75])
def test_report_completion_preserves_new_episodes_and_retries_failed_snapshot(manager, status):
    root,start,events,wait,_,_=manager
    (root/'config').write_text('TELEMETRY=1\n')
    (root/'report-hold').touch()
    (root/'report-exit').write_text(str(status))
    kernel(root)
    start();wait(started('smbd'))
    # The older, worse episode stays in flight while a resolved one ends.
    kernel(root,(4242,'getnewbuf'),bufmem=RAISED_LOWATER)
    until(root,lambda:'cannot help' in stderr(root))
    kernel(root)
    wait(report_matches(events,'capped'))
    first=reports(events)[0]
    kernel(root,(5000,'getnewbuf'))
    until(root,lambda:lowater_writes(root)==[RAISED_LOWATER])
    kernel(root,lowater=RAISED_LOWATER)
    until(root,lambda:'buffer stall over (resolved' in stderr(root))
    assert len(reports(events))==1
    (root/'report-hold').unlink()
    wait(lambda rows:any(e['kind']=='report-done' for e in rows))
    (root/'report-exit').unlink()
    wait(lambda rows:len(reports(events))==2,timeout=35)
    sent=[e for e in events() if e['kind']=='start' and '--report' in e['args']]
    done=next(e for e in events() if e['kind']=='report-done')
    delay=sent[1]['at']-done['at']
    if status==0:
        assert reports(events)[1].startswith('bufstall:resolved:')
        assert delay>=TIMINGS['TC_BUFSTALL_REPORT_MS']/1000
    else:
        assert reports(events)[1].startswith('bufstall:capped:')
        assert int(reports(events)[1].split(':')[-1])>=int(first.split(':')[-1])
        assert delay>=TIMINGS['TC_BUFSTALL_REPORT_RETRY_MS']/1000
        assert delay<TIMINGS['TC_BUFSTALL_REPORT_MS']/1000


def test_timed_out_report_is_retried_without_another_episode(manager):
    root,start,events,wait,_,_=manager
    (root/'config').write_text('TELEMETRY=1\n')
    (root/'telemetry-hang').touch()
    kernel(root)
    start();wait(started('smbd'))
    kernel(root,(4242,'getnewbuf'))
    until(root,lambda:lowater_writes(root)==[RAISED_LOWATER])
    kernel(root,lowater=RAISED_LOWATER)
    wait(report_matches(events,'resolved'))
    first=next(e for e in events() if e['kind']=='start' and '--report' in e['args'])
    # Only the first child hangs. The timeout must kill it and retain its report.
    (root/'telemetry-hang').unlink()
    wait(lambda rows:len(reports(events))==2,timeout=40)
    assert reports(events)[0]==reports(events)[1]
    with pytest.raises(ProcessLookupError):os.kill(first['pid'],0)
    second=[e for e in events() if e['kind']=='start' and '--report' in e['args']][1]
    assert second['at']-first['at']>=TIMINGS['TC_BUFSTALL_REPORT_TIMEOUT_MS']/1000


def test_failed_wake_is_logged_once(manager):
    root,start,_,wait,_,_=manager
    (root/'wake').rmdir()
    kernel(root)
    start();wait(started('smbd'))
    kernel(root,(4242,'getnewbuf'))
    until(root,lambda:lowater_writes(root)==[RAISED_LOWATER])
    until(root,lambda:f'buffer-stall wake could not read {root}/wake' in stderr(root))
    time.sleep(2)
    assert stderr(root).count('buffer-stall wake could not read')==1 and wakes(root)==[]


def test_buffer_stall_that_outlasts_the_raise_is_restored_then_raised_again(manager):
    root,start,events,wait,_,_=manager
    (root/'config').write_text('TELEMETRY=1\n')
    kernel(root)
    start();wait(started('smbd'))
    kernel(root,(4242,'getnewbuf'))
    until(root,lambda:lowater_writes(root)==[RAISED_LOWATER])
    until(root,lambda:lowater_writes(root)==[RAISED_LOWATER,APPLE_LOWATER])
    until(root,lambda:'processes still waiting' in stderr(root) and 'raising again in 6 s' in stderr(root))
    # Raising again is cheap: it is retried while the stall lasts.
    until(root,lambda:lowater_writes(root)==[RAISED_LOWATER,APPLE_LOWATER,RAISED_LOWATER])
    assert reports(events)==[]
    kernel(root,lowater=RAISED_LOWATER)
    until(root,lambda:lowater_writes(root)==[RAISED_LOWATER,APPLE_LOWATER]*2)
    wait(report_matches(events,'stuck'))


def test_post_raise_stall_counts_as_stuck(manager):
    root,start,_,wait,_,_=manager
    kernel(root)
    start();wait(started('smbd'))
    kernel(root,(4242,'getnewbuf'))
    until(root,lambda:lowater_writes(root)==[RAISED_LOWATER])
    # The raise freed 4242; 5000 starts waiting afterwards and never stops.
    kernel(root,(5000,'getnewbuf'),lowater=RAISED_LOWATER)
    until(root,lambda:lowater_writes(root)==[RAISED_LOWATER,APPLE_LOWATER])
    until(root,lambda:'processes still waiting' in stderr(root))


def test_capped_stall_is_rechecked_and_raised_once_the_cache_shrinks(manager):
    root,start,events,wait,_,_=manager
    (root/'config').write_text('TELEMETRY=1\n')
    kernel(root)
    start();wait(started('smbd'))
    kernel(root,(4242,'getnewbuf'),bufmem=RAISED_LOWATER)
    until(root,lambda:'raising vm.bufmem_lowater cannot help; checking again every 6 s' in stderr(root))
    time.sleep(1)
    assert lowater_writes(root)==[] and wakes(root)==[]
    kernel(root,(4242,'getnewbuf'),bufmem=3000000)
    until(root,lambda:lowater_writes(root)==[RAISED_LOWATER])
    assert stderr(root).count('cannot help')==1
    kernel(root,lowater=RAISED_LOWATER)
    until(root,lambda:lowater_writes(root)==[RAISED_LOWATER,APPLE_LOWATER])
    wait(report_matches(events,'capped'))


def test_capped_stall_that_clears_is_not_reported_with_telemetry_off(manager):
    root,start,events,wait,_,_=manager
    kernel(root)
    start();wait(started('smbd'))
    kernel(root,(4242,'getnewbuf'),bufmem=RAISED_LOWATER)
    until(root,lambda:'cannot help' in stderr(root))
    kernel(root,bufmem=RAISED_LOWATER)
    until(root,lambda:re.search(r'buffer stall over \(capped, longest wait \d+ s\)\n',stderr(root)))
    time.sleep(1)
    assert lowater_writes(root)==[] and reports(events)==[]


def test_refused_raise_is_logged_once_and_wakes_nothing(manager):
    root,start,events,wait,_,_=manager
    (root/'config').write_text('TELEMETRY=1\n')
    kernel(root,readonly=True)
    start();wait(started('smbd'))
    kernel(root,(4242,'getnewbuf'),readonly=True)
    until(root,lambda:f'could not set vm.bufmem_lowater to {RAISED_LOWATER}' in stderr(root))
    time.sleep(1)
    assert stderr(root).count('could not set')==1 and wakes(root)==[]
    kernel(root,readonly=True)
    wait(report_matches(events,'failed'))


def test_start_restores_a_low_water_mark_left_raised(manager):
    root,start,_,wait,_,_=manager
    kernel(root,lowater=RAISED_LOWATER)
    start();wait(started('smbd'))
    until(root,lambda:lowater_writes(root)==[APPLE_LOWATER])
    until(root,lambda:f'restored vm.bufmem_lowater from {RAISED_LOWATER} to {APPLE_LOWATER}\n' in stderr(root))


def test_refused_restore_is_retried_until_it_works(manager):
    root,start,_,wait,_,_=manager
    kernel(root,lowater=RAISED_LOWATER,readonly=True)
    start();wait(started('smbd'))
    until(root,lambda:f'could not set vm.bufmem_lowater to {APPLE_LOWATER}' in stderr(root))
    time.sleep(1.5)
    assert stderr(root).count('could not set')==1 and lowater_writes(root)==[]
    kernel(root,lowater=RAISED_LOWATER)
    until(root,lambda:lowater_writes(root)==[APPLE_LOWATER])


def test_unreadable_state_is_logged_once_until_it_returns(manager):
    root,start,_,wait,_,_=manager
    start();wait(started('smbd'))
    until(root,lambda:'cannot read buffer-cache state' in stderr(root))
    time.sleep(1.5)
    assert stderr(root).count('cannot read buffer-cache state')==1
    kernel(root)
    until(root,lambda:'buffer-cache state readable again' in stderr(root))


def test_stall_while_stopping_is_still_recovered(manager):
    root,start,_,wait,_,_=manager
    (root/'smbd-ignore-term').touch()
    kernel(root)
    process=start();wait(started('smbd'))
    # smbd ignores SIGTERM, so the manager drains for the grace period, as
    # it would for Samba workers stuck in a buffer wait.
    process.terminate()
    kernel(root,(4242,'getnewbuf'))
    until(root,lambda:lowater_writes(root)==[RAISED_LOWATER])
    until(root,lambda:len(wakes(root))>=1)
    assert process.poll() is None
    # The grace period ends with SIGKILL; the exit restores Apple's value.
    process.wait(timeout=30)
    assert lowater_writes(root)==[RAISED_LOWATER,APPLE_LOWATER]


def test_stopping_during_a_stall_restores_apples_low_water_mark(manager):
    root,start,_,wait,_,_=manager
    kernel(root)
    process=start();wait(started('smbd'))
    kernel(root,(4242,'getnewbuf'))
    until(root,lambda:lowater_writes(root)==[RAISED_LOWATER])
    process.terminate();process.wait(timeout=15)
    assert lowater_writes(root)==[RAISED_LOWATER,APPLE_LOWATER]
    assert f'to {APPLE_LOWATER} at stop' in stderr(root)


def test_unchanged_kernel_and_no_waits_are_never_written(manager):
    root,start,_,wait,_,_=manager
    kernel(root,(4242,'select'),(4243,'nanoslee'))
    start();wait(started('smbd'))
    time.sleep(2)
    assert lowater_writes(root)==[] and wakes(root)==[]
    assert 'vm.bufmem_lowater' not in stderr(root)
