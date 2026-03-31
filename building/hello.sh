# I can ssh into an Apple Time Capsule (NetBSD 6.0 evbarm) using ssh -oHostKeyAlgorithms=+ssh-dss root@192.168.1.217

# Environment is super minimal (tiny mdroot, tiny flash, 16 MB tmpfs). No sftp-server, no compiler, limited tools. 256MB ram. The 2 TB disk is mounted at /Volumes/dk2.

# I am on a NetBSD 10.1 vm as root (no sudo) and want to compile an elf file that runs hello world on the Time Capsule (NetBSD 6.0 evbarm). Note that this requires earmv4. I will use this to cross compile other software later, so build a full sysroot etc

# I have downloaded the netbsd 6 source and extracted it to /usr/src
# ja# ls /root/netbsd6/
# gnusrc.tgz    mk.eabi4.conf sharesrc.tgz  src.tgz       syssrc.tgz    usr           xsrc.tgz
# ja# ls /usr/src
# .git         Makefile     README.md    bin          common       crypto       distrib      etc          games        lib          regress      sbin         sys          tools        usr.sbin
# BUILDING     Makefile.inc UPDATING     build.sh     compat       dist         doc          external     include      libexec      rescue       share        tests        usr.bin
# ja#  

# What are the steps? 

cd /usr/src

OBJ=/root/nb-v4/obj
TOOLS=/root/nb-v4/tools

./build.sh -U -O "$OBJ" -T "$TOOLS" -m evbarm -a earmv4 tools

# ja# ls $TOOLS/
# armv4--netbsdelf-eabi  bin                    include                info                   lib                    libexec                man                    share

./build.sh -U -O "$OBJ" -T "$TOOLS" -m evbarm -a earmv4 -V MKMAN=no -V MKSHARE=no -V MKCATPAGES=no -V MKDEBUGLIB=no idistribution



SRC=/usr/src
OBJ=/root/nb-v4/obj
TOOLS=/root/nb-v4/tools
TOOLDIR=$(ls -d "$TOOLS"/tooldir.NetBSD-*-evbarm)
NBMAKE="$TOOLDIR/bin/nbmake-evbarm"
DEST=/root/nb-v4/destdir.evbarm            # our mini sysroot
mkdir -p "$DEST"






cd /usr/src
export SRC=/usr/src
export OBJ=/root/nb-v4/obj
export TOOLS=/root/nb-v4/tools
./build.sh -U -O "$OBJ" -T "$TOOLS" -m evbarm -a earmv4 tools
export TOOLDIR=$(./build.sh -O "$OBJ" -T "$TOOLS" -m evbarm -a earmv4 -V TOOLDIR tools)
export NBMAKE="$TOOLDIR/bin/nbmake-evbarm"
echo "TOOLDIR=$TOOLDIR"
ls -l "$TOOLDIR/bin"/nbmake-evbarm



TOOLDIR=$(find "$TOOLS" "$OBJ" -maxdepth 1 -type d -name 'tooldir.*-evbarm' | head -n1)
if [ -z "$TOOLDIR" ]; then
  echo "ERROR: could not locate tooldir.*-evbarm under $TOOLS or $OBJ"; exit 1
fi
NBMAKE="$TOOLDIR/bin/nbmake-evbarm"

echo "TOOLDIR = $TOOLDIR"
ls -l "$NBMAKE"


mkdir -p /root/nb-v4/obj /root/nb-v4/tools
cd /usr/src
export OBJ=/root/nb-v4/obj
export TOOLS=/root/nb-v4/tools
./build.sh -U -O "$OBJ" -T "$TOOLS" -m evbarm -a earmv4 tools