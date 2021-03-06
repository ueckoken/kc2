import asyncio
from base64 import b64encode
import crypt
from enum import Enum
from dataclasses import dataclass
from itertools import filterfalse
import re
from typing import Literal, Optional, Union
from fastapi import FastAPI, Request, Response, Form, Cookie, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pylxd import Client  # type: ignore
from .simplestreams import Product, list_remote_images

app = FastAPI()
templates = Jinja2Templates(directory="templates")

client = Client()

# linuxcontainer.org image server is defined but not supported
# some images on this server does not contain OpenSSH Server by default
LINUXCONTAINER_IMAGE_SERVER = "https://images.linuxcontainers.org"

UBUNTU_IMAGE_SERVER = "https://cloud-images.ubuntu.com/releases"
DEFAULT_IMAGE_SERVER = UBUNTU_IMAGE_SERVER

DEFAULT_ARCH = "amd64"

CLOUDINIT_USERDATA = """
#cloud-config
system_info:
  default_user:
    name: {default_user_name}
    passwd: {hashed_default_user_passwd}
    lock_passwd: false
ssh_pwauth: true

write_files:
- content: |
    proxy_url = "socks5://socks.cc.uec.ac.jp:1080"
  path: /etc/transocks.toml
  owner: root:root
  permissions: '0644'
- content: |
    [Unit]
    Description=transocks: Transparent SOCKS5 proxy
    Documentation=https://github.com/cybozu-go/transocks
    After=network.target

    [Service]
    ExecStart=/usr/local/bin/transocks

    [Install]
    WantedBy=multi-user.target
  path: /etc/systemd/system/transocks.service
  owner: root:root
  permissions: '0644'
- content: |
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
  path: /etc/ufw/before.rules
  append: true
  owner: root:root
  perissions: '0640'
- content: !!binary {transocks_binary_content}
  path: /usr/local/bin/transocks
  owner: root:root
  permissions: '0755'

runcmd:
- systemctl daemon-reload
- systemctl enable transocks
- systemctl start transocks
- ufw enable
- ufw allow 22
- apt update
- apt install -y avahi-daemon
- systemctl enable avahi-daemon
- systemctl start avahi-daemon
- ufw allow 5353
"""


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


CloudinitStatus = Literal["running", "done"]
ContainerStatus = Literal["pending", "running", "stopped"]

# read-only information of Container
@dataclass(frozen=True)
class ContainerInfo:
    name: str
    addresses: list[str]
    status: ContainerStatus


def is_loopback_interface(interface):
    return interface["type"] == "loopback"


def is_ipv4(address):
    return address["family"] == "inet"


def get_addresses(network) -> list[str]:
    addresses: list[str] = []
    for interface in network.values():
        if is_loopback_interface(interface):
            continue
        ipv4_addr = filter(is_ipv4, interface["addresses"])
        addresses += ipv4_addr
    return addresses


def is_running(container) -> bool:
    return container.status == "Running"


def is_stopped(container) -> bool:
    return container.status == "Stopped"


async def get_container_cloudinit_status(container) -> Union[CloudinitStatus, None]:
    loop = asyncio.get_running_loop()
    try:
        exit_code, stdout, stderr = await loop.run_in_executor(
            None, container.execute, ["cloud-init", "status"]
        )
    except Exception:
        return None
    match = re.match(r"status: (?P<status>\w+)$", stdout)
    if match is None:
        return None
    status: CloudinitStatus = match.group("status")
    return status


async def get_container_status(container) -> ContainerStatus:
    if is_running(container):
        cloudinit_status = await get_container_cloudinit_status(container)
        if cloudinit_status is None:
            return "running"
        elif cloudinit_status == "running":
            return "pending"
        elif cloudinit_status == "done":
            return "running"
    else:
        return "stopped"


async def get_container_info(container) -> ContainerInfo:
    status = await get_container_status(container)
    state = container.state()
    addresses = get_addresses(state.network) if state.network is not None else []
    return ContainerInfo(name=container.name, addresses=addresses, status=status)


def is_default_arch(image: RemoteImage) -> bool:
    return image.arch == DEFAULT_ARCH


def is_cloud_image(image: RemoteImage) -> bool:
    if image.variant is None:
        return True
    return image.variant == "cloud"


def is_available_image(image: RemoteImage) -> bool:
    return is_default_arch(image) and is_cloud_image(image)


def product2image(product: Product) -> RemoteImage:
    return RemoteImage(
        aliases=product["aliases"].split(","),
        os=product["os"],
        release=product["release"],
        release_codename=product.get("release"),
        release_title=product["release_title"],
        variant=product.get("variant"),
        arch=product["arch"],
    )


def load_transocks_as_base64_str() -> str:
    with open("bin/transocks", mode="rb") as f:
        transocks_binary = f.read()
    return b64encode(transocks_binary).decode("ascii")


def create_config(
    name: str, default_user_name: str, default_user_passwd: str, image_alias: str
):
    hashed_default_user_passwd = crypt.crypt(default_user_passwd)
    return {
        "name": name,
        "config": {
            "user.user-data": CLOUDINIT_USERDATA.format(
                default_user_name=default_user_name,
                hashed_default_user_passwd=hashed_default_user_passwd,
                transocks_binary_content=load_transocks_as_base64_str(),
            ),
        },
        "source": {
            "type": "image",
            "mode": "pull",
            "server": DEFAULT_IMAGE_SERVER,
            "protocol": "simplestreams",
            "alias": image_alias,
        },
    }


@app.exception_handler(RequestValidationError)
def validation_exception_handler(request, exc):
    return PlainTextResponse(str(exc), status_code=400)


@app.get("/")
def redirect_to_create_container():
    return RedirectResponse("/instances/new")


@app.get("/instances", response_class=HTMLResponse)
async def list_containers(request: Request):
    containers = client.containers.all()
    container_info_list = await asyncio.gather(*map(get_container_info, containers))
    return templates.TemplateResponse(
        "list.html", {"request": request, "containers": container_info_list}
    )


@app.post("/instances")
def create_a_container(
    image_alias: str = Form(...),
    name: str = Form(...),
    default_user_name: str = Form(...),
    default_user_passwd: str = Form(...),
):
    config = create_config(
        name=name,
        default_user_name=default_user_name,
        default_user_passwd=default_user_passwd,
        image_alias=image_alias,
    )
    container = client.containers.create(config, wait=True)
    return RedirectResponse("/instances", status_code=303)


@app.get("/instances/new", response_class=HTMLResponse)
def new_container(request: Request):
    raw_images = list_remote_images(DEFAULT_IMAGE_SERVER)
    images = map(product2image, raw_images)
    available_images = list(filter(is_available_image, images))
    return templates.TemplateResponse(
        "new.html", {"request": request, "images": available_images}
    )


@app.post("/instances/{name}/start")
def start_container(name: str):
    container = client.containers.get(name)
    response = RedirectResponse("/instances", status_code=303)
    # cannot start a running container
    if is_running(container):
        return response
    container.start(wait=True)
    return response


@app.post("/instances/{name}/stop")
def stop_container(name: str):
    container = client.containers.get(name)
    response = RedirectResponse("/instances", status_code=303)
    # cannot stop a stopped container
    if is_stopped(container):
        return response
    container.stop(wait=True)
    return response


@app.post("/instances/{name}/restart")
def restart_container(name: str):
    container = client.containers.get(name)
    response = RedirectResponse("/instances", status_code=303)
    # cannot restart a stopped container
    if is_stopped(container):
        return response
    container.restart(wait=True)
    return response


@app.post("/instances/{name}/destroy")
def destroy_container(name: str):
    container = client.containers.get(name)
    response = RedirectResponse("/instances", status_code=303)
    # cannot delete a running container
    if is_running(container):
        return response
    container.delete(wait=True)
    return response
