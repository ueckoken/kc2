from typing import TypedDict, Union

# minimum rough type definition
# ref: https://github.com/canonical/cloud-init/blob/main/cloudinit/config/cloud-init-schema.json


class DefaultUser(TypedDict, total=False):
    name: str
    passwd: str
    lock_passwd: bool


SshPwauth = bool


class SystemInfo(TypedDict, total=False):
    default_user: DefaultUser


Bootcmd = list[Union[str, list[str]]]


class WriteFilesItem(TypedDict, total=False):
    path: str
    content: str
    owner: str
    permissions: str
    encoding: str
    append: bool
    defer: bool


WriteFiles = list[WriteFilesItem]
Runcmd = list[Union[str, list[str]]]


class UserData(TypedDict, total=False):
    system_info: SystemInfo
    ssh_pwauth: SshPwauth
    bootcmd: Bootcmd
    write_files: WriteFiles
    runcmd: Runcmd
