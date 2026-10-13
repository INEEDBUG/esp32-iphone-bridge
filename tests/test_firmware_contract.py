"""Static guardrails for the first, serial-only firmware backend."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKETCH = ROOT / "firmware" / "classic_ble" / "classic_ble.ino"


class FirmwareContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SKETCH.read_text(encoding="utf-8") if SKETCH.exists() else ""

    def test_protocol_is_bounded_json_with_allowlisted_operations(self):
        self.assertRegex(self.source, r"MAX_FRAME_BYTES\s*=\s*512")
        self.assertRegex(self.source, r"MAX_TOKEN_BYTES\s*=\s*64")
        self.assertRegex(self.source, r"MIN_TTL_MS\s*=\s*1")
        self.assertRegex(self.source, r"MAX_TTL_MS\s*=\s*5000")
        for operation in ("hello", "move", "click", "press", "release", "scroll", "type", "key", "stop", "ping", "home"):
            with self.subTest(operation=operation):
                self.assertRegex(self.source, rf'"{operation}"')
        self.assertRegex(self.source, r"MAX_TYPE_BYTES\s*=\s*128")
        self.assertRegex(self.source, r"MAX_MOVE_DELTA\s*=\s*2048")

    def test_json_strings_reject_embedded_nul_before_copy_and_args_are_exact(self):
        copy_start = self.source.index("bool copyJsonString")
        copy_end = self.source.index("bool knownOperation", copy_start)
        string_copy = self.source[copy_start:copy_end]
        self.assertIn("jsonString.size()", string_copy)
        self.assertIn("strlen(cString)", string_copy)
        self.assertLess(string_copy.index("strlen(cString)"), string_copy.index("out = cString"))
        args_start = self.source.index("bool exactArgs = false")
        args_end = self.source.index('error = "invalid_args"', args_start)
        args_contract = self.source[args_start:args_end]
        for operation, count, keys in (
            ("move", 2, ('"dx"', '"dy"')),
            ("scroll", 2, ('"v"', '"h"')),
            ("type", 1, ('"text"',)),
            ("key", 1, ('"key"',)),
        ):
            with self.subTest(operation=operation):
                self.assertIn(f'request.op == "{operation}"', args_contract)
                self.assertIn(f"args.size() == {count}", args_contract)
                for key in keys:
                    self.assertIn(f"args.containsKey({key})", args_contract)
        self.assertIn("exactArgs = args.size() == 0", args_contract)

    def test_session_gates_actions_and_hello_releases_old_state(self):
        self.assertRegex(self.source, r"activeSession")
        self.assertRegex(self.source, r"op\s*==\s*\"hello\"[\s\S]{0,600}armSession")
        self.assertRegex(self.source, r"request\.session\s*==\s*activeSession")
        self.assertRegex(self.source, r"activeSession\s*=\s*request\.session")

    def test_stop_interrupts_work_and_watchdog_disarms(self):
        self.assertRegex(self.source, r"Serial\.available\(\)")
        self.assertRegex(self.source, r"cancelRequested")
        self.assertRegex(self.source, r"op\s*==\s*\"stop\"[\s\S]{0,300}disarm\(\"stopped\", true\)")
        self.assertRegex(self.source, r"SESSION_WATCHDOG_MS\s*=\s*(?:[1-9]\d{0,2}|1\d{3}|2000)")
        self.assertRegex(self.source, r"void releaseAll\(bool transmitRelease\)")
        self.assertIn("bridge.cancelText()", self.source)
        self.assertIn('"duplicate_id"', self.source)
        self.assertRegex(self.source, r"MOVE_STEP_MS\s*=\s*5")

    def test_hid_backend_is_ble_only_and_never_erases_nvs(self):
        self.assertIn("ESP32KeyBridgeEspBle.h", self.source)
        self.assertIn("EspBleHidOutputAdapter", self.source)
        self.assertIn("ble.advertising().start()", self.source)
        self.assertIn("ble.update()", self.source)
        for forbidden in ("WiFi.h", "esp_wifi", "EspBleClassic", "ESP32KeyBridgeEspBleClassic.h", "GPIO", "nvs_flash_erase"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.source)

    def test_build_script_is_compile_only(self):
        script = (ROOT / "scripts" / "Build-Firmware.ps1").read_text(encoding="utf-8")
        self.assertIn("compile --fqbn", script)
        for forbidden in (" upload ", "--upload", "burn-bootloader", "nvs_flash_erase", "SerialPort", "COM3"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, script)
        self.assertIn("CoreSourcePristineSha256", script)
        self.assertIn("CoreSourcePatchedSha256", script)
        self.assertIn("esp_partition_erase_range", script)

    def test_mouse_readiness_is_distinct_and_stop_discards_all_old_motion(self):
        release = self.source[self.source.index('void releaseAll('):self.source.index('bool requestIdSeen(')]
        self.assertIn('commandInput.takeAxisDelta', release)
        self.assertIn('hidOutput.discardPendingMouseMotion()', release)
        self.assertLess(release.index('discardPendingMouseMotion'), release.index('bridge.update'))
        self.assertIn('transmitRelease && hidOutput.mouseReady()', release)
        self.assertIn('const EspBleHidMouseReport releasedMouse = {}', release)
        self.assertIn('response["mouse_ready"] = hidOutput.mouseReady()', self.source)
        self.assertIn('mouse_report_failed', self.source)
        self.assertIn('mouse_not_ready', self.source)
        disconnect = self.source[self.source.index('ble.onDisconnected'):]
        self.assertIn('disarm("ble_disconnected", false)', disconnect)

    def test_core_patch_is_pinned_fail_closed_and_not_an_erase_fallback(self):
        patch = ROOT / "firmware" / "patches" / "arduino-esp32-3.3.11-nvs-fail-closed.patch"
        text = patch.read_text(encoding="utf-8")
        self.assertIn("refusing to erase the NVS partition automatically", text)
        self.assertIn("vTaskDelay(portMAX_DELAY)", text)
        additions = "\n".join(line for line in text.splitlines() if line.startswith("+") and not line.startswith("+++"))
        self.assertNotIn("esp_partition_erase_range", additions)


if __name__ == "__main__":
    unittest.main()
