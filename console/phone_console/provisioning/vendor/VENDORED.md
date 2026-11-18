# Vendored: Espressif Network Provisioner client

Source: https://github.com/espressif/idf-extra-components
Path: `network_provisioning/tool/esp_prov` + `network_provisioning/python`
Commit: c8bdc26dbabe6de69448416a8202d7c92728f39d
License: Apache-2.0 (see LICENSE-APACHE-2.0.txt)

Local changes (keep in sync when updating):

1. `esp_prov/proto/__init__.py`: load protocomm/network protobuf modules from
   `vendor/protocomm_python` and `vendor/python` instead of `$IDF_PATH`.
2. `esp_prov/transport/ble_cli.py`: only pass `adapter`/`bluez` to bleak on
   Linux; tolerate descriptor read failures on macOS.
3. `esp_prov/transport/transport_ble.py`: when descriptor lookup fails, derive
   the characteristic UUIDs from the *advertised* service UUID (Network
   Provisioner 1.x uses a randomised base UUID).
