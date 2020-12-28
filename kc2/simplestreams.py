from dataclasses import dataclass
from typing import Optional, TypedDict, Any
import requests

# https://github.com/lxc/lxd/blob/f12f03a4ba4645892ef6cc167c24da49d1217b02/shared/simplestreams/index.go
class StreamIndex(TypedDict):
    datatype: str
    path: str
    updated: Optional[str]
    products: list[str]
    format: Optional[str]


class Stream(TypedDict):
    index: dict[str, StreamIndex]
    updated: Optional[str]
    format: str


# https://github.com/lxc/lxd/blob/8fb15e5905c30f8f32339e4867b61a4a77a6dc18/shared/simplestreams/products.go

# some attributes in ProductVersionItem contains hyphen
# cannot use class syntax
# use alternative TypeDict syntax instead
ProductVersionItem = TypedDict(
    "ProductVersionItem",
    {
        "combined_disk1-img_sha256": Optional[str],
        "combined_disk-kvm-img_sha256": Optional[str],
        "combined_uefi1-kvm-img_sha256": Optional[str],
        "combined_rootxz_sha256": Optional[str],
        "combined_sha256": Optional[str],
        "combined_squashfs_sha256": Optional[str],
        "ftype": str,
        "md5": Optional[str],
        "path": str,
        "sha256": Optional[str],
        "size": int,
        "delta_base": Optional[str],
    },
)


class ProductVersion(TypedDict):
    items: dict[str, ProductVersionItem]
    label: Optional[str]
    pubname: Optional[str]


class Product(TypedDict):
    aliases: str
    arch: str
    os: str
    release: str
    release_codename: Optional[str]
    release_title: str
    supported: Optional[bool]
    supported_eol: Optional[str]
    version: Optional[str]
    versions: dict[str, ProductVersion]
    variant: Optional[str]


class Products(TypedDict):
    content_id: str
    datatype: str
    format: str
    license: Optional[str]
    products: dict[str, Product]
    updated: Optional[str]


def get_index(remote: str) -> Stream:
    # https://github.com/lxc/lxd/blob/lxd-4.9/shared/simplestreams/simplestreams.go#L151
    response = requests.get(remote + "/" + "streams/v1/index.json")
    return response.json()


# example: path="streams/v1/images.json"
def list_remote_images_by_path(remote: str, path: str) -> Products:
    response = requests.get(remote + "/" + path)
    return response.json()


def list_remote_images(remote: str) -> list[Product]:
    stream = get_index(remote=remote)["index"]
    all_images: list[Product] = []
    for entry in stream.values():
        # https://github.com/lxc/lxd/blob/lxd-4.9/shared/simplestreams/simplestreams.go#L286
        if entry["datatype"] != "image-downloads":
            continue
        if len(entry["products"]) == 0:
            continue
        path = entry["path"]
        product = list_remote_images_by_path(remote, path)
        products = product["products"]
        all_images += products.values()
    return all_images
