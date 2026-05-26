from __future__ import annotations


class _FakeDevice:
    def __init__(self, data):
        self.data = data


class _FakeManager:
    def __init__(self, devices, user_info):
        self._devices = [_FakeDevice(data) for data in devices]
        self._user_info = user_info
        self._server_ctx = {"token": "ctx"}
        self.refresh_calls = []

    def __iter__(self):
        return iter(self._devices)

    def _refresh_client(self, locate=True):
        self.refresh_calls.append(locate)


class _FakeAPI:
    def __init__(self, manager):
        self.devices = manager


class TestICloudClient:
    def test_refresh_devices_sets_owner_for_family_members(self, mock_state):
        from presencesync.icloud import ICloudClient

        manager = _FakeManager(
            devices=[
                {
                    "id": "family-device",
                    "name": "Pat's iPhone",
                    "deviceDisplayName": "iPhone 15 Pro",
                    "location": {
                        "latitude": 37.7749,
                        "longitude": -122.4194,
                        "horizontalAccuracy": 8,
                        "timeStamp": 1700000000000,
                    },
                    "batteryLevel": 0.42,
                    "batteryStatus": "Unplugged",
                    "deviceClass": "iPhone",
                    "baUUID": "family-beacon",
                    "prsId": "family-prs",
                },
                {
                    "id": "my-device",
                    "name": "My iPhone",
                    "deviceDisplayName": "iPhone 15 Pro",
                    "location": {
                        "latitude": 37.775,
                        "longitude": -122.4195,
                        "horizontalAccuracy": 5,
                        "timeStamp": 1700000005000,
                    },
                    "batteryLevel": 0.81,
                    "batteryStatus": "Charging",
                    "deviceClass": "iPhone",
                    "baUUID": "my-beacon",
                    "prsId": "my-prs",
                },
            ],
            user_info={
                "prsId": "my-prs",
                "familyShareDetails": {
                    "members": [
                        {"prsId": "family-prs", "fullName": "Pat Example"},
                    ],
                },
            },
        )
        client = ICloudClient()
        client._api = _FakeAPI(manager)

        fixes = client.refresh_devices()

        assert manager.refresh_calls == [True]
        assert [fix.owner for fix in fixes] == ["Pat Example", None]

    def test_poll_devices_uses_cached_refresh(self, mock_state):
        from presencesync.icloud import ICloudClient

        manager = _FakeManager(
            devices=[
                {
                    "id": "cached-device",
                    "name": "Cached iPhone",
                    "deviceDisplayName": "iPhone 15 Pro",
                    "location": {
                        "latitude": 37.7749,
                        "longitude": -122.4194,
                        "horizontalAccuracy": 8,
                        "timeStamp": 1700000000000,
                    },
                    "batteryLevel": 0.42,
                    "batteryStatus": "Unplugged",
                    "deviceClass": "iPhone",
                    "prsId": "unknown-prs",
                },
            ],
            user_info={
                "prsId": "my-prs",
                "familyShareDetails": {"members": []},
            },
        )
        client = ICloudClient()
        client._api = _FakeAPI(manager)

        fixes = client.poll_devices()

        assert manager.refresh_calls == [False]
        assert len(fixes) == 1
        assert fixes[0].owner is None
