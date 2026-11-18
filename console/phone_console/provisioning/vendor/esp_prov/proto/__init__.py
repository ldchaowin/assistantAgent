# SPDX-FileCopyrightText: 2018-2024 Espressif Systems (Shanghai) CO LTD
# SPDX-License-Identifier: Apache-2.0
#

import importlib.util
import os
import sys
from importlib.abc import Loader
from typing import Any


def _load_source(name: str, path: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec:
        return None

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert isinstance(spec.loader, Loader)
    spec.loader.exec_module(module)
    return module


esp_prov_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
vendor_path = os.path.dirname(esp_prov_path)
protocomm_path = os.path.join(vendor_path, 'protocomm_python')
network_path = os.path.join(vendor_path, 'python')

# protocomm component related python files generated from .proto files
constants_pb2 = _load_source('constants_pb2', os.path.join(protocomm_path, 'constants_pb2.py'))
sec0_pb2      = _load_source('sec0_pb2',      os.path.join(protocomm_path, 'sec0_pb2.py'))
sec1_pb2      = _load_source('sec1_pb2',      os.path.join(protocomm_path, 'sec1_pb2.py'))
sec2_pb2      = _load_source('sec2_pb2',      os.path.join(protocomm_path, 'sec2_pb2.py'))
session_pb2   = _load_source('session_pb2',   os.path.join(protocomm_path, 'session_pb2.py'))

# network_provisioning component related python files generated from .proto files
network_constants_pb2 = _load_source('network_constants_pb2', os.path.join(network_path, 'network_constants_pb2.py'))
network_config_pb2    = _load_source('network_config_pb2',    os.path.join(network_path, 'network_config_pb2.py'))
network_scan_pb2      = _load_source('network_scan_pb2',      os.path.join(network_path, 'network_scan_pb2.py'))
network_ctrl_pb2    = _load_source('network_ctrl_pb2',    os.path.join(network_path, 'network_ctrl_pb2.py'))
