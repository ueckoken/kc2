import crypt
from enum import Enum
from dataclasses import dataclass
from itertools import filterfalse
from typing import Optional
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

UEC_HTTP_PROXY = "http://proxy.uec.ac.jp:8080/"
UEC_NO_PROXY = "localhost,192.168.0.0/16,172.16.0.0/12,10.0.0.0/8,130.153.0.0/16"

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
    http_proxy={http_proxy}
    https_proxy={http_proxy}
    ftp_proxy={http_proxy}
    no_proxy="{no_proxy}"
    HTTP_PROXY={http_proxy}
    HTTPS_PROXY={http_proxy}
    FTP_PROXY={http_proxy}
    NO_PROXY="{no_proxy}"
  path: /etc/environment
  append: true
  owner: root:root
  permissions: '0644'
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


# read-only information of Container
@dataclass(frozen=True)
class ContainerInfo:
    name: str
    addresses: list[str]
    status: str
    running: bool
    stopped: bool


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


def get_container_info(container) -> ContainerInfo:
    state = container.state()
    addresses = get_addresses(state.network) if state.network is not None else []
    return ContainerInfo(
        name=container.name,
        addresses=addresses,
        status=container.status,
        running=is_running(container),
        stopped=is_stopped(container),
    )


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


def create_config(
    name: str, default_user_name: str, default_user_passwd: str, image_alias: str
):
    hashed_default_user_passwd = crypt.crypt(default_user_passwd)
    return {
        "name": name,
        "config": {
            "environment.http_proxy": UEC_HTTP_PROXY,
            "environment.https_proxy": UEC_HTTP_PROXY,
            "environment.ftp_proxy": UEC_HTTP_PROXY,
            "environment.HTTP_PROXY": UEC_HTTP_PROXY,
            "environment.HTTPS_PROXY": UEC_HTTP_PROXY,
            "environment.FTP_PROXY": UEC_HTTP_PROXY,
            "environment.no_proxy": UEC_NO_PROXY,
            "environment.NO_PROXY": UEC_NO_PROXY,
            "user.user-data": CLOUDINIT_USERDATA.format(
                default_user_name=default_user_name,
                hashed_default_user_passwd=hashed_default_user_passwd,
                http_proxy=UEC_HTTP_PROXY,
                no_proxy=UEC_NO_PROXY,
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
def list_containers(request: Request):
    containers = client.containers.all()
    container_info_list = list(map(get_container_info, containers))
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
