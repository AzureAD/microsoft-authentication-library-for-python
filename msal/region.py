import os
import json
import logging

logger = logging.getLogger(__name__)


def _detect_region(http_client):
    return _detect_region_of_azure_function() or _detect_region_of_azure_vm(http_client)


def _detect_region_of_azure_function():
    return os.environ.get("REGION_NAME")


def _detect_region_of_azure_vm(http_client):
    url = "http://169.254.169.254/metadata/instance?api-version=2021-01-01"
    try:
        # https://docs.microsoft.com/en-us/azure/virtual-machines/windows/instance-metadata-service?tabs=linux#instance-metadata
        resp = http_client.get(url, headers={"Metadata": "true"})
    except:
        logger.info("IMDS {} unavailable. Perhaps not running in Azure VM?".format(url))
        return None
    else:
        return json.loads(resp.text)["compute"]["location"]

