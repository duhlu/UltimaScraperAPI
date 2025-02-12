from pathlib import Path

from pywidevine.cdm import Cdm
from pywidevine.device import Device
from pywidevine.pssh import PSSH
from typing import TYPE_CHECKING, Any
import orjson
import xmltodict
import re
from ultima_scraper_api.apis.onlyfans.classes.extras import endpoint_links
import subprocess

if TYPE_CHECKING:
    import ultima_scraper_api

    auth_types = ultima_scraper_api.auth_types

# Replace authed with your ClientSession
# Replace endpoint_links.drm_server with "https://onlyfans.com/api2/v2/users/media/{MEDIA_ID}/drm/post/{POST_ID}?type=widevine"
# media_item is a Post's media from the "https://onlyfans.com/api2/v2/posts/{USER_ID}?skip_users=all" api


class OnlyDRM:
    def __init__(
        self, client_key_path: Path, private_key_path: Path, authed: "auth_types"
    ) -> None:
        self.client_key = client_key_path.read_bytes()
        self.private_key = private_key_path.read_bytes()
        self.device = Device(
            type_=Device.Types.ANDROID,
            security_level=3,
            flags={},
            client_id=self.client_key,
            private_key=self.private_key,
        )
        self.cdm = Cdm.from_device(self.device)
        self.session_id = self.cdm.open()
        self.authed = authed

    def has_drm(self, media_item: dict[str, Any]):
        try:
            return self.get_dash_url(media_item)
        except KeyError as _e:
            pass

    def extract_hex_id(self, dash_url: str):
        match = re.search(r"/([0-9a-f]{32})/", dash_url)
        assert match
        hex_id = match.group(1)
        return hex_id

    def extract_directory_from_url(self, dash_url: str):
        match = re.search(r"files/(.*?)/\w+\.mpd$", dash_url)
        assert match
        directory = match.group(1)
        return directory

    def get_dash_url(self, media_item: dict[str, Any]):
        drm = media_item["files"]["drm"]
        manifest = drm["manifest"]
        dash: str = manifest["dash"]
        return dash

    async def get_signature(self, media_item: dict[str, Any]):
        drm = media_item["files"]["drm"]
        signature = drm["signature"]["dash"]
        cookie_str = ""
        for key, value in signature.items():
            cookie_str += f"{key}={value}; "
        signature_str = cookie_str.strip()
        return signature_str

    async def get_mpd(self, media_item: dict[str, Any]):
        drm = media_item["files"]["drm"]
        manifest = drm["manifest"]
        signature = drm["signature"]["dash"]
        cookie_str = ""
        for key, value in signature.items():
            cookie_str += f"{key}={value}; "
        cookie_str = cookie_str.strip()
        dash = manifest["dash"]
        r = await self.authed.session_manager.request(
            dash, premade_settings="", custom_cookies=cookie_str
        )
        xml = xmltodict.parse(await r.text())
        mpd: dict[str, Any] = orjson.loads(orjson.dumps(xml))
        return mpd

    async def get_pssh(self, mpd: dict[str, Any]):
        tracks = mpd["MPD"]["Period"]["AdaptationSet"]
        pssh_str = ""
        for video_tracks in tracks:
            if video_tracks["@mimeType"] == "video/mp4":
                for t in video_tracks["ContentProtection"]:
                    if (
                        t["@schemeIdUri"].lower()
                        == "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"
                    ):
                        pssh_str = t["cenc:pssh"]
                        return pssh_str

    async def get_license(
        self, content_item: dict[str, Any], media_item: dict[str, Any], raw_pssh: str
    ):
        pssh = PSSH(raw_pssh)
        challenge = self.cdm.get_license_challenge(self.session_id, pssh)
        url = endpoint_links(
            media_item["id"], content_item["responseType"], content_item["id"]
        ).drm_server
        licence = await self.authed.session_manager.request(
            url, method="POST", data=challenge
        )
        return await licence.read()

    async def get_keys(self, licence: bytes):
        session_id = self.session_id
        cdm = self.cdm
        cdm.parse_license(session_id, licence)
        keys = cdm.get_keys(session_id)
        return keys

    def get_video_url(self, mpd: dict[str, Any], media_item: dict[str, Any]):
        dash_url = self.get_dash_url(media_item)
        directory_url = self.extract_directory_from_url(dash_url)
        base_url = mpd["MPD"]["Period"]["AdaptationSet"][0]["Representation"][0][
            "BaseURL"
        ]
        media_url = f"https://cdn3.onlyfans.com/dash/files/{directory_url}/{base_url}"
        return media_url

    def get_audio_url(self, mpd: dict[str, Any], media_item: dict[str, Any]):
        dash_url = self.get_dash_url(media_item)
        directory_url = self.extract_directory_from_url(dash_url)
        base_url = mpd["MPD"]["Period"]["AdaptationSet"][1]["Representation"]["BaseURL"]
        media_url = f"https://cdn3.onlyfans.com/dash/files/{directory_url}/{base_url}"
        return media_url

    def decrypt_file(self, filepath: Path, key: str):
        output_filepath = Path(filepath.as_posix().replace("enc", "dec"))
        pid = subprocess.Popen(
            [
                "./mp4decrypt",
                f"{filepath.as_posix()}",
                f"{output_filepath.as_posix()}",
                "--key",
                key,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
        )
        pid.wait()
        if pid.stderr:
            error = pid.stderr.read()
            if not error:
                return output_filepath
            else:
                raise Exception(error)
