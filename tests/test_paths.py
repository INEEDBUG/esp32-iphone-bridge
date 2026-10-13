import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from phonelab.paths import (
    PathConfigurationError,
    load_local_config,
    resolve_data_root,
    resolve_python_source,
    resolve_pwsh,
)
from phonelab import deploy
from phonelab import usb_capture


class PathResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write_config(self, **values):
        (self.root / ".phonelab-local.json").write_text(
            json.dumps(values), encoding="utf-8")

    def test_environment_overrides_local_data_path_and_explicit_wins(self):
        self.write_config(data_root="config-data")
        env = {"PHONELAB_DATA_ROOT": str(self.root / "env-data"),
               "APPDATA": str(self.root / "appdata")}
        self.assertEqual(resolve_data_root(project_root=self.root, environ=env),
                         (self.root / "env-data").resolve())
        self.assertEqual(resolve_data_root(self.root / "explicit", project_root=self.root,
                                           environ=env),
                         (self.root / "explicit").resolve())

    def test_local_config_and_appdata_fallback_are_outside_project(self):
        self.write_config(data_root="configured-data")
        configured = resolve_data_root(project_root=self.root, environ={})
        self.assertEqual(configured, (self.root / "configured-data").resolve())
        (self.root / ".phonelab-local.json").unlink()
        fallback = resolve_data_root(project_root=self.root,
                                     environ={"APPDATA": str(self.root / "roaming")})
        self.assertEqual(fallback, (self.root / "roaming" / "PhoneLab" / "data").resolve())
        self.assertFalse(fallback.is_relative_to(self.root / "project"))

    def test_pwsh_and_python_source_use_env_then_config_then_path_lookup(self):
        self.write_config(pwsh="config-pwsh.exe", python_source="config-python")
        env = {"PHONELAB_PWSH": "env-pwsh.exe"}
        self.assertEqual(resolve_pwsh(project_root=self.root, environ=env), Path("env-pwsh.exe"))
        self.assertEqual(resolve_python_source(project_root=self.root, environ={}),
                         (self.root / "config-python").resolve())
        self.write_config()
        self.assertEqual(resolve_pwsh(project_root=self.root, environ={},
                                      path_lookup=lambda name: "C:/tools/pwsh.exe"),
                         Path("C:/tools/pwsh.exe"))
        self.assertIsNone(resolve_python_source(project_root=self.root, environ={}))

    def test_malformed_local_configuration_fails_closed(self):
        (self.root / ".phonelab-local.json").write_text("[]", encoding="utf-8")
        with self.assertRaises(PathConfigurationError):
            load_local_config(self.root)

    def test_runtime_modules_share_the_configured_external_data_root(self):
        self.assertEqual(deploy.DATA, resolve_data_root())
        self.assertEqual(usb_capture.TASK_DATA_ROOT, resolve_data_root())
        self.assertFalse(deploy.DATA.is_relative_to(deploy.ROOT))


if __name__ == "__main__":
    unittest.main()
