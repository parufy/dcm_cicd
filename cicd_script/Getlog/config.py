ENB_CU_PARAM = {
    "host": "172.21.232.11",
    "user": "enb-6-admin",
    "password": "enb-6-admin123",
    "script_dir": "cicd_4gcu",
    "script_install_path": "/home/enb-6-admin/log",
}

ENB_DU_PARAM = {
    "host": "172.21.237.171",
    "user": "sysadmin",
    "password": "docomoCM3665!",
    "script_dir": "cicd_4gdu",
    "script_install_path": "/home/sysadmin",
    "namespace": "du1-530401",
    "log_path": "/var/rootdirs/scratch/Getlog/",
    "export_kubeconf": "export KUBECONFIG=/etc/kubernetes/admin.conf"
}
