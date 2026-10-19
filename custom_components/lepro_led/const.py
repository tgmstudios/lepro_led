DOMAIN = "lepro_led"

# Fork default is North America (matches entries created before `region` existed).
DEFAULT_REGION = "na"
REGIONS = {
    "eu": "api-eu-iot.lepro.com",
    "na": "api-na-iot.lepro.com",
    "fe": "api-fe-iot.lepro.com",
    "us": "api-us-iot.lepro.com",
}

LOGIN_PATH = "/user/login"
FAMILY_LIST_PATH = "/family/list/timestamp/{timestamp}"
USER_PROFILE_PATH = "/user/profile"
DEVICE_LIST_PATH = "/v3/device/list/fid/{fid}/timestamp/{timestamp}"
SCENE_LIST_PATH = "/v3/scene/list/fid/{fid}/did/{did}/cid/0/limit/50/timestamp/{timestamp}"
# /statistic/record is app telemetry and is intentionally never called.

CONF_ACCOUNT = "account"
CONF_PASSWORD = "password"
CONF_LANGUAGE = "language"
CONF_REGION = "region"
CONF_MAC = "persistent_mac"
CONF_FCM = "fcm_token"

SERVICE_SET_SEGMENTS = "set_segments"
SERVICE_SET_SCENE = "set_scene"
SERVICE_APPLY_CLOUD_SCENE = "apply_cloud_scene"
SERVICE_REFRESH_CLOUD_SCENES = "refresh_cloud_scenes"
SERVICE_SEND_DEBUG_COMMAND = "send_debug_command"
SERVICE_REQUEST_DEBUG_STATE = "request_debug_state"
SERVICES = (
    SERVICE_SET_SEGMENTS,
    SERVICE_SET_SCENE,
    SERVICE_APPLY_CLOUD_SCENE,
    SERVICE_REFRESH_CLOUD_SCENES,
    SERVICE_SEND_DEBUG_COMMAND,
    SERVICE_REQUEST_DEBUG_STATE,
)


def canonical_unique_id(account: str, region: str) -> str:
    return f"{account.lower()}_{region}"
