import config
import sys
import subprocess
import paramiko

def main():
    Get_4GDU_LOG()
    Get_4GCU_LOG()

def Get_4GDU_LOG():
    ##### DU Environment parameter ####
    DU_HOST                 = config.ENB_DU_PARAM["host"]
    DU_USER                 = config.ENB_DU_PARAM["user"]
    DU_PW                   = config.ENB_DU_PARAM["password"]
    DU_SCRIPT_DIR           = config.ENB_DU_PARAM["script_dir"]
    DU_SCRIPT_INSTALL_PATH  = config.ENB_DU_PARAM["script_install_path"] 
    DU_SCRIPT_PATH          = DU_SCRIPT_INSTALL_PATH + "/" +DU_SCRIPT_DIR
    DU_NAMESPACE            = config.ENB_DU_PARAM["namespace"]
    DU_LOG_PATH             = config.ENB_DU_PARAM["log_path"]
    EXPORT_KUBECONF         = config.ENB_DU_PARAM["export_kubeconf"]

    ## Copy Script to DU Server 
    print("Copy 4GDU Getlog Script to 4GDU Controller(" + DU_HOST + ")")
    try:
        CMD = "sshpass -p " + "'" + DU_PW + "' " + "scp -r " + "/cicd/Getlog/" + DU_SCRIPT_DIR + " " + DU_USER + "@" + DU_HOST + ":" + DU_SCRIPT_INSTALL_PATH
        subprocess.run([CMD], shell=True, check=True)
    except subprocess.CalledProcessError as e:
        sys.exit("Failed DUScript-copy!")
    
    ## Perform 4GDU_Getlog ##
    print("##### Perform 4GDU_Getlog #####")
    client_du = paramiko.SSHClient()
    client_du.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        client_du.connect(hostname=DU_HOST, username=DU_USER, password=DU_PW)
        print("ssh connect" + DU_HOST)
    
        # Get DU PODNAME
        GET_DUPODNAME_CMD="/bin/bash ./get_dupod_name.sh " + DU_NAMESPACE
        stdin, stdout, stderr = client_du.exec_command(EXPORT_KUBECONF + "; cd "+ DU_SCRIPT_PATH +"; " + GET_DUPODNAME_CMD)
        for line in stdout:
            DU_PODNAME = line.strip()
    
        # Perform DU-GETLOG
        GET_DULOG_CMD="python3 4gdu_getlog.py " + DU_PODNAME 
        stdin, stdout, stderr = client_du.exec_command(EXPORT_KUBECONF + "; cd "+ DU_SCRIPT_PATH +"; " + GET_DULOG_CMD)
        for line in stdout:
            print(line.strip())
#        for line in stderr:
#            print(line.strip())
  
    except paramiko.AuthenticationException:
        print("Failed Authntication. Please check user/pw")
    except paramiko.SSHException as e:
        print("ssh connection error:{e}")
    finally:
        client_du.close()
  
    # Copy DULog from DU Server ####
    try:
        CMD = "sshpass -p " + "'" + DU_PW + "' " + "scp " + DU_USER + "@" + DU_HOST + ":" + DU_LOG_PATH + "/" + DU_PODNAME + ".tar.gz ~/LOG/"
        subprocess.run([CMD], shell=True, check=True)
    except subprocess.CalledProcessError as e:
        sys.exit("Failed DULog-copy!")
    
    # Delete DUlog from DU Server 
    try:
        CMD = "sshpass -p " + "'" + DU_PW + "' " + "ssh " + DU_USER + "@" + DU_HOST + " " + "rm -rf " + DU_LOG_PATH + "/" + DU_PODNAME + ".tar.gz"
        subprocess.run([CMD], shell=True, check=True)
    except subprocess.CalledProcessError as e:
        sys.exit("Failed delete logfile from DU Server!")
  
    # Show Log file name
    print("***************************************")
    print("**   Output 4GDU Getlog File name    **")
    print("***************************************")
    print("4GDU LOG PATH: ~/LOG/" + DU_PODNAME + ".tar.gz")


def Get_4GCU_LOG():
    ###### CU Environment parameter ####
    CU_HOST                 = config.ENB_CU_PARAM["host"]
    CU_USER                 = config.ENB_CU_PARAM["user"]
    CU_PW                   = config.ENB_CU_PARAM["password"] 
    CU_SCRIPT_DIR           = config.ENB_CU_PARAM["script_dir"] 
    CU_SCRIPT_INSTALL_PATH  = config.ENB_CU_PARAM["script_install_path"]
    CU_SCRIPT_PATH          = CU_SCRIPT_INSTALL_PATH + "/" + CU_SCRIPT_DIR
    CU_LOG_PATH             = CU_SCRIPT_PATH 

    # Script to CU Server 
    print("Copy 4GCU Getlog Script to 4GCU Controller(" + CU_HOST + ")")
    try: 
        CMD = "sshpass -p " + "'" + CU_PW + "' " + "scp -r " + "/cicd/Getlog/" + CU_SCRIPT_DIR + " " + CU_USER + "@" + CU_HOST + ":" + CU_SCRIPT_INSTALL_PATH
        subprocess.run([CMD], shell=True, check=True)
    except subprocess.CalledProcessError as e:
        sys.exit("Failed CUScript-copy!")
    
    # Perform 4GCU_Getlog
    print("##### Perform 4GCU_Getlog #####")
    client_cu = paramiko.SSHClient()
    client_cu.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        client_cu.connect(hostname=CU_HOST, username=CU_USER, password=CU_PW)
        print("ssh connect " + CU_HOST)
    
        # Perform CU-GETLOG
        GET_CULOG_CMD="/bin/bash ./4gcu_getlog.sh " + CU_USER+ " " + CU_PW 
        stdin, stdout, stderr = client_cu.exec_command("cd "+ CU_SCRIPT_PATH +"; " + GET_CULOG_CMD)
        for line in stdout:
            CU_LOGNAME = line.strip()
            print(CU_LOGNAME)
#        for line in stderr:
#            print(line.strip())
    except paramiko.AuthenticationException:
        print("Filad Authntication. Please check user/pw")
    except paramiko.SSHException as e:
        print("ssh connection error:{e}")
    finally:
        client_cu.close()
    
    # Copy CULog from CU Server ####
    try:
        CMD = "sshpass -p " + "'" + CU_PW + "' " + "scp " + CU_USER + "@" + CU_HOST + ":" + CU_LOG_PATH + "/" + CU_LOGNAME + ".tar.gz ~/LOG/"
        subprocess.run([CMD], shell=True, check=True)
    except subprocess.CalledProcessError as e:
        sys.exit("Failed CULog-copy!")
    
    # Delete CUlog from CU Server
    try:
        CMD = "sshpass -p " + "'" + CU_PW + "' " + "ssh " + CU_USER + "@" + CU_HOST + " " + "rm -rf " + CU_LOG_PATH + "/" + CU_LOGNAME + ".tar.gz"
        subprocess.run([CMD], shell=True, check=True)
    except subprocess.CalledProcessError as e:
        sys.exit("Failed delete logfile from CU Server!")
  
    # Show Log file name
    print("***************************************")
    print("**   Output 4GCU Getlog File name    **")
    print("***************************************")
    print("4GCU LOG PATH: ~/LOG/" + CU_LOGNAME + ".tar.gz")



if __name__ == "__main__":
    main()


