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
# /statistic/record is app telemetry and is intentionally never called.

CONF_ACCOUNT = "account"
CONF_PASSWORD = "password"
CONF_LANGUAGE = "language"
CONF_REGION = "region"
CONF_MAC = "persistent_mac"
CONF_FCM = "fcm_token"

SERVICE_SET_SEGMENTS = "set_segments"
