General plan:
1. Prepare (cross compile etc) netbsd evbarm Samba for Apple Time Capsule on my M1 Mac
2. Follow the setup directions below

How the software works.
1. Use mDNS to detect the Apple Time Capsules on the network
2. Ask user what Time Capsule to modify, get the ip address (or prefer the mDNS hostname (e.g., Basement-AirPort-Time-Capsule.local) over raw IP)
3. Create a virtualenv, then use AirPyrt to enable ssh to root on the Time Capsule
4. Then ssh into the Time Capsule and set up modern Samba


- Paths & mounts: on the TC, Apple’s firmware mounts the disk under /Volumes/dk2 with a ShareRoot dir
- Persistent storage: the tiny internal flash is mounted at /mnt/Flash—handy for configs/binaries that must survive reboots. 
- Host key algorithms: old DSA host keys require a legacy flag on modern clients (-oHostKeyAlgorithms=+ssh-dss). OpenSSH documents this explicitly. 
- Security & updates: if you turn on vfs_fruit for Time Machine, keep an eye on Samba security advisories (there was a widely-publicized vuln in 2022)
- Problem: the 2tb hdd is not mounted unless you use afp and connect. If you disable afp youre screwed. We need a workaround for that
    - Keep Apple filesharing on (for auto-mount), but steal the ports
    - leave File Sharing enabled in AirPort Utility so the disk mounts, then use pf to intercept incoming ports
        - Redirect external TCP 445→1445 and 139→1139 to your Samba build
        - Run Samba bound to the high ports (smb ports = 1445 1139)