import sys
import subprocess

PODNAME=sys.argv[1]

### Performing 1_GetLogDU.sh ###
try:
  subprocess.run(["/bin/bash","./1_GetLogDU.sh", PODNAME], cwd='/scratch/resources/GetLog')
except subprocess.CalledProcessError as e:
  sys.exit("1_GetLogDU.sh failed!")

### Performing 2_CollectLogDU.sh ###
try:
  subprocess.run(["/bin/bash","2_CollectLogDU.sh", PODNAME], cwd='/scratch/resources/GetLog')
except subprocess.CalledProcessError as e:
  sys.exit("2_CollectLogDU.sh failed!")

### Logfile compression ###
try:
  CMD = "tar -zcvf " + PODNAME + ".tar.gz " + PODNAME
  subprocess.run([CMD], cwd='/var/rootdirs/scratch/Getlog', shell=True)
except subprocess.CalledProcessError as e:
  sys.exit("Failed logfile-compression!")

try:
  CMD = "tar -zcvf " + PODNAME + ".tar.gz " + PODNAME
  subprocess.run([CMD], cwd='/var/rootdirs/scratch/Getlog', shell=True)
except subprocess.CalledProcessError as e:
  sys.exit("Failed logfile-compression!")

CMD = "rm -rf " + PODNAME
subprocess.run([CMD], cwd='/var/rootdirs/scratch/Getlog', shell=True)
