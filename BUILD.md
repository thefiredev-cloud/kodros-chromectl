# Build guide: kodros-chromectl

## Requirements

- Python 3. Nothing to install.
- Chromium or Chrome with the DevTools port open on loopback.
- For a remote machine: key-based SSH to it (`BatchMode=yes`, no password prompt).

## 1. Open the DevTools port

Add these flags to the browser launch, for example in `~/.config/chromium-flags.conf`:

```text
--remote-debugging-port=9333
--remote-debugging-address=127.0.0.1
```

Restart the browser after changing flags. Never bind the port to a non-loopback address: CDP has no authentication.

## 2. Test

```bash
python3 -m unittest -v
```

The suite starts an in-process stub of the DevTools HTTP routes and WebSocket (`tests/stub_cdp.py`). `CHROMECTL_ENDPOINT=host:port` points the tool at the stub. The in-process tests run under a socket guard that fails any connect to another port. The child-process tests set `CHROMECTL_ENDPOINT` for the script they start, so a real browser on 9333 is never touched.

## 3. Run

```bash
./chromectl status                       # browser version and endpoint
./chromectl tabs                         # id prefix, title, url
./chromectl open URL                     # new background tab, prints its id
./chromectl nav ID URL
./chromectl text ID                      # visible page text
./chromectl eval ID 'document.title'     # JSON result
./chromectl click ID 'button.save'
./chromectl type ID 'input[name=q]' 'hello'
./chromectl key ID Enter
./chromectl shot ID out.png              # no focus change
./chromectl front ID --owner-visible     # only when the person asked
./chromectl close ID
```

An ID can be any unique prefix of the target id. To put it on your PATH: `ln -s "$PWD/chromectl" ~/.local/bin/chromectl`.

## 4. A second machine

`--host remote` reaches Chromium on another machine through an SSH tunnel to its loopback port. The tunnel uses local port 19335.

| Variable | Default | Meaning |
| --- | --- | --- |
| `CHROMECTL_LOCAL_PORT` | `9333` | DevTools port on this machine |
| `CHROMECTL_REMOTE_PORT` | `9335` | DevTools port on the remote machine |
| `CHROMECTL_REMOTE` | `browser-host.example.com` | SSH host of the remote machine (placeholder, set it) |
| `CHROMECTL_ON_REMOTE` | unset | Set to `1` when the script itself runs on the remote machine, so no tunnel opens |
| `CHROMECTL_ENDPOINT` | unset | `host:port` that overrides everything (used by the tests) |

## Layout

```text
chromectl               the whole tool
tests/stub_cdp.py       in-process DevTools stub
tests/test_chromectl.py 36 tests
docs/architecture.svg   architecture diagram
```
