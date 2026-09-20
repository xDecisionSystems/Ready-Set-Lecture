from __future__ import annotations

import unittest

from app.recorder.core.input_devices import (
    InputDevice, bluetooth_name, device_key, parse_device_path, usages_to_listen_on,
)

# Shapes taken from real Windows paths, with made-up ids.
LE_KEYBOARD = r"\\?\HID#{00001812-0000-1000-8000-00805f9b34fb}_Dev_VID&02045e_PID&0c0f_REV&0596_aabbccddeeff&Col01#9&13243a44&0&0000#{884b96c3-56ef-11d1-bc8c-00a0c91405dd}"
LE_OTHER_PART = LE_KEYBOARD.replace("&Col01", "&Col04").replace("&0&0000", "&0&0003")
CLASSIC = r"\\?\HID#{00001124-0000-1000-8000-00805f9b34fb}_VID&000205ac_PID&022c&Col01#8&11112222&0&0000#{884b96c3-56ef-11d1-bc8c-00a0c91405dd}"
CLASSIC_OTHER_PART = CLASSIC.replace("&Col01", "&Col03").replace("&0&0000", "&0&0002")
CLASSIC_SECOND_REMOTE = CLASSIC.replace("8&11112222&0", "8&33334444&0")
USB = r"\\?\HID#VID_046D&PID_C52B&MI_01&Col02#8&2f5d1a3e&0&0001#{4d1e55b2-f16f-11cf-88cb-001111000030}"
INTERNAL = r"\\?\HID#Target_KIP&Category_HID&Col03#4&1bdc739&0&0002#{4d1e55b2-f16f-11cf-88cb-001111000030}"
ODD = r"\\?\Microsoft HID RID\000D_0002\2"


class FakeRegistry:
    """Enum\\... as nested dicts; paths are matched case-insensitively, like the real registry."""

    def __init__(self, tree: dict) -> None:
        self._tree = tree

    def _node(self, path: str) -> dict | None:
        node = self._tree
        for part in path.lower().split("\\"):
            node = next((v for k, v in node.get("keys", {}).items() if k.lower() == part), None)
            if node is None:
                return None
        return node

    def subkeys(self, path: str) -> list[str]:
        node = self._node(path)
        return list(node.get("keys", {})) if node else []

    def value(self, path: str, name: str) -> str | None:
        node = self._node(path)
        return node.get("values", {}).get(name) if node else None


def registry() -> FakeRegistry:
    return FakeRegistry({"keys": {
        "BTHLE": {"keys": {"Dev_aabbccddeeff": {"keys": {"7&2b&0&aabbccddeeff": {"values": {"FriendlyName": "Test Pen"}}}},
                           "Dev_001122334455": {"keys": {"7&2b&0&001122334455": {"values": {"DeviceDesc": "@bthleenum.inf,%x%;Bluetooth LE Device"}}}}}},
        "BTHENUM": {"keys": {
            "Dev_ccddeeff0011": {"keys": {"7&1&0&BluetoothDevice_ccddeeff0011": {"values": {"FriendlyName": "Remote One"}}}},
            "Dev_ddeeff001122": {"keys": {"7&1&0&BluetoothDevice_ddeeff001122": {"values": {"FriendlyName": "Remote Two"}}}},
            "{00001124-0000-1000-8000-00805f9b34fb}_VID&000205ac_PID&022c": {"keys": {
                "7&1&0&CCDDEEFF0011_C00000000": {"values": {"ParentIdPrefix": "8&11112222&0"}},
                "7&1&0&DDEEFF001122_C00000000": {"values": {"ParentIdPrefix": "8&33334444&0"}}}},
            "{00001101-0000-1000-8000-00805f9b34fb}_LOCALMFG&0000": {"keys": {"x": {"values": {"ParentIdPrefix": "8&11112222&0"}}}},
        }},
    }})


class ParsePathTests(unittest.TestCase):
    def test_a_bluetooth_le_keyboard_part(self) -> None:
        p = parse_device_path(LE_KEYBOARD)
        self.assertEqual((p.bluetooth, p.vid, p.pid, p.address, p.instance, p.collection), (True, 0x045E, 0x0C0F, "aabbccddeeff", "9&13243a44&0", 1))

    def test_classic_bluetooth_has_no_address_but_an_instance_id(self) -> None:
        p = parse_device_path(CLASSIC)
        self.assertEqual((p.bluetooth, p.vid, p.pid, p.address, p.instance, p.collection), (True, 0x05AC, 0x022C, None, "8&11112222&0", 1))

    def test_a_usb_device_is_not_bluetooth(self) -> None:
        p = parse_device_path(USB)
        self.assertEqual((p.bluetooth, p.vid, p.pid, p.collection), (False, 0x046D, 0xC52B, 2))

    def test_internal_and_odd_paths_do_not_crash(self) -> None:
        for path in (INTERNAL, ODD, "", "garbage", "#", "###"):
            parse_device_path(path)
        self.assertFalse(parse_device_path(INTERNAL).bluetooth)
        self.assertIsNone(parse_device_path(ODD).vid)


class DeviceKeyTests(unittest.TestCase):
    def test_all_parts_of_one_device_share_a_key(self) -> None:
        self.assertEqual(device_key(LE_KEYBOARD), device_key(LE_OTHER_PART))
        self.assertEqual(device_key(CLASSIC), device_key(CLASSIC_OTHER_PART))

    def test_different_devices_get_different_keys_even_with_the_same_vendor_and_product(self) -> None:
        self.assertNotEqual(device_key(CLASSIC), device_key(CLASSIC_SECOND_REMOTE))
        self.assertNotEqual(device_key(CLASSIC), device_key(LE_KEYBOARD))
        self.assertNotEqual(device_key(USB), device_key(INTERNAL))

    def test_keys_are_stable_and_readable(self) -> None:
        self.assertEqual(device_key(LE_KEYBOARD), "045e:0c0f:aabbccddeeff")  # the Bluetooth address, once known
        self.assertEqual(device_key(CLASSIC), "05ac:022c:8&11112222&0")
        self.assertEqual(device_key(LE_KEYBOARD.upper()), device_key(LE_KEYBOARD))  # case never changes who a device is

    def test_paths_with_no_ids_still_get_a_key(self) -> None:
        self.assertTrue(device_key(ODD).startswith("----:----:"))


class BluetoothNameTests(unittest.TestCase):
    def test_an_le_device_is_named_by_its_address(self) -> None:
        self.assertEqual(bluetooth_name(LE_KEYBOARD, registry()), "Test Pen")

    def test_a_classic_device_is_named_by_finding_its_parent(self) -> None:
        self.assertEqual(bluetooth_name(CLASSIC, registry()), "Remote One")
        self.assertEqual(bluetooth_name(CLASSIC_OTHER_PART, registry()), "Remote One")

    def test_two_remotes_with_the_same_ids_get_their_own_names(self) -> None:
        self.assertEqual(bluetooth_name(CLASSIC_SECOND_REMOTE, registry()), "Remote Two")

    def test_the_description_is_used_when_there_is_no_friendly_name(self) -> None:
        path = LE_KEYBOARD.replace("aabbccddeeff", "001122334455")
        self.assertEqual(bluetooth_name(path, registry()), "Bluetooth LE Device")

    def test_unknown_or_non_bluetooth_devices_have_no_bluetooth_name(self) -> None:
        self.assertIsNone(bluetooth_name(USB, registry()))
        self.assertIsNone(bluetooth_name(INTERNAL, registry()))
        self.assertIsNone(bluetooth_name(CLASSIC.replace("8&11112222&0", "8&99999999&0"), registry()))
        self.assertIsNone(bluetooth_name(LE_KEYBOARD.replace("aabbccddeeff", "ffffffffffff"), registry()))

    def test_only_bluetooth_hid_services_are_consulted_for_the_parent(self) -> None:
        # the serial-port node in the fake registry has the same ParentIdPrefix; it must not be taken for the HID one
        self.assertEqual(bluetooth_name(CLASSIC, registry()), "Remote One")


class UsagesTests(unittest.TestCase):
    def test_a_known_device_is_listened_to_by_its_own_collections(self) -> None:
        device = InputDevice("k", "T01", True, frozenset({"hid"}), frozenset({(0x0C, 0x01), (0x0D, 0x04)}))
        self.assertEqual(usages_to_listen_on(device), frozenset({(0x0C, 0x01), (0x0D, 0x04)}))

    def test_an_unknown_device_is_listened_to_as_a_keyboard_and_media_control(self) -> None:
        self.assertEqual(usages_to_listen_on(None), frozenset({(0x01, 0x06), (0x0C, 0x01)}))
        self.assertIn((0x01, 0x02), usages_to_listen_on(None, want_mouse=True))


if __name__ == "__main__":
    unittest.main()
