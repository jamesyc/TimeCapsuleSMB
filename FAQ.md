# Frequently Asked Questions

## General Questions

### What is TimeCapsuleSMB?

TimeCapsuleSMB is a project that configures a modern Samba setup to run directly on Apple AirPort Time Capsules. This allows Time Capsules to work as normal SMB servers on modern macOS systems, since Apple has removed SMB1 support and is removing AFP support.

### Which Time Capsule models are supported?

This project is currently confirmed to work for:
- NetBSD 6 based Time Capsules (automatic startup on boot)
- NetBSD 4 based Time Capsules (manual activation required after each reboot)

Your Time Capsule should work if it looks like the image shown in the README.

### Will this work with my Mac?

TimeCapsuleSMB works with modern macOS versions that support SMB3. However, there are some known issues with specific macOS versions (see below).

## Setup and Configuration

### Do I need to enable "Device Password" mode on my Time Capsule?

**Yes, this is critical!** Your Time Capsule must be set to "Device Password" mode, NOT "User/Password" mode. 

To check/change this:
1. Open AirPort Utility on your Mac
2. Select your Time Capsule
3. Go to Base Station > Edit
4. Navigate to the "Disks" tab
5. Ensure "Secure Shared Disks" is set to "With a device password" (NOT "With account access")

If you use User/Password mode, the Samba setup will not work correctly.

### What macOS versions should I avoid for Time Machine backups?

**Avoid these macOS versions for Time Machine backups:**
- macOS 26.4.x (Time Machine is currently broken)
- macOS 15.7.5+ (Time Machine has issues)

Macs running these versions can still use the device as a standard Samba network share in Finder, but Time Machine backups will not work properly.

See [this article](https://www.cultofmac.com/news/macos-tahoe-26-4-breaks-time-machine-network-backups) for more details on the macOS 26.4 issue.

### Do I need to rebuild the Samba binaries?

No! The working binaries are already included in this repository under the `bin/` directory. You can use them directly without building anything yourself.

### Can I use this if my Time Capsule is my primary router?

**Not recommended.** This is a hobby project built by a developer in their free time, not supported by Apple. While it's not expected to permanently break the Time Capsule, it may mess up your configuration/data. You should be comfortable with the possibility of needing to reset/wipe the device.

## Troubleshooting

### I'm getting "Error 22" or "Invalid Argument" errors

These errors usually indicate **disk corruption**. Try running:

```bash
.venv/bin/tcapsule fsck
```

This will repair the internal disk before deployment. If the issue persists, you may need to consider backing up your data and resetting the Time Capsule.

### The Time Capsule doesn't show up in Finder

Run the diagnostic command:

```bash
.venv/bin/tcapsule doctor
```

This will check:
- Your configuration is valid
- Required tools are available
- Binaries are present and correct
- SSH is reachable
- Bonjour advertisement is working
- SMB connections work

You can also check Bonjour directly:

```bash
dns-sd -B _smb._tcp local.
```

### I have a NetBSD 4 Time Capsule and Samba doesn't start after reboot

This is expected behavior for older NetBSD 4 devices. You need to run:

```bash
.venv/bin/tcapsule activate
```

After every reboot on older hardware. The firmware doesn't persist the boot hook needed for auto-start.

### Finder still doesn't connect even though the device is advertised

Try these steps:
1. Reboot the Time Capsule
2. Try connecting directly with the SMB URL: `smb://timecapsulesamba4.local/Data`
3. Use the IP address from your `.env` file instead of the hostname
4. Run `.venv/bin/tcapsule doctor` to verify everything is working

### Deploy says SMB listing failed right after reboot

This can happen if the device is still finishing startup. These old Time Capsules have slow CPUs. Wait a bit and run:

```bash
.venv/bin/tcapsule doctor
```

## Security and Privacy

### Is this secure enough for internet access?

**No!** This should be treated as a LAN-only setup. Do not expose this SMB service directly to the public internet. Do not forward ports to it. The Time Capsule is old hardware and should not be treated as a hardened NAS.

### What username and password should I use?

After setup, connect with:
- **Username:** `admin`
- **Password:** The same Time Capsule password you entered during configuration

The Samba password is set to match your Time Capsule device password for simplicity. Guest access is disabled.

### Does this disable Apple's AFP and SMB?

**Yes.** This will disable Apple's AFP and SMB file server, so do not expect those to be running at the same time. The `uninstall` script can restore the device to factory clean condition.

## After Installation

### How do I remove TimeCapsuleSMB?

Run:

```bash
.venv/bin/tcapsule uninstall
```

This removes the managed files and can optionally reboot the device. After reboot, your Time Capsule will be restored to factory clean condition.

### Can I delete the TimeCapsuleSMB folder from my Mac?

Yes! All scripts, binaries, and configuration are stored in the TimeCapsuleSMB folder. Once you've completed setup, you can safely delete the folder from your Mac to clean up.

### What if I want to rebuild the binaries myself?

If you're an expert and want to rebuild `smbd` yourself, run the scripts in the `build/` directory on a NetBSD machine. The build process is documented in [DETAIL.md](DETAIL.md).

## Getting Help

### Where can I get help?

If you find any problems, please [file an issue here](https://github.com/jamesyc/TimeCapsuleSMB/issues). The developer is actively working on the project and appreciates feedback.

### What information should I include when reporting an issue?

When filing an issue, please include:
- Your Time Capsule model
- Your macOS version
- Output of `.venv/bin/tcapsule doctor`
- Any error messages you're seeing
- Steps to reproduce the problem
