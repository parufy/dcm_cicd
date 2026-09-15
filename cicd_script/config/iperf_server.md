# U-planeサーバのiperf3自動起動

## hosts.yamlとoperations.yamlを使う構成

`hosts.yaml` の `uplane_server` にSSH管理IP (`address`)、通信先IP (`data_address`)、
SSHユーザー (`user`)、パスワード (`password`)、SSHポート (`port`) を設定してください。
`data_address` を省略すると通信先にも `address` を使用します。
追加したIPとログイン情報はサンプル値です。実環境の値に置き換えてください。

`operations.yaml` に `ping.uplane` と `iperf.uplane_udp_ul` を追加しました。
`target_host: uplane_server` はping宛先を参照し、`server_host: uplane_server` は
iperfの通信先とSSH接続情報を参照します。`host: host1` はUE制御PCの指定です。
参照指定がある場合、ホスト定義から解決した値を使用し、直接指定の `target`、`server`、
`server_ssh_*` より優先します。直接指定に戻す場合は参照項目を `null` にしてください。
iperf待ち受けポート、OS、バイナリパスはoperationsのparamsで指定します。

実行例は `tests/test_scenario_uplane.yaml` です。UE_SERIALを実機のシリアルに変更してください。
この構成では別ファイルの `config.iperf_server` やトップレベル `iperf_server` は不要です。

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
IP・ログイン情報は `config/iperf_server.yaml` にまとめて設定できます。
以下のようにシナリオから読み込みます（パスはシナリオファイル基準）。

```yaml
config:
  hosts: ../config/hosts.yaml
  defaults: ../config/action_defaults.yaml
  operations: ../config/operations.yaml
  iperf_server: ../config/iperf_server.yaml

scenarios:
  - name: UEからU-planeへiperf
    action: iperf
    params:
      adb_serial: "UE_SERIAL"
      duration: 10
```

同じ内容をシナリオのトップレベル `iperf_server:` に直接記載することもできます。
トップレベルに記載がある場合は外部ファイルより優先されるため、外部ファイルを使うときは
既存の `iperf_server:` ブロックを削除してください。
設定の優先順は `defaults.iperf < iperf_server < operationsのparams < ステップのparams` です。
既存ステップの `server` / `port` 等が残っている場合は、そちらが優先されます。
この共通設定はiperf専用です。pingの `target` は別途指定してください。
サンプルのIPは説明用、ログイン情報はプレースホルダーなので、実環境に合わせて変更してください。
