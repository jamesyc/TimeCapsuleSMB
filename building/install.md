Copy built files to Time Capsule

scp -O -p -r \
  -oHostKeyAlgorithms=+ssh-dss,ssh-rsa \
  -oPubkeyAcceptedAlgorithms=+ssh-rsa \
  ~/tc-stage \
  root@192.168.1.217:/Volumes/dk2/


On the Time Capsule

export PREFIX=/Volumes/dk2/tc-stage
export SAMBA=$PREFIX/samba-min
export LD_LIBRARY_PATH=$PREFIX/lib




scp -O -oHostKeyAlgorithms=+ssh-dss,ssh-rsa  /root/tc-build/hello root@192.168.1.217:/Volumes/dk2/