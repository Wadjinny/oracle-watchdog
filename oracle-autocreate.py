#!/usr/bin/env python3
"""
oci_a1_launcher.py

Retries launching an Always Free Ampere A1 (VM.Standard.A1.Flex) compute
instance on Oracle Cloud Infrastructure until host capacity becomes
available in one of the given availability domains.

Why this exists:
    OCI's Always Free Ampere A1 capacity in popular regions (e.g. us-ashburn-1)
    is frequently exhausted. The Console and the API hit the exact same
    LaunchInstance backend, so there's no trick to "unlock" capacity that
    doesn't exist -- but capacity does open up in short windows when other
    tenants terminate or resize instances. This script polls LaunchInstance
    on an interval and grabs the instance the moment a slot is free.

Setup:
    1. install oci
    2. Put credentials under secrets/:
         - oci_config: API config (DEFAULT profile; key_file relative to secrets/)
         - oracle.pem: API signing private key
         - id_ed25519.pub: SSH public key for instance login
    3. Fill in the CONFIG section below with your own OCIDs. You can find:
         - compartment_id: Identity -> Compartments (root compartment OCID
           is on the tenancy details page)
         - subnet_id: Networking -> Virtual Cloud Networks -> your VCN -> subnet
         - image_id: Compute -> Instances -> Create Instance -> Change Image
           (pick "Oracle Linux 9", then use Console dev tools / API docs to
           find the image OCID for your region, or list images with the SDK
           -- see list_images_helper() below)
    4. Run: python3 oracle-autocreate.py

This script does NOT bypass or forge any limits -- it only automates
legitimate retries against your own tenancy, using your own credentials.
"""

import sys
import time
import random
from datetime import datetime
from pathlib import Path

import oci

# ---------------------------------------------------------------------------
# CONFIG -- fill these in for your tenancy before running
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent
SECRETS_DIR = PROJECT_ROOT / "secrets"
OCI_CONFIG_PATH = SECRETS_DIR / "oci_config"
SSH_PUBLIC_KEY_PATH = SECRETS_DIR / "id_ed25519.pub"

CONFIG_PROFILE = "DEFAULT"          # profile name in secrets/oci_config
REGION = "us-ashburn-1"             # your home region

COMPARTMENT_ID = "ocid1.tenancy.oc1..aaaaaaaaf5prrh43q6whbn53y2jl7hn3rkn3zogonbr5w3ubqw3jsirywbra"
SUBNET_ID = "ocid1.subnet.oc1.iad.aaaaaaaafwf6dlsitnqfb4smm7jqbycz23ksvzezay5c2kzkgxnq77sbsbpa"
IMAGE_ID = "ocid1.image.oc1.iad.aaaaaaaagqq77c7jg2dkfuvjdipp7gfkyfyjwst4pc5vkxleknysannp5wrq"

# Availability domains to try, in order, each pass through the loop.
AVAILABILITY_DOMAINS = [
    "swqP:US-ASHBURN-AD-1",
    "swqP:US-ASHBURN-AD-2",
    "swqP:US-ASHBURN-AD-3",
]

INSTANCE_DISPLAY_NAME = "a1-always-free"
SHAPE = "VM.Standard.A1.Flex"
# This tenancy's A1 limits are currently 2 OCPU / 12 GB (not the full 4/24).
# Each attempt uses one (ocpus, memory) pair. A1.Flex needs memory in [ocpus, ocpus*64].
SHAPE_CONFIGS = [
    (2, 12),
    (1, 6),
]

# Capacity-poll pacing (between full AD/shape passes)
MIN_INTERVAL_SECONDS = 30
MAX_INTERVAL_SECONDS = 60

# Rate-limit / transient backoff (exponential with full jitter)
BACKOFF_BASE_SECONDS = 60
BACKOFF_MAX_SECONDS = 15 * 60
BACKOFF_MULTIPLIER = 2.0

# ---------------------------------------------------------------------------


def load_ssh_key(path: Path) -> str:
    with path.open("r") as f:
        return f.read().strip()


def load_oci_config() -> dict:
    import configparser

    parser = configparser.ConfigParser()
    parser.read(OCI_CONFIG_PATH)
    if CONFIG_PROFILE not in parser:
        raise KeyError(f"Profile {CONFIG_PROFILE!r} not found in {OCI_CONFIG_PATH}")
    config = dict(parser[CONFIG_PROFILE])
    key_file = Path(config["key_file"]).expanduser()
    if not key_file.is_absolute():
        key_file = SECRETS_DIR / key_file
    config["key_file"] = str(key_file)
    oci.config.validate_config(config)
    return config


def build_launch_details(
    availability_domain: str,
    ssh_key: str,
    ocpus: float,
    memory_in_gbs: float,
):
    return oci.core.models.LaunchInstanceDetails(
        compartment_id=COMPARTMENT_ID,
        availability_domain=availability_domain,
        display_name=INSTANCE_DISPLAY_NAME,
        shape=SHAPE,
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=float(ocpus),
            memory_in_gbs=float(memory_in_gbs),
        ),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=IMAGE_ID,
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=SUBNET_ID,
            assign_public_ip=True,
        ),
        metadata={
            "ssh_authorized_keys": ssh_key,
        },
    )


def is_capacity_error(service_error: oci.exceptions.ServiceError) -> bool:
    """
    OCI returns a few different shapes of error for "no host capacity",
    depending on the service and moment in time. We match broadly on the
    message text as well as the more specific status/code combinations.
    """
    message = (service_error.message or "").lower()
    capacity_phrases = (
        "out of host capacity",
        "out of capacity",
        "insufficient capacity",
    )
    if any(phrase in message for phrase in capacity_phrases):
        return True
    # Some capacity errors surface as generic 500 InternalError
    if service_error.status == 500 and service_error.code == "InternalError":
        return True
    return False


def is_limit_error(service_error: oci.exceptions.ServiceError) -> bool:
    message = (service_error.message or "").lower()
    return "limit" in message and "exceeded" in message


def is_rate_limit_error(service_error: oci.exceptions.ServiceError) -> bool:
    if service_error.status == 429:
        return True
    code = (service_error.code or "").lower()
    message = (service_error.message or "").lower()
    return (
        code in ("toomanyrequests", "throttled", "ratedexceeded")
        or "too many requests" in message
        or "rate limit" in message
    )


def is_retryable_error(service_error: oci.exceptions.ServiceError) -> bool:
    """Transient API failures that are worth backing off and retrying."""
    if is_rate_limit_error(service_error):
        return True
    # 503 / 502 / occasional 5xx that are not capacity InternalError
    if service_error.status in (502, 503, 504):
        return True
    return False


def next_backoff_seconds(consecutive_failures: int) -> float:
    """
    Full-jitter exponential backoff:
        sleep ~ U(0, min(cap, base * multiplier^n))
    """
    exp = consecutive_failures - 1
    if exp < 0:
        exp = 0
    ceiling = min(
        BACKOFF_MAX_SECONDS,
        BACKOFF_BASE_SECONDS * (BACKOFF_MULTIPLIER ** exp),
    )
    return random.uniform(0, ceiling)


def log(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def notify_success(instance):
    """
    Called once the instance is successfully launched. Extend this to send
    yourself a push notification, e-mail, Slack/Telegram message, etc.
    Left as a simple loud console message + terminal bell by default.
    """
    print("\a" * 3)  # terminal bell, if your terminal supports it
    log("=" * 60)
    log("SUCCESS -- instance launched!")
    log(f"  Display name: {instance.display_name}")
    log(f"  OCID:         {instance.id}")
    log(f"  AD:           {instance.availability_domain}")
    log(f"  Lifecycle:    {instance.lifecycle_state}")
    log("=" * 60)


def list_images_helper(compute_client):
    """
    Handy one-off helper: run this manually (e.g. from a Python REPL) to find
    the image OCID for "Oracle Linux 9" in your region if you don't already
    have it from the Console.
    """
    images = compute_client.list_images(
        compartment_id=COMPARTMENT_ID,
        operating_system="Oracle Linux",
        operating_system_version="9",
        shape=SHAPE,
    ).data
    for img in images:
        print(img.id, img.display_name)


def main():
    if not OCI_CONFIG_PATH.is_file():
        log(f"Could not find OCI config at {OCI_CONFIG_PATH}.")
        sys.exit(1)

    config = load_oci_config()
    config["region"] = REGION
    compute_client = oci.core.ComputeClient(config)

    try:
        ssh_key = load_ssh_key(SSH_PUBLIC_KEY_PATH)
    except FileNotFoundError:
        log(f"Could not find SSH public key at {SSH_PUBLIC_KEY_PATH}. "
            f"Copy your .pub key into secrets/ or generate one with "
            f"`ssh-keygen -t ed25519`.")
        sys.exit(1)

    attempt = 0
    consecutive_backoff_failures = 0
    while True:
        attempt += 1
        hit_retryable = False
        for ad in AVAILABILITY_DOMAINS:
            for ocpus, memory_in_gbs in SHAPE_CONFIGS:
                log(
                    f"Attempt {attempt}: trying {SHAPE} "
                    f"({ocpus} OCPU / {memory_in_gbs} GB) in {ad}..."
                )
                try:
                    details = build_launch_details(
                        ad, ssh_key, ocpus, memory_in_gbs
                    )
                    response = compute_client.launch_instance(details)
                    notify_success(response.data)
                    return
                except oci.exceptions.ServiceError as e:
                    if is_capacity_error(e):
                        consecutive_backoff_failures = 0
                        log(
                            f"  No capacity in {ad} for {ocpus}OCPU/"
                            f"{memory_in_gbs}GB (status={e.status}, "
                            f"code={e.code}). Trying next..."
                        )
                    elif is_retryable_error(e):
                        hit_retryable = True
                        consecutive_backoff_failures += 1
                        sleep_for = next_backoff_seconds(
                            consecutive_backoff_failures
                        )
                        log(
                            f"  Rate/transient error (status={e.status}, "
                            f"code={e.code}): {e.message}"
                        )
                        log(
                            f"  Backing off {sleep_for:.0f}s "
                            f"(failure #{consecutive_backoff_failures}, "
                            f"cap={BACKOFF_MAX_SECONDS}s)..."
                        )
                        time.sleep(sleep_for)
                        # Stop this pass early so we don't keep hammering
                        # the API while throttled.
                        break
                    elif is_limit_error(e):
                        log(f"  Service limit issue: {e.message}")
                        log(
                            "  This is a quota problem, not a capacity "
                            "problem -- retrying won't help until you free "
                            "up or increase your A1 core/memory limits. "
                            "Exiting."
                        )
                        return
                    else:
                        log(
                            f"  Unexpected API error (status={e.status}, "
                            f"code={e.code}): {e.message}"
                        )
                        log("  Exiting -- check your CONFIG values.")
                        return
            else:
                continue
            break

        if hit_retryable:
            # Already slept under the retryable branch; start a fresh pass.
            continue

        sleep_for = random.uniform(MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS)
        log(f"No capacity in any AD this pass. Sleeping {sleep_for:.0f}s...\n")
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()