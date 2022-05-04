import asyncio
import crypt
from dataclasses import dataclass
import math
import os
import re
from typing import Literal, Optional, TypedDict, Union
from typing_extensions import TypeGuard
from fastapi import FastAPI, Response, Request, Form
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import psutil
from .cloud_init import UserData
from pylxd import Client  # type: ignore
from simplesimplestreams import Product, SimpleStreamsClient
import yaml

app = FastAPI()
templates = Jinja2Templates(directory="templates")

client = Client()

LINUXCONTAINER_IMAGE_SERVER = "https://images.linuxcontainers.org"
UBUNTU_IMAGE_SERVER = "https://cloud-images.ubuntu.com/releases"

ubuntuSsclient = SimpleStreamsClient(url=UBUNTU_IMAGE_SERVER)
lxcSsclient = SimpleStreamsClient(url=LINUXCONTAINER_IMAGE_SERVER)

RemoteEnum = Literal["ubuntu", "lxc"]
remote_enum_map: dict[RemoteEnum, SimpleStreamsClient] = {
    "ubuntu": ubuntuSsclient,
    "lxc": lxcSsclient,
}

LXC_SUPPORTED_IMAGE_PREFIX = ["debian/buster", "archlinux"]

DEFAULT_ARCH = "amd64"

UEC_PROXY_URL = "http://proxy.uec.ac.jp:8080"
UEC_NOPROXY = "130.153.0.0/16"
TRANSOCKS_SERVICE = """
[Unit]
Description=transocks: Transparent SOCKS5 proxy
Documentation=https://github.com/cybozu-go/transocks
After=network.target

[Service]
ExecStart=/usr/local/bin/transocks

[Install]
WantedBy=multi-user.target
"""
TRANSOCKS_IPTABLES_RULE = """
*nat
-F

:PREROUTING ACCEPT [0:0]
:INPUT ACCEPT [0:0]
:OUTPUT ACCEPT [0:0]
:POSTROUTING ACCEPT [0:0]
:TRANSOCKS - [0:0]
-A OUTPUT -j TRANSOCKS
-A TRANSOCKS -d 0.0.0.0/8 -j RETURN
-A TRANSOCKS -d 10.0.0.0/8 -j RETURN
-A TRANSOCKS -d 127.0.0.0/8 -j RETURN
-A TRANSOCKS -d 169.254.0.0/16 -j RETURN
-A TRANSOCKS -d 172.16.0.0/12 -j RETURN
-A TRANSOCKS -d 192.168.0.0/16 -j RETURN
-A TRANSOCKS -d 224.0.0.0/4 -j RETURN
-A TRANSOCKS -d 240.0.0.0/4 -j RETURN
-A TRANSOCKS -d 130.153.0.0/16 -j RETURN
-A TRANSOCKS -p tcp -j REDIRECT --to-ports 1081
-A TRANSOCKS -p icmp -j REDIRECT --to-ports 1081

COMMIT
"""
TRANSOCKS_NFTABLES_RULE = """
table ip nat {
        chain PREROUTING {
                type nat hook prerouting priority -100; policy accept;
        }

        chain INPUT {
                type nat hook input priority 100; policy accept;
        }

        chain POSTROUTING {
                type nat hook postrouting priority 100; policy accept;
        }

        chain OUTPUT {
                type nat hook output priority -100; policy accept;
                counter packets 6 bytes 389 jump TRANSOCKS
        }

        chain TRANSOCKS {
                ip daddr 0.0.0.0/8 counter packets 0 bytes 0 return
                ip daddr 10.0.0.0/8 counter packets 0 bytes 0 return
                ip daddr 127.0.0.0/8 counter packets 0 bytes 0 return
                ip daddr 169.254.0.0/16 counter packets 0 bytes 0 return
                ip daddr 172.16.0.0/12 counter packets 0 bytes 0 return
                ip daddr 192.168.0.0/16 counter packets 4 bytes 269 return
                ip daddr 224.0.0.0/4 counter packets 0 bytes 0 return
                ip daddr 240.0.0.0/4 counter packets 0 bytes 0 return
                ip daddr 130.153.0.0/16 counter packets 1 bytes 60 return
                meta l4proto tcp counter packets 1 bytes 60 redirect to :1081
                meta l4proto icmp counter packets 0 bytes 0 redirect to :1081
        }
}
"""


def is_valid_remote(remote: str) -> TypeGuard[RemoteEnum]:
    return remote == "ubuntu" or remote == "lxc"


def run_cmd_with_uec_proxy_env(cmd: str):
    return f"http_proxy={UEC_PROXY_URL} https_proxy={UEC_PROXY_URL} no_proxy={UEC_NOPROXY} HTTP_PROXY={UEC_PROXY_URL} HTTPS_PROXY={UEC_PROXY_URL} NO_PROXY={UEC_NOPROXY} {cmd}"


def build_cloudinit_userdata_yaml(
    default_user_name: str,
    raw_default_user_passwd: str,
    remote: RemoteEnum,
    image_alias: str,
):
    hashed_default_user_passwd = crypt.crypt(raw_default_user_passwd)
    userdata: UserData = {
        "system_info": {
            "default_user": {
                "name": default_user_name,
                "passwd": hashed_default_user_passwd,
                "lock_passwd": False,
            }
        },
        "ssh_pwauth": True,
        "bootcmd": [
            "sleep 5",  # wait a little for name resolve
            run_cmd_with_uec_proxy_env(
                "wget -l -P /usr/local/bin -O /usr/local/bin/transocks https://github.com/otariidae/transocks/releases/download/v1.1.1+2cf9915/transocks_x86_64"
            ),
            "chmod +x /usr/local/bin/transocks",
        ],
        "write_files": [
            {
                "content": 'proxy_url = "socks5://socks.cc.uec.ac.jp:1080"',
                "path": "/etc/transocks.toml",
                "owner": "root:root",
                "permissions": "0644",
            },
            {
                "content": TRANSOCKS_SERVICE,
                "path": "/etc/systemd/system/transocks.service",
                "owner": "root:root",
                "permissions": "0644",
            },
        ],
        "runcmd": [
            "systemctl daemon-reload",
            "systemctl enable transocks",
            "systemctl start transocks",
        ],
    }
    if remote == "ubuntu":
        userdata["write_files"].append(
            {
                "content": TRANSOCKS_IPTABLES_RULE,
                "path": "/etc/ufw/before.rules",
                "owner": "root:root",
                "append": True,
                "permissions": "0640",
            }
        )
        userdata["runcmd"].extend(
            [
                "ufw enable",
                "ufw allow 22",
                "ufw allow 5353",
                "apt update",
                "apt install -y avahi-daemon",
            ]
        )
    elif image_alias.startswith("debian/buster"):
        userdata["bootcmd"].insert(
            0, run_cmd_with_uec_proxy_env("apt install -y wget nftables openssh-server")
        )
        userdata["write_files"].append(
            {
                "content": TRANSOCKS_NFTABLES_RULE,
                "path": "/etc/nftables.conf",
                "owner": "root:root",
                "permissions": "0644",
            }
        )
        userdata["runcmd"].extend(
            [
                "systemctl enable nftables",
                "systemctl start nftables",
                "apt update",
                "apt install -y avahi-daemon",
            ]
        )
    elif image_alias.startswith("archlinux"):
        userdata["bootcmd"].insert(
            0, run_cmd_with_uec_proxy_env("pacman -S --noconfirm wget openssh")
        )
        userdata["write_files"].append(
            {
                "content": TRANSOCKS_IPTABLES_RULE,
                "path": "/etc/iptables/iptables.rules",
                "owner": "root:root",
                "permissions": "0640",
            }
        )
        userdata["runcmd"].extend(
            [
                "systemctl enable iptables",
                "systemctl start iptables",
                "pacman -S --noconfirm avahi",
            ]
        )
    userdata["runcmd"].extend(
        [
            "systemctl enable avahi-daemon",
            "systemctl start avahi-daemon",
        ]
    )
    userdata_yaml = "#cloud-config\n" + yaml.dump(userdata, width=256)
    return userdata_yaml


InstanceType = Literal["container", "virtual-machine"]

# minimum rough type definition
class InstancePost(TypedDict):
    name: str
    source: dict[str, str]
    config: dict[str, str]


# type-friendly, immutable, and simpler version of Product
@dataclass(frozen=True)
class RemoteImage:
    aliases: list[str]
    arch: str
    os: str
    release: str
    release_codename: Optional[str]
    release_title: str
    variant: Optional[str]
    remote: RemoteEnum


CloudinitStatus = Literal["running", "done"]


@dataclass(frozen=True)
class Address:
    address: str
    netmask: str


# read-only information of Instance
@dataclass(frozen=True)
class InstanceInfo:
    name: str
    status: str
    addresses: list[Address]
    type: Literal["container", "virtual-machine"]


def is_loopback_interface(interface):
    return interface["type"] == "loopback"


def is_ipv4(address):
    return address["family"] == "inet"


def get_addresses(network) -> list[Address]:
    ipv4_addrs: list = []
    for interface in network.values():
        if is_loopback_interface(interface):
            continue
        ipv4_addrs += filter(is_ipv4, interface["addresses"])

    ipv4_addresses = map(
        lambda addr: Address(address=addr["address"], netmask=addr["netmask"]),
        ipv4_addrs,
    )
    return list(ipv4_addresses)


def is_running(instance) -> bool:
    return instance.status == "Running"


def is_stopped(instance) -> bool:
    return instance.status == "Stopped"


async def get_instance_cloudinit_status(instance) -> Union[CloudinitStatus, None]:
    loop = asyncio.get_running_loop()
    try:
        exit_code, stdout, stderr = await loop.run_in_executor(
            None, instance.execute, ["cloud-init", "status"]
        )
    except Exception:
        return None
    match = re.match(r"status: (?P<status>\w+)$", stdout)
    if match is None:
        return None
    status: CloudinitStatus = match.group("status")
    return status


def get_instance_addresses(instance) -> list[Address]:
    state = instance.state()
    addresses = get_addresses(state.network) if state.network is not None else []
    return addresses


def get_instance_info(instance) -> InstanceInfo:
    addresses = get_instance_addresses(instance)
    instance_info = InstanceInfo(
        name=instance.name,
        status=instance.status,
        addresses=addresses,
        type=instance.type,
    )
    return instance_info


def is_default_arch(image: RemoteImage) -> bool:
    return image.arch == DEFAULT_ARCH


def is_cloud_image(image: RemoteImage) -> bool:
    if image.variant is None:
        return True
    return image.variant == "cloud"


def is_available_image(image: RemoteImage) -> bool:
    return is_default_arch(image) and is_cloud_image(image)


def is_supported_lxc_image(image: RemoteImage) -> bool:
    for alias in image.aliases:
        for prefix in LXC_SUPPORTED_IMAGE_PREFIX:
            if alias.startswith(prefix):
                return True
    return False


def product2image(product: Product, ssclient: SimpleStreamsClient) -> RemoteImage:
    remote: RemoteEnum
    if ssclient.url == UBUNTU_IMAGE_SERVER:
        remote = "ubuntu"
    elif ssclient.url == LINUXCONTAINER_IMAGE_SERVER:
        remote = "lxc"
    else:
        remote = "lxc"  # fallback
    return RemoteImage(
        aliases=product["aliases"].split(","),
        os=product["os"],
        release=product["release"],
        release_codename=product.get("release"),
        release_title=product["release_title"],
        variant=product.get("variant"),
        arch=product["arch"],
        remote=remote,
    )


def create_config(
    name: str,
    default_user_name: str,
    default_user_passwd: str,
    remote: RemoteEnum,
    image_alias: str,
    instance_type: InstanceType,
    vcpu: Optional[int],
    memory: Optional[int],
):
    ssclient = remote_enum_map[remote]
    config: InstancePost = {
        "name": name,
        "config": {
            "user.user-data": build_cloudinit_userdata_yaml(
                default_user_name=default_user_name,
                raw_default_user_passwd=default_user_passwd,
                remote=remote,
                image_alias=image_alias,
            ),
        },
        "source": {
            "type": "image",
            "mode": "pull",
            "server": ssclient.url,
            "protocol": "simplestreams",
            "alias": image_alias,
        },
    }
    if instance_type == "virtual-machine":
        if vcpu is not None:
            config["config"]["limits.cpu"] = str(vcpu)
        if memory is not None:
            config["config"]["limits.memory"] = str(memory) + "MB"
    return config


@app.exception_handler(RequestValidationError)
def validation_exception_handler(request, exc):
    return PlainTextResponse(str(exc), status_code=400)


@app.get("/")
def redirect_to_create_instance():
    return RedirectResponse("/instances")


@app.get("/instances", response_class=HTMLResponse)
async def list_instances(request: Request):
    instances = client.instances.all()
    instance_infos = list(map(get_instance_info, instances))
    return templates.TemplateResponse(
        "list.html", {"request": request, "instances": instance_infos}
    )


@app.post("/instances")
def create_a_instance(
    os: str = Form(...),
    instance_type: InstanceType = Form(...),
    vcpu: Optional[int] = Form(None),
    memory: Optional[int] = Form(None),
    name: str = Form(...),
    default_user_name: str = Form(...),
    default_user_passwd: str = Form(...),
):
    [remote, image_alias] = os.split(":")
    if not is_valid_remote(remote):
        return Response(status_code=400)
    config = create_config(
        name=name,
        default_user_name=default_user_name,
        default_user_passwd=default_user_passwd,
        remote=remote,
        image_alias=image_alias,
        instance_type=instance_type,
        vcpu=vcpu,
        memory=memory,
    )
    model = (
        client.virtual_machines
        if instance_type == "virtual-machine"
        else client.containers
    )
    instance = model.create(config, wait=True)
    return RedirectResponse("/instances", status_code=303)


@app.get("/instances/new", response_class=HTMLResponse)
def new_instance(request: Request):
    available_cpu_core = os.cpu_count()
    mem = psutil.virtual_memory()
    available_mem_megabytes = math.ceil(mem.available / 1024 / 1024)
    return templates.TemplateResponse(
        "new.html",
        {
            "request": request,
            "cpu_count": available_cpu_core,
            "available_memory": available_mem_megabytes,
        },
    )


@app.get("/instances/{name}/status")
async def show_status(name: str):
    instance = client.instances.get(name)
    status = await get_instance_cloudinit_status(instance)
    return status


@app.post("/instances/{name}/start")
def start_instance(name: str):
    instance = client.instances.get(name)
    response = RedirectResponse("/instances", status_code=303)
    # cannot start a running instance
    if is_running(instance):
        return response
    instance.start(wait=True)
    return response


@app.post("/instances/{name}/stop")
def stop_instance(name: str):
    instance = client.instances.get(name)
    response = RedirectResponse("/instances", status_code=303)
    # cannot stop a stopped instance
    if is_stopped(instance):
        return response
    instance.stop(wait=True)
    return response


@app.post("/instances/{name}/restart")
def restart_instance(name: str):
    instance = client.instances.get(name)
    response = RedirectResponse("/instances", status_code=303)
    # cannot restart a stopped instance
    if is_stopped(instance):
        return response
    instance.restart(wait=True)
    return response


@app.post("/instances/{name}/destroy")
def destroy_instance(name: str):
    instance = client.instances.get(name)
    response = RedirectResponse("/instances", status_code=303)
    # cannot delete a running instance
    if is_running(instance):
        return response
    instance.delete(wait=True)
    return response


@app.get("/images")
def list_images():
    raw_ubuntu_images = ubuntuSsclient.list_images()
    raw_lxc_images = lxcSsclient.list_images()
    ubuntu_images = map(
        lambda product: product2image(product, ubuntuSsclient), raw_ubuntu_images
    )
    lxc_images = map(
        lambda product: product2image(product, lxcSsclient), raw_lxc_images
    )
    available_ubuntu_images = list(filter(is_available_image, ubuntu_images))
    available_lxc_images = list(
        filter(is_supported_lxc_image, filter(is_available_image, lxc_images))
    )
    available_images = available_ubuntu_images + available_lxc_images
    return available_images
