import os
import json
import logging

logger = logging.getLogger(__name__)


def _detect_region(http_client=None):
    region = _detect_region_of_azure_function()  # It is cheap, so we do it always
    if http_client and not region:
        return _detect_region_of_azure_vm(http_client)  # It could hang for minutes
    return region


def _detect_region_of_azure_function():
    return os.environ.get("REGION_NAME")


def _detect_region_of_azure_vm(http_client):
    url = "http://169.254.169.254/metadata/instance?api-version=2021-01-01"
    logger.info(
        "Connecting to IMDS {}. "
        "You may want to use a shorter timeout on your http_client".format(url))
    try:
        # https://docs.microsoft.com/en-us/azure/virtual-machines/windows/instance-metadata-service?tabs=linux#instance-metadata
        resp = http_client.get(url, headers={"Metadata": "true"})
    except:
        logger.info(
            "IMDS {} unavailable. Perhaps not running in Azure VM?".format(url))
        return None
    else:
        return json.loads(resp.text)["compute"]["location"]

