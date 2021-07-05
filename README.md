# KC2: Koken Compute Cloud

Dead simple Web UI for managing virtual machines for UEC private network build on top of LXD

## Feature

- Super easy to create virtual machine
- Transparent [UEC SOCKS proxy](https://www.cc.uec.ac.jp/ug/ja/network/socks/) out of the box via [transocks](https://github.com/cybozu-go/transocks)
- Resolve `<server-name>.local` to local IP address via preinstalled `avahi-daemon`

## Development

### Requirement

- Python 3.9.x (pyenv recommended)
- poetry
- [transocks](https://github.com/cybozu-go/transocks) binary installed at `bin/transocks`

### Command

- Install dependencies: `poetry install`
- Start development server: `poetry run uvicorn main:app --reload`
- Type check: `poetry run mypy .`
- Format code: `poetry run black .`

## Deployment

Public Docker image: [ghcr.io/ueckoken/kc2](https://github.com/orgs/ueckoken/packages/container/package/kc2)

```
docker run -p 8000:8000 -v /var/snap/lxd/common/lxd/unix.socket:/var/snap/lxd/common/lxd/unix.socket:ro -it ghcr.io/ueckoken/kc2:latest
```
