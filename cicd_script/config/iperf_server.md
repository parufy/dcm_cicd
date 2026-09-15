# U-planeサーバのiperf3自動起動

WindowsまたはLinuxのU-planeサーバへSSH接続し、iperf3の待ち受けを開始してからUEで通信します。
`server_os: windows` / `server_os: linux` で切り替えます。省略時は従来どおりLinuxです。
サーバにiperf3を事前配置し、SSHユーザーに実行権限と一時ディレクトリへの書き込み権限を与えてください。
バイナリの転送・インストールは行いません。SSH接続は既存のパスワード認証を使用します。

```yaml
scenarios:
  - name: UEからU-planeサーバへUDP送信
    action: iperf
    params:
      host: host1                     # hostsに定義したUE制御PC（SSH）
      adb_serial: "UE_SERIAL"
      adb_iperf_path: /data/local/tmp/iperf3
      server: 192.0.2.10               # UEから到達できるU-plane IPに変更
      port: 5301                      # サーバ待ち受け・UE接続先の共通ポート
      protocol: udp
      direction: ul
      bandwidth: 1M
      duration: 10
      server_adb_serial: null          # サーバ側ではADBを使用しない
      server_auto_start: true
      server_os: windows
      server_iperf_path: 'C:\tools\iperf3\iperf3.exe'
      server_ssh_host: 192.0.2.20      # サーバのSSH管理IPに変更
      server_ssh_user: testuser
      server_ssh_password: "CHANGE_ME"
      server_ssh_port: 22
      server_startup_wait: 2
      output_file: iperf_result.json
```

`host` はUE制御PC、`server` は通信の宛先、`server_ssh_host` はU-planeサーバのSSH接続先です。
管理IPとU-plane IPが同じ場合は、`server` と `server_ssh_host` に同じIPを指定します。
UEを実行環境のローカルADBで操作する場合は `host: null` にします。

## Windows

Windows OpenSSH ServerでのSSH接続と、Windows PowerShellの実行が必要です。
iperf3.exeと必要なDLLを配置し、指定ポートへの通信をWindows Firewallで許可してください
（TCP試験はTCP、UDP試験は制御用TCPとデータ用UDP）。
WindowsのパスはYAMLのシングルクォートで囲むとバックスラッシュをそのまま記述できます。

PowerShellの `Start-Process -WindowStyle Hidden` で指定exeを `-s -p <port> -J` 付きで起動します。
`-D` は使用しません。パスに空白や日本語が含まれる場合も、エンコードしたPowerShellコマンドで渡します。
起動後にプロセスと、そのプロセスが指定TCPポートで待ち受けていることを確認します。
試験終了時は `%TEMP%` の試験専用記録にあるPIDと起動時刻を照合し、`Stop-Process` で停止します。
標準出力・標準エラーの一時ファイルも削除します。

## Linux

Linuxに切り替える場合は、次の2項目を変更します。

```yaml
server_os: linux
server_iperf_path: /opt/iperf3/bin/iperf3
```

SSHユーザーには `/tmp` への書き込み権限が必要です。サーバでは次の形式のコマンドを発行します。

```sh
/opt/iperf3/bin/iperf3 -s -p 5301 -D -J --pidfile /tmp/cicd-iperf-<id>.pid --logfile /tmp/cicd-iperf-<id>.log
```

起動後は指定秒数待ってプロセスの生存を確認します。ネットワーク到達性はUEの実通信で確認します。
試験終了時（UEの通信失敗時も含む）は専用PIDファイルを使って起動したプロセスを停止し、一時ファイルを削除します。
同じサーバで複数の試験を並列実行するときは、試験ごとに異なる `port` を指定してください。

既に待ち受け中のサーバを使う場合は `server_auto_start: false` と `server_adb_serial: null` にします。
`server_auto_start` の初期値は `false` です。共通設定の `server_adb_serial` の初期値は `null` に変更しました。
端末間試験では `server_adb_serial` を明示してください（空文字はADB自動選択）。

共通設定は `action_defaults.yaml` の `defaults.iperf` に書けます。
個別シナリオの `params` が優先されます。トップレベルの `iperf_server` は実行設定には反映されません。
